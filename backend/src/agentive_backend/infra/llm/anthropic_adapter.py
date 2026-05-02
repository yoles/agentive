"""AnthropicProvider — concrete :class:`LLMProvider` wrapping ``langchain-anthropic``.

Why we wrap LangChain rather than the bare Anthropic SDK
--------------------------------------------------------
* LangChain partner SDKs (``langchain-anthropic``) are already pinned by
  Story 1.1 (cf ``backend/pyproject.toml:13``) for the LangGraph spike.
* They normalize the SDK's response shape (``AIMessage`` with
  ``usage_metadata``) — we still re-normalize into our own
  :class:`Completion` because LangChain's typing is *too* permissive
  (``content: str | list[ContentBlock]``).
* The escape hatch :meth:`raw_provider_call` lets callers reach
  Anthropic-specific features (``cache_control``, betas) without leaking
  them through ``complete()``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, ClassVar, cast

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import SecretStr

from agentive_backend.shared.llm.exceptions import (
    LLMError,
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from agentive_backend.shared.llm.redaction import redact_secrets
from agentive_backend.shared.llm.types import ChatMessage, Completion, FinishReason

# Snapshot 2026-05 — verify against
# https://docs.anthropic.com/claude/docs/models-overview#model-pricing
# Tuple = (input USD per 1M tokens, output USD per 1M tokens).
#
# Long-form aliases — Anthropic returns dated identifiers in
# ``response_metadata.model_name`` (e.g. ``claude-sonnet-4-6-20250508``).
# Both forms must be priced or _compute_cost silently returns None for
# the long form, under-reporting LLM_COST_USD_TOTAL — exactly the case
# Story 9.4 budget caps need (review fix-batch P1).
_OPUS_PRICE = (Decimal("15.00"), Decimal("75.00"))
_SONNET_PRICE = (Decimal("3.00"), Decimal("15.00"))
_HAIKU_PRICE = (Decimal("0.80"), Decimal("4.00"))

MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-4-7": _OPUS_PRICE,
    "claude-opus-4-7-20250508": _OPUS_PRICE,
    "claude-sonnet-4-6": _SONNET_PRICE,
    "claude-sonnet-4-6-20250508": _SONNET_PRICE,
    "claude-haiku-4-5": _HAIKU_PRICE,
    "claude-haiku-4-5-20251001": _HAIKU_PRICE,
}

# Anthropic stop_reason → our normalized FinishReason.
_FINISH_REASON_MAP: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_use",
}


def _to_lc_messages(
    messages: Sequence[ChatMessage],
) -> tuple[list[BaseMessage], str | None]:
    """Split out a single concatenated system prompt; map the rest 1:1.

    Anthropic exposes ``system`` as a top-level kwarg, not as an inline
    role. Multiple system messages are concatenated with ``\\n\\n`` to
    preserve ordering.
    """
    system_chunks: list[str] = []
    converted: list[BaseMessage] = []
    for m in messages:
        if m.role == "system":
            system_chunks.append(m.content)
        elif m.role == "user":
            converted.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            converted.append(AIMessage(content=m.content))
    system_prompt = "\n\n".join(system_chunks) if system_chunks else None
    return converted, system_prompt


def _merge_system(system_kwarg: str | None, extracted: str | None) -> str | None:
    """Combine an explicit ``system=`` kwarg with inline system messages.

    Symmetric with the OpenAI adapter — both layer the explicit kwarg
    FIRST (treated as "instance instructions") then concatenate any
    inline system messages with a blank line. Previously the Anthropic
    adapter would drop the inline messages silently when ``system=``
    was passed; the OpenAI adapter would duplicate them. Both now agree
    on append-with-separator (review fix-batch P5).
    """
    if system_kwarg is not None and extracted is not None:
        return f"{system_kwarg}\n\n{extracted}"
    return system_kwarg if system_kwarg is not None else extracted


def _extract_text(content: Any) -> str:
    """LangChain returns ``str`` *or* ``list[ContentBlock]`` — flatten safely.

    The unknown-shape fallback ``str(content)`` runs through
    :func:`redact_secrets` because some SDK responses embed raw request
    metadata (auth headers, query strings) inside dict reprs — we don't
    want a dict cast to leak an API key into ``Completion.text`` (review
    fix-batch P13).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return redact_secrets(str(content))


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    in_price, out_price = pricing
    cost = (
        Decimal(input_tokens) / Decimal(1_000_000) * in_price
        + Decimal(output_tokens) / Decimal(1_000_000) * out_price
    )
    return cost.quantize(Decimal("0.000001"))


