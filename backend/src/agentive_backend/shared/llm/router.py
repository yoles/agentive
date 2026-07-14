"""LLMRouter — provider chain with automatic fallback (NFR12, NFR20).

Design choices
--------------
1. **Callback for fallback events** rather than direct ``event_bus.publish_and_commit``.
   The router would otherwise need an ``AsyncSession`` factory in its
   constructor — that pulls a DB dependency into a module that should be
   self-contained. Production wiring binds the callback to event-bus
   publishing in :mod:`agentive_backend.app.lifespan`; tests inject a
   plain ``AsyncMock``.
2. **Model fallback map is per-router** (not a global). Stories that
   want a non-default mapping (e.g. an agent that should never silently
   downgrade Sonnet → Haiku) inject their own map.
3. **Provider list is positional** in ``default_chain`` rather than
   sorted alphabetically — the order is meaningful (primary, secondary, …).

Long-form model IDs
-------------------
Anthropic returns dated model identifiers (e.g.
``claude-sonnet-4-6-20250508``) in ``response_metadata.model_name``. The
canonical short form (``claude-sonnet-4-6``) and the long form must both
be present in :data:`DEFAULT_MODEL_FALLBACK_MAP` so cost / fallback
lookups work regardless of which form callers pass — see Story 1.6
review fix-batch P1.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.llm.config import AgentLLMConfig
from agentive_backend.shared.llm.error_classifier import classify_error
from agentive_backend.shared.llm.exceptions import (
    LLMAllProvidersFailedError,
    LLMNoFallbackModelError,
)
from agentive_backend.shared.llm.interface import Completer, LLMProvider, RawProviderAccess
from agentive_backend.shared.llm.metrics import (
    LLM_COST_USD_TOTAL,
    LLM_FALLBACK_TRIGGERED_TOTAL,
    LLM_REQUEST_LATENCY_SECONDS,
    LLM_REQUESTS_IN_FLIGHT,
    LLM_TOKENS_TOTAL,
)
from agentive_backend.shared.llm.redaction import redact_secrets
from agentive_backend.shared.llm.types import ChatMessage, Completion
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

DEFAULT_MODEL_FALLBACK_MAP: dict[str, dict[str, str]] = {
    # Anthropic primary → OpenAI fallback. Both short and long forms.
    "claude-opus-4-7": {"openai": "gpt-5"},
    "claude-opus-4-7-20250508": {"openai": "gpt-5"},
    "claude-sonnet-4-6": {"openai": "gpt-5"},
    "claude-sonnet-4-6-20250508": {"openai": "gpt-5"},
    "claude-haiku-4-5": {"openai": "gpt-5-mini"},
    "claude-haiku-4-5-20251001": {"openai": "gpt-5-mini"},
    # OpenAI primary → Anthropic fallback.
    "gpt-5": {"anthropic": "claude-sonnet-4-6"},
    "gpt-5-mini": {"anthropic": "claude-haiku-4-5"},
    "gpt-4.1": {"anthropic": "claude-sonnet-4-6"},
    "o4-mini": {"anthropic": "claude-haiku-4-5"},
}


@dataclass(frozen=True, slots=True)
class FallbackContext:
    """Payload handed to the ``on_fallback`` callback.

    ``correlation_id`` is captured at fallback time from the structlog
    ContextVar (or generated as ``"unbound:<uuid>"`` if no request
    context is active — keeps the field non-empty so callbacks that
    require a correlation can still publish the event).
    """

    failed_provider: str
    next_provider: str
    error_class: str
    error_type: str
    model_attempted: str
    model_fallback: str
    correlation_id: str


FallbackCallback = Callable[[FallbackContext], Awaitable[None]]


async def _noop_callback(_ctx: FallbackContext) -> None:  # pragma: no cover - trivial
    return None


class LLMRouter:
    """Multi-provider router with automatic fallback on retriable errors."""

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        default_chain: Sequence[str],
        *,
        model_fallback_map: dict[str, dict[str, str]] | None = None,
        on_fallback: FallbackCallback | None = None,
    ) -> None:
        if not default_chain:
            raise ValueError("default_chain must contain at least one provider name")
        for name in default_chain:
            if name not in providers:
                raise ValueError(
                    f"default_chain references unknown provider {name!r} "
                    f"(known: {sorted(providers)})"
                )
        self._providers: dict[str, LLMProvider] = dict(providers)
        self._default_chain: tuple[str, ...] = tuple(default_chain)
        self._model_fallback_map = (
            model_fallback_map if model_fallback_map is not None else DEFAULT_MODEL_FALLBACK_MAP
        )
        self._on_fallback: FallbackCallback = on_fallback or _noop_callback

    def set_on_fallback(self, callback: FallbackCallback) -> None:
        """Replace the fallback callback (Story 1.6 — used by lifespan wiring).

        Public setter to avoid mutating a private attribute from
        ``app.lifespan`` after construction.
        """
        self._on_fallback = callback

    @property
    def providers(self) -> dict[str, LLMProvider]:
        """Read-only-ish copy — caller can introspect, not mutate routing."""
        return dict(self._providers)

    @property
    def default_chain(self) -> tuple[str, ...]:
        return self._default_chain

    def _resolve_model(self, primary_model: str, target_provider: str, is_primary: bool) -> str:
        if is_primary:
            return primary_model
        mapping = self._model_fallback_map.get(primary_model, {})
        resolved = mapping.get(target_provider)
        if resolved is None:
            raise LLMNoFallbackModelError(
                detail=(
                    f"no fallback model registered for {primary_model!r} → "
                    f"provider {target_provider!r}"
                ),
                context={"primary_model": primary_model, "target_provider": target_provider},
            )
        return resolved

    @classmethod
    def from_agent_config(
        cls,
        agent_config: AgentLLMConfig,
        *,
        providers: dict[str, LLMProvider],
        model_fallback_map: dict[str, dict[str, str]] | None = None,
        on_fallback: FallbackCallback | None = None,
    ) -> RouterCall:
        """Pre-bind a router to an agent's :class:`AgentLLMConfig`.

        Returns a :class:`RouterCall` that exposes only ``complete()`` —
        the agent passes ``messages=[...]`` and the rest of the kwargs
        (model, chain, temperature, …) come from the bound config.

        Used by the M8 Agent Configurator (Story 2.2) so each agent
        instance carries its own LLM contract without touching the
        global router's defaults.
        """
        router = cls(
            providers=providers,
            default_chain=agent_config.provider_chain,
            model_fallback_map=model_fallback_map,
            on_fallback=on_fallback,
        )
        return RouterCall(_router=router, _config=agent_config)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float = 0.7,
        system: str | None = None,
        stop: Sequence[str] | None = None,
        timeout_s: float = 30.0,
        provider_chain: Sequence[str] | None = None,
    ) -> Completion:
        # P10 — refuse empty messages early instead of letting the SDK
        # round-trip a 400 BadRequest (fatal, no fallback). Same intent
        # as a config-validation failure surfaced ASAP.
        if not messages:
            raise ValueError("messages must contain at least one ChatMessage")

        # P9 — explicit None vs falsy: an empty provider_chain is a
        # caller bug, NOT a request to use the default. Refuse it.
        if provider_chain is None:
            chain: tuple[str, ...] = self._default_chain
        else:
            chain = tuple(provider_chain)
            if not chain:
                raise ValueError(
                    "provider_chain is explicitly empty — pass None to use the default"
                )

        for name in chain:
            if name not in self._providers:
                raise ValueError(
                    f"provider_chain references unknown provider {name!r} "
                    f"(known: {sorted(self._providers)})"
                )

        # Capture correlation_id once at entry so every fallback event
        # along this call carries the same id (NFR15 traceability).
        correlation_id = get_correlation_id() or "unbound"

        attempts: list[dict[str, str]] = []
        last_error: Exception | None = None

        for index, provider_name in enumerate(chain):
            is_primary = index == 0
            try:
                resolved_model = self._resolve_model(model, provider_name, is_primary)
            except LLMNoFallbackModelError:
                # No mapping for this fallback — abort the chain and surface
                # the misconfig instead of silently skipping.
                raise

            # ISP (audit §2.4) — the fallback loop only completes, so it
            # depends on the narrow `Completer` port, not the full provider.
            provider: Completer = self._providers[provider_name]
            in_flight = LLM_REQUESTS_IN_FLIGHT.labels(provider=provider_name)
            in_flight.inc()
            attempt_started = time.perf_counter()
            try:
                completion = await provider.complete(
                    messages=messages,
                    model=resolved_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system,
                    stop=stop,
                    timeout_s=timeout_s,
                )
            except Exception as exc:
                last_error = exc
                error_class = classify_error(exc, provider_name)
                # P6 — observe ACTUAL elapsed time on failure path; the
                # previous ``observe(0.0)`` polluted the histogram and
                # made p50 of error buckets look artificially fast.
                attempt_elapsed = time.perf_counter() - attempt_started
                LLM_REQUEST_LATENCY_SECONDS.labels(
                    provider=provider_name,
                    model=resolved_model,
                    status=("error_retriable" if error_class != "fatal" else "error_fatal"),
                ).observe(attempt_elapsed)

                attempts.append(
                    {
                        "provider": provider_name,
                        "model_attempted": resolved_model,
                        "error_type": exc.__class__.__name__,
                        "error_detail": redact_secrets(str(exc))[:500],
                        "error_class": error_class,
                    }
                )

                if error_class == "fatal":
                    raise

                if index + 1 >= len(chain):
                    # No next provider — fall through to LLMAllProvidersFailedError
                    break

                next_provider = chain[index + 1]
                # P7 — the second `_resolve_model` call may also raise
                # LLMNoFallbackModelError (e.g. fallback chain reached an
                # unmapped (model, provider) pair). Append the no-fallback
                # context to ``attempts`` and surface as
                # LLMAllProvidersFailedError so the caller sees both the
                # original retriable failure AND the misconfig.
                try:
                    next_model = self._resolve_model(model, next_provider, is_primary=False)
                except LLMNoFallbackModelError as nfm:
                    attempts.append(
                        {
                            "provider": next_provider,
                            "model_attempted": "<unresolved>",
                            "error_type": nfm.__class__.__name__,
                            "error_detail": redact_secrets(str(nfm))[:500],
                            "error_class": "fatal",
                        }
                    )
                    last_error = nfm
                    break

                LLM_FALLBACK_TRIGGERED_TOTAL.labels(
                    failed_provider=provider_name,
                    next_provider=next_provider,
                    error_class=error_class,
                ).inc()

                ctx = FallbackContext(
                    failed_provider=provider_name,
                    next_provider=next_provider,
                    error_class=error_class,
                    error_type=exc.__class__.__name__,
                    model_attempted=resolved_model,
                    model_fallback=next_model,
                    correlation_id=correlation_id,
                )
                # P4 — never let a callback failure abort the fallback chain.
                # A transient bus / DB outage during ``publish_and_commit``
                # would otherwise convert a recoverable provider failure into
                # a hard error for the caller.
                try:
                    await self._on_fallback(ctx)
                except Exception:
                    _log.exception(
                        "llm_router.fallback_callback_raised",
                        failed_provider=provider_name,
                        next_provider=next_provider,
                    )

                continue
            else:
                LLM_REQUEST_LATENCY_SECONDS.labels(
                    provider=provider_name,
                    model=resolved_model,
                    status="success",
                ).observe(completion.latency_ms / 1000.0)
                LLM_TOKENS_TOTAL.labels(
                    provider=provider_name,
                    model=resolved_model,
                    direction="input",
                ).inc(completion.input_tokens)
                LLM_TOKENS_TOTAL.labels(
                    provider=provider_name,
                    model=resolved_model,
                    direction="output",
                ).inc(completion.output_tokens)
                # P11 — guard against non-finite cost values reaching
                # Counter.inc(float(...)), which raises in prometheus_client.
                if (
                    completion.cost_estimate_usd is not None
                    and completion.cost_estimate_usd.is_finite()
                ):
                    LLM_COST_USD_TOTAL.labels(
                        provider=provider_name,
                        model=resolved_model,
                    ).inc(float(completion.cost_estimate_usd))
                return completion
            finally:
                in_flight.dec()

        # Every provider raised a retriable error (or the chain hit a
        # mid-chain LLMNoFallbackModelError on resolution).
        raise LLMAllProvidersFailedError(
            detail=f"all {len(attempts)} providers failed",
            context={"attempts": attempts},
        ) from last_error

    async def raw_provider_call(self, provider: str, /, **kwargs: Any) -> Any:
        """Direct passthrough to a specific provider's escape hatch."""
        if provider not in self._providers:
            raise ValueError(f"unknown provider {provider!r} (known: {sorted(self._providers)})")
        # ISP (audit §2.4) — this path only needs the raw escape hatch.
        target: RawProviderAccess = self._providers[provider]
        return await target.raw_provider_call(**kwargs)


@dataclass(frozen=True, slots=True)
class RouterCall:
    """Pre-bound :class:`LLMRouter` callable scoped to one agent's config.

    Returned by :meth:`LLMRouter.from_agent_config`. Exposes only
    :meth:`complete` so callers don't need to remember the model /
    chain / kwargs the agent was registered with — same purpose as
    ``functools.partial`` but typed.
    """

    _router: LLMRouter
    _config: AgentLLMConfig

    async def complete(self, messages: Sequence[ChatMessage]) -> Completion:
        return await self._router.complete(
            messages=messages,
            model=self._config.model,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            system=self._config.system,
            stop=self._config.stop,
            timeout_s=self._config.timeout_s,
            provider_chain=self._config.provider_chain,
        )

    @property
    def router(self) -> LLMRouter:
        """Expose the underlying router for raw_provider_call escape hatches."""
        return self._router

    @property
    def config(self) -> AgentLLMConfig:
        return self._config