def _classify_anthropic_exception(exc: Exception) -> LLMError | None:
    """Convert an SDK / HTTP exception into our domain hierarchy.

    Returns ``None`` when the exception does not match any known SDK or
    HTTP shape — the caller re-raises the original exception so the
    router's :func:`classify_error` routes it via the
    ``(TypeError, ValueError, ValidationError, AttributeError, KeyError)``
    fatal branch. Previously unknown exceptions were silently wrapped as
    ``LLMProviderUnavailableError`` (retriable), which made adapter bugs
    masquerade as transient outages (review fix-batch P12).

    Pure dispatch — does NOT log. The router (or caller) owns logging.
    """
    import httpx

    detail = redact_secrets(str(exc))

    if isinstance(exc, httpx.TimeoutException):
        return LLMProviderTimeoutError(detail=detail)
    if isinstance(exc, httpx.ConnectError):
        return LLMProviderUnavailableError(detail=detail)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 401 or code == 403:
            return LLMProviderAuthError(detail=detail)
        if code == 429:
            return LLMProviderRateLimitError(detail=detail)
        if 400 <= code < 500:
            return LLMProviderBadRequestError(detail=detail)
        if 500 <= code < 600:
            return LLMProviderUnavailableError(detail=detail)
    # Anthropic SDK wraps some errors in its own classes (anthropic.APIStatusError,
    # anthropic.AuthenticationError, …). Inspect by class name to avoid a hard
    # import dependency on the SDK internals (LangChain may swap it).
    cls_name = exc.__class__.__name__
    if "Auth" in cls_name:
        return LLMProviderAuthError(detail=detail)
    if "RateLimit" in cls_name:
        return LLMProviderRateLimitError(detail=detail)
    if "Timeout" in cls_name:
        return LLMProviderTimeoutError(detail=detail)
    if "BadRequest" in cls_name or "NotFound" in cls_name or "InvalidRequest" in cls_name:
        return LLMProviderBadRequestError(detail=detail)
    if "Overload" in cls_name or "ServiceUnavailable" in cls_name:
        return LLMProviderUnavailableError(detail=detail)
    return None


class AnthropicProvider:
    """:class:`LLMProvider` adapter for Anthropic via ``langchain-anthropic``."""

    provider_name: ClassVar[str] = "anthropic"

    def __init__(
        self,
        api_key: SecretStr | str,
        *,
        max_retries: int = 0,
    ) -> None:
        if isinstance(api_key, str):
            api_key = SecretStr(api_key)
        self._api_key: SecretStr = api_key
        self._max_retries = max_retries

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
    ) -> Completion:
        lc_messages, extracted_system = _to_lc_messages(messages)

        # P5 — symmetric system handling: explicit ``system=`` kwarg is
        # combined with any inline system messages rather than dropping
        # them silently.
        effective_system = _merge_system(system, extracted_system)
        if effective_system is not None:
            lc_messages = [SystemMessage(content=effective_system), *lc_messages]

        chat = ChatAnthropic(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            default_request_timeout=timeout_s,
            stop_sequences=list(stop) if stop else None,
            anthropic_api_key=self._api_key,
            max_retries=self._max_retries,
        )

        start = time.perf_counter()
        try:
            response = await chat.ainvoke(lc_messages)
        except Exception as exc:
            translated = _classify_anthropic_exception(exc)
            if translated is None:
                # Unknown shape (programmer error, pydantic mismatch, …).
                # Re-raise raw so classify_error can route as fatal.
                raise
            raise translated from exc
        latency_ms = (time.perf_counter() - start) * 1000.0

        return self._to_completion(response, model, latency_ms)

    async def raw_provider_call(self, **kwargs: Any) -> Any:
        """Pass kwargs straight to ``ChatAnthropic`` and call ``ainvoke``.

        Caller is responsible for the kwargs shape (model, system as
        list[ContentBlock] for prompt caching, betas, …) and for parsing
        the returned :class:`AIMessage`. SDK / HTTP errors are still
        translated into the domain hierarchy so the router's
        :func:`classify_error` works uniformly across complete() and
        raw_provider_call() — caller-side error handling stays consistent.
        """
        kwargs.setdefault("anthropic_api_key", self._api_key)
        messages = kwargs.pop("messages", None)
        if messages is None:
            raise ValueError("raw_provider_call requires a 'messages' kwarg")
        chat = ChatAnthropic(**kwargs)
        try:
            return await chat.ainvoke(messages)
        except Exception as exc:
            translated = _classify_anthropic_exception(exc)
            if translated is None:
                raise
            raise translated from exc

    @staticmethod
    def _to_completion(
        response: BaseMessage, requested_model: str, latency_ms: float
    ) -> Completion:
        text = _extract_text(getattr(response, "content", ""))
        metadata = cast(dict[str, Any], getattr(response, "response_metadata", {}) or {})
        usage = cast(
            dict[str, Any],
            getattr(response, "usage_metadata", None) or metadata.get("usage", {}) or {},
        )

        # `response_metadata.model_name` is the canonical Anthropic-returned
        # model id (often the dated form). Fall back to the requested model.
        model_name = str(metadata.get("model_name") or metadata.get("model") or requested_model)

        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))

        stop_reason = str(metadata.get("stop_reason") or metadata.get("finish_reason") or "")
        finish_reason: FinishReason = _FINISH_REASON_MAP.get(stop_reason, "error")

        provider_request_id = metadata.get("id")
        cost = _compute_cost(model_name, input_tokens, output_tokens)

        return Completion(
            text=text,
            model=model_name,
            provider=AnthropicProvider.provider_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            provider_request_id=str(provider_request_id) if provider_request_id else None,
            cost_estimate_usd=cost,
        )
