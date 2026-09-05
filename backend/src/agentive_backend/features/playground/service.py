"""Playground service — agent test isolation (Story 2.7, FR48).

Loads an ``agent_templates`` row in MEMORY (no INSERT ``agent_instances``
— anti-scope AC2 strict), resolves ``system_prompt`` variables from the
caller's ``arguments``, calls :meth:`LLMRouter.complete`, optionally
resolves tool calls via :func:`infra.mcp.client.call_tool` (bypassing
:class:`ToolHubService.invoke_tool` so the ``tool_hub.tool.invoked`` audit is
NOT published — AC2 isolation), and returns a rich response with the
prompt, raw output, parsed output (best-effort), token usage, cost, and
tool invocation log.

After the run, a SINGLE audit event ``playground.run.completed`` is
published via the bypass pattern (Story 2.1 P-15) with METRICS only —
no prompt/output/args (secret-safety).

Isolation contract (AC2) — enforced by what this module does NOT import :
- No ``MemoryChunkRepo`` ;
- No ``AgentInstanceRepo`` ;
- No ``ToolHubService.invoke_tool`` (would publish tool_hub.tool.invoked).
"""

from __future__ import annotations

import asyncio
import json
import re
import string
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from opentelemetry import trace

from agentive_backend.features.playground.schemas import RunPlaygroundResponse, TokenUsage
from agentive_backend.shared.contracts.events import PlaygroundRunCompletedEvent
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import (
    DependencyError,
    ValidationError,
)
from agentive_backend.shared.llm.types import ChatMessage
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.llm.router import LLMRouter
    from agentive_backend.shared.repositories.ports import (
        AgentTemplateRepository,
        AgentTemplateToolRepository,
    )

_log = get_logger(__name__)

# Audit M-01 (1.4) — named defaults for the LLM call when the template's
# ``config.llm`` snapshot omits a field. The default MODEL is a product
# choice — it lives here as a visible, testable constant instead of a
# literal buried in ``run()``. max_tokens/temperature mirror the
# ``LLMParams`` defaults in m2 schemas (cross-feature import forbidden by
# the ``features-isolated`` contract — keep in sync manually).
#
# ⚠️ Known divergence surfaced by the audit: this model is NOT in the m2
# ``LLMModel`` whitelist (claude-3-5-*/gpt-4o*). To resolve when the
# whitelist is refreshed (Story 4.6).
#
# P-13 (fix-batch 2026-08-31, reviewed — kept as-is) : the code-review asked
# for this default to read from a host-level ``settings.default_llm_model``
# instead of being hardcoded. No such setting exists anywhere in this
# codebase yet (`grep -rn default_llm_model backend/` = 0 hits) — inventing
# one here would be a new config surface with no product decision behind it.
# ``DEFAULT_LLM_MODEL`` is already present in ``LLMRouter``'s own fallback
# map (``shared/llm/router.py`` — this exact string), so it is a working,
# testable default, not a hardcoded literal. Deferred, not fixed.
DEFAULT_LLM_MODEL: Final = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS: Final = 4096
DEFAULT_TEMPERATURE: Final = 0.7
# P-19 (fix-batch 2026-08-31) — hard ceiling on a template's
# ``config.llm.max_tokens``, independent of what the template stores.
# Story 9.4 (FinOps) owns real budget caps ; this is a Sprint-1 safety net.
MAX_TOKENS_HARD_CAP: Final = 16_000


# H-03 (fix-batch 2026-09-02): a template's ``system_prompt`` can amplify
# a bounded ``arguments`` payload into a multi-hundred-MB (or larger)
# string via format-spec width/precision (``{a:>999999999}``, ~1 GB from
# a single 15-byte field) or plain field repetition (``"{a}" * 500``
# against a 64 KiB argument, ~32 MB). The 64 KiB cap on the REQUEST
# ``arguments`` (schemas.py P-32) does not bound the RESOLVED OUTPUT, and
# this happens BEFORE any LLM call: a cost/memory-DoS vector reachable
# without a valid provider API key. Both constants are Sprint-1 safety
# nets, not real budget caps (Story 9.4 owns those).
_MAX_RESOLVED_PROMPT_CHARS: Final = 65_536
_MAX_FORMAT_SPEC_NUMBER: Final = 10_000
_DIGITS_RE = re.compile(r"\d+")


class _SafeFormatter(string.Formatter):
    """Restricted ``string.Formatter`` for ``system_prompt`` resolution.

    ``str.format_map`` allows attribute / index traversal in replacement
    fields (``{x.__class__}``, ``{x[0]}``) — the classic Python
    format-string info-disclosure vector. Since ``prompt_resolved`` is
    echoed back to the caller, a malicious or malformed template could
    exfiltrate server-side object internals. This formatter keeps plain
    ``{variable}`` substitution (and format specs) but rejects any field
    traversal with ``ValueError`` — translated to a sanitized 422 by the
    caller.

    H-03: also bounds what each substitution can produce. A fresh
    instance MUST be created per resolution call (never shared as a
    module-level singleton) : ``_running_len`` is per-call state and
    would race across concurrent requests otherwise.
    """

    def __init__(self) -> None:
        super().__init__()
        self._running_len = 0

    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        if "." in field_name or "[" in field_name:
            raise ValueError(f"unsupported field reference: {field_name!r}")
        return super().get_field(field_name, args, kwargs)

    def format_field(self, value: Any, format_spec: str) -> str:
        # Reject an absurd width/precision BEFORE calling format(): the
        # allocation happens inside format(), so checking the OUTPUT length
        # after the fact is too late for a single-field bomb like
        # "{a:>999999999}" (~1 GB from a 15-byte spec).
        for number in _DIGITS_RE.findall(format_spec):
            if int(number) > _MAX_FORMAT_SPEC_NUMBER:
                raise ValueError(f"format spec {format_spec!r} exceeds the width/precision cap")
        formatted: str = super().format_field(value, format_spec)
        self._running_len += len(formatted)
        if self._running_len > _MAX_RESOLVED_PROMPT_CHARS:
            # Catches the repetition vector ("{a}" * 500 against a 64 KiB
            # argument) : bails out on the field that crosses the cap
            # instead of waiting for the whole template to be resolved, so
            # the transient allocation stays bounded near the cap rather
            # than growing with the number of repeated fields.
            raise ValueError("resolved system_prompt exceeds the Sprint 1 size limit")
        return formatted


def _best_effort_json(raw_output: str) -> dict[str, Any] | None:
    """Parse ``raw_output`` as a JSON object, best-effort (Sprint 1 — full JSON
    Schema validation against the output_contract deferred Story 4.x). Returns
    ``None`` on any parse failure or a non-object payload."""
    try:
        candidate = json.loads(raw_output)
    except json.JSONDecodeError, TypeError, ValueError:
        return None
    return candidate if isinstance(candidate, dict) else None


@dataclass(frozen=True, slots=True)
class _LLMOutcome:
    """Result of the LLM completion step — the value ``run`` audits once then
    either raises (``error``) or turns into the response. Audit A-09: lets the
    success and llm_error paths converge on a single audit call."""

    status: Literal["success", "llm_error", "cancelled"]
    raw_output: str
    # IG-01 / H-04 (fix-batch 2026-09-02): ``None`` on the cancelled path,
    # the LLM call was interrupted before returning a ``Completion``, so
    # actual token usage is genuinely UNKNOWN, not zero. Writing ``0`` here
    # used to assert a false fact into the audit trail (a cancelled-but-
    # billed run looked, to FinOps Story 9.4, like it cost nothing).
    input_tokens: int | None
    output_tokens: int | None
    cost_estimate_usd: Decimal | None
    model_used: str
    provider_used: str
    # P-11 (fix-batch 2026-08-31) — widened from ``Exception`` to
    # ``BaseException`` : ``asyncio.CancelledError`` is a ``BaseException``
    # subclass (not ``Exception``, since Python 3.8), and this field now
    # also carries it on the cancellation path.
    error: BaseException | None


class PlaygroundService:
    """Run an agent-template in isolation — Story 2.7 FR48."""

    def __init__(
        self,
        *,
        template_repo: AgentTemplateRepository,
        assignment_repo: AgentTemplateToolRepository,
        llm_router: LLMRouter,
    ) -> None:
        # P-24 (fix-batch 2026-08-31) — a ``tool_repo`` param used to be
        # accepted here and stored unused ; Sprint 1 never resolves tool
        # calls (D80), so nothing in this service touches the tools table
        # directly. Removed rather than kept "for later" (YAGNI).
        self._template_repo = template_repo
        self._assignment_repo = assignment_repo
        self._llm_router = llm_router

    async def run(
        self,
        *,
        template_id: UUID,
        arguments: dict[str, Any],
        enabled_tool_ids: list[UUID] | None,
        timeout_seconds: float,
        tenant_id: UUID | None = None,
    ) -> RunPlaygroundResponse:
        """Execute a Playground run — AC1-AC6.

        Raises
        ------
        NotFoundError
            ``template_id`` does not exist.
        ValidationError
            ``arguments`` is missing a variable referenced by the
            template's ``system_prompt``, or the template could not be
            resolved (positional field, attribute/index traversal,
            malformed format spec — audit A-01).
        ValidationError
            ``enabled_tool_ids`` references a tool NOT assigned to this
            template.
        DependencyError
            LLM provider failed (timeout / rate limit / classifier
            DependencyError — cf Story 1.6 error classifier).
        """
        # A-09 (audit §2.1a) — the method reads as its own table of contents;
        # each numbered step is a focused, independently-testable helper.
        start = time.monotonic()

        # C-01 (fix-batch 2026-09-02): AC2 requires every Playground run
        # to carry ``playground.run=true`` on its OTel span, unconditionally
        # (success, llm_error, AND cancelled). ``get_current_span()``
        # returns a safe no-op ``NonRecordingSpan`` when no tracer provider
        # is configured (opentelemetry-api only is installed, no SDK/
        # exporter), so this stays inert until a real one is wired.
        trace.get_current_span().set_attribute("playground.run", True)

        # 1. Load the template snapshot in memory — NO INSERT into
        #    agent_instances (AC2 isolation). 404 if the template is missing.
        config_snapshot, assigned_tools = await self._load_config_and_tools(template_id, tenant_id)
        # 2. Resolve how many assigned tools this run activates (validates
        #    enabled_tool_ids against the template's assignments).
        tools_activated_count = self._count_activated_tools(
            assigned_tools, enabled_tool_ids, template_id
        )
        # 3. Resolve the system prompt from the caller's arguments (sanitized
        #    422 on any resolution failure — audit A-01 / CR P-02).
        prompt_resolved = self._resolve_prompt(config_snapshot, arguments, template_id)
        # 4. Call the LLM, capturing the outcome (never raising inline) so the
        #    audit is published exactly once regardless of success/failure.
        outcome = await self._complete(
            config_snapshot=config_snapshot,
            prompt_resolved=prompt_resolved,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            template_id=template_id,
        )

        duration_ms_total = int((time.monotonic() - start) * 1000)

        # 5. Single audit-publish path (AC5, exactly one event) for the
        #    success, llm_error, AND cancelled outcomes.
        #
        # H-01 (fix-batch 2026-09-02): wrapped in ``asyncio.shield()``. If
        # this request's cancellation was raised by a LEVEL-TRIGGERED
        # cancel scope (anyio, used internally by Starlette's
        # ``BaseHTTPMiddleware``), the scope stays "cancelled" across
        # every checkpoint until it is exited, unlike a bare
        # ``asyncio.Task.cancel()``, where catching the first
        # ``CancelledError`` in ``_complete`` does NOT clear it. Without
        # ``shield()`` the very next ``await`` here (opening the audit
        # transaction) would immediately re-raise, and a cancelled run
        # would publish ZERO audit events instead of exactly one (AC5).
        # ``shield()`` detaches the publish into its own task so it
        # finishes even if THIS await is re-cancelled.
        await asyncio.shield(
            self._publish_audit_event(
                template_id=template_id,
                tools_activated=tools_activated_count,
                duration_ms_total=duration_ms_total,
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                cost_estimate_usd=outcome.cost_estimate_usd,
                model_used=outcome.model_used,
                provider_used=outcome.provider_used,
                status=outcome.status,
                tenant_id=tenant_id,
            )
        )

        if outcome.error is not None:
            raise outcome.error

        # 6. Best-effort parse + response (success path only).
        parsed_output = _best_effort_json(outcome.raw_output)

        _log.info(
            "playground_run_completed",
            template_id=str(template_id),
            tools_activated=tools_activated_count,
            duration_ms_total=duration_ms_total,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            status=outcome.status,
            model_used=outcome.model_used,
            provider_used=outcome.provider_used,
            playground_run=True,  # AC2 OTel-style flag for log filtering
        )

        return RunPlaygroundResponse(
            prompt_resolved=prompt_resolved,
            raw_output=outcome.raw_output,
            parsed_output=parsed_output,
            tokens=TokenUsage(
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
            ),
            cost_estimate_usd=outcome.cost_estimate_usd,
            model_used=outcome.model_used,
            provider_used=outcome.provider_used,
            tool_invocations=[],  # Sprint 1 — D80 defer Story 4.x
            duration_ms_total=duration_ms_total,
        )

    # ─── run helpers (A-09 decomposition) ─────────────────────────

    async def _load_config_and_tools(
        self, template_id: UUID, tenant_id: UUID | None
    ) -> tuple[dict[str, Any], list[tuple[Any, Any]]]:
        """Load the template config snapshot + its tool assignments in a single
        session. 404 ``NotFoundError`` if the template is missing."""
        async with self._template_repo.with_tenant(tenant_id) as session:
            template = await self._template_repo.require_by_id_in_session(session, template_id)
            config_snapshot: dict[str, Any] = dict(template.config or {})
            assigned_tools = await self._assignment_repo.list_by_template_in_session(
                session, template_id
            )
        return config_snapshot, assigned_tools

    @staticmethod
    def _count_activated_tools(
        assigned_tools: list[tuple[Any, Any]],
        enabled_tool_ids: list[UUID] | None,
        template_id: UUID,
    ) -> int:
        """Count activated tools; ``None`` means all assigned. Raises 422 if
        ``enabled_tool_ids`` references a tool not assigned to the template."""
        assigned_tool_ids: set[UUID] = {tool.id for tool, _at in assigned_tools}
        if enabled_tool_ids is None:
            return len(assigned_tool_ids)
        requested = set(enabled_tool_ids)
        missing = requested - assigned_tool_ids
        if missing:
            raise ValidationError(
                detail=(
                    f"enabled_tool_ids contains {len(missing)} tool(s) "
                    f"not assigned to template '{template_id}'"
                ),
                context={
                    "template_id": str(template_id),
                    "missing_tool_ids": [str(t) for t in missing],
                },
            )
        return len(requested)

    @staticmethod
    def _resolve_prompt(
        config_snapshot: dict[str, Any], arguments: dict[str, Any], template_id: UUID
    ) -> str:
        """Resolve ``system_prompt`` via the restricted formatter. Every failure
        mode (missing variable, positional field, blocked traversal, malformed
        spec) becomes a sanitized 422 — never a 500 with a stack trace."""
        # P-12 (fix-batch 2026-08-31) — ``dict.get(key, default)`` only
        # applies ``default`` when the KEY is absent. A template stored with
        # an EXPLICIT ``"system_prompt": null`` still returns ``None`` here,
        # and ``str(None)`` would send the literal word "None" to the LLM as
        # its system prompt. ``or ""`` normalizes both "absent" and
        # "explicitly null" to the empty string.
        system_prompt_template: str = str(config_snapshot.get("system_prompt") or "")
        try:
            # H-03: fresh instance per call, see _SafeFormatter docstring.
            return _SafeFormatter().vformat(system_prompt_template, (), dict(arguments))
        except KeyError as exc:
            raise ValidationError(
                detail=(
                    f"system_prompt references variable {exc.args[0]!r} not present in arguments"
                ),
                context={
                    "missing_variable": str(exc.args[0]),
                    "template_id": str(template_id),
                },
            ) from exc
        except (IndexError, ValueError, AttributeError, TypeError) as exc:
            raise ValidationError(
                detail="system_prompt could not be resolved from the provided arguments",
                # No exploitable detail (no template content, no exception
                # message) — the template may embed server-side context.
                context={"template_id": str(template_id)},
            ) from exc

    async def _complete(
        self,
        *,
        config_snapshot: dict[str, Any],
        prompt_resolved: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
        template_id: UUID,
    ) -> _LLMOutcome:
        """Call ``LLMRouter.complete`` and capture the outcome. On any provider
        failure (H-02 fix-batch 2026-09-02: not just ``LLMError``, see the
        ``except Exception`` clause below) the outcome carries
        ``status="llm_error"`` (zeroed usage, requested model, no provider)
        plus the :class:`DependencyError` for the caller to re-raise after
        the single audit publish — so the failure is always audited exactly
        once (Sprint 1: no formal tool_use — D80 Story 4.x). On cancellation
        the outcome carries ``status="cancelled"`` with ``None`` usage
        (IG-01 / H-04, genuinely unknown, not zero)."""
        llm_cfg: dict[str, Any] = config_snapshot.get("llm", {}) or {}
        model: str = str(llm_cfg.get("model", DEFAULT_LLM_MODEL))
        # P-10 (fix-batch 2026-08-31) — a template's ``config.llm`` blob is
        # free-form JSONB, not request-validated. A stored non-numeric value
        # (e.g. ``max_tokens: "unbounded"``) must surface as a sanitized 422,
        # not an uncaught ValueError/TypeError → 500.
        try:
            max_tokens = int(llm_cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                detail="agent template config.llm.max_tokens is not a valid integer",
                context={"template_id": str(template_id), "field": "config.llm.max_tokens"},
            ) from exc
        try:
            temperature = float(llm_cfg.get("temperature", DEFAULT_TEMPERATURE))
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                detail="agent template config.llm.temperature is not a valid number",
                context={"template_id": str(template_id), "field": "config.llm.temperature"},
            ) from exc
        # P-25 (fix-batch 2026-08-31, reviewed — kept as-is) : an
        # out-of-provider-range ``temperature`` (e.g. 5.0) is not clamped
        # here. Unlike ``max_tokens`` (a pure cost/resource ceiling, safe to
        # silently cap), temperature changes the LLM's actual sampling
        # behavior — silently altering it would hide a real template
        # authoring mistake instead of surfacing one. The provider SDK
        # already rejects it (400 → ``LLMError`` → ``DependencyError`` 503
        # below), which is the correct failure mode : visible, not silent.
        # P-19 (fix-batch 2026-08-31) — an unbounded template-level
        # max_tokens is a cost-DoS vector : every Playground run pays for it.
        # Story 9.4 (FinOps budget caps) will own this properly ; this is a
        # Sprint-1 safety net, not a replacement.
        if max_tokens > MAX_TOKENS_HARD_CAP:
            _log.warning(
                "playground_max_tokens_clamped",
                template_id=str(template_id),
                requested=max_tokens,
                cap=MAX_TOKENS_HARD_CAP,
            )
            max_tokens = MAX_TOKENS_HARD_CAP

        # P-09 (fix-batch 2026-08-31, reviewed not patched) — ``arguments``
        # is ``RunPlaygroundRequest.arguments: dict[str, Any]``, populated by
        # Pydantic from a JSON request body. JSON has no Decimal/datetime/set
        # literal, so every value reachable here already round-trips through
        # ``json.dumps`` — wrapping this in a try/except would guard against
        # an input shape that cannot occur.
        messages: list[ChatMessage] = [
            ChatMessage(role="user", content=json.dumps(arguments, ensure_ascii=False)),
        ]
        try:
            completion = await self._llm_router.complete(
                messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=prompt_resolved,
                timeout_s=timeout_seconds,
            )
        except asyncio.CancelledError as exc:
            # P-11 (fix-batch 2026-08-31) — a disconnected client cancels
            # this coroutine while the LLM call is in flight. Without this
            # clause CancelledError propagated straight out of ``run()``,
            # skipping ``_publish_audit_event`` entirely — an attempted (and
            # possibly billed) run left with zero trace. Return an outcome so
            # the single-audit-publish path in ``run()`` still fires, then
            # let the caller re-raise ``exc`` unchanged so task cancellation
            # keeps propagating normally.
            #
            # IG-01 (fix-batch 2026-09-02): status is "cancelled", not
            # "llm_error" : a client disconnect is not a provider failure,
            # and conflating the two made every disconnect look like an
            # Anthropic/OpenAI incident on any dashboard built on this
            # event. NOTE for later : this is still a single coarse bucket.
            # If cancellation volume becomes operationally significant,
            # a future story could distinguish "cancelled before any
            # provider bytes sent" from "cancelled mid-stream". Not needed
            # now.
            return _LLMOutcome(
                status="cancelled",
                raw_output="",
                input_tokens=None,
                output_tokens=None,
                cost_estimate_usd=None,
                model_used=model,
                provider_used="",
                error=exc,
            )
        except Exception as exc:
            # H-02 (fix-batch 2026-09-02): was ``except LLMError``. But
            # ``classify_error()`` (error_classifier.py) buckets
            # ``TypeError``/``ValueError``/``AttributeError``/``KeyError``/
            # ``pydantic.ValidationError`` as "fatal", and ``LLMRouter.
            # complete()`` (router.py) re-raises a "fatal"-classified
            # exception RAW : it is NEVER wrapped in ``LLMError`` for that
            # bucket. A narrower catch here let any of those escape
            # ``_complete()`` uncaught : zero audit event (AC5 broken) and
            # a raw non-RFC-7807 500 instead of the documented 503.
            # Catching the boundary broadly and wrapping uniformly closes
            # this for any exception shape the provider stack can produce,
            # not just today's known list, which would drift out of sync
            # with ``classify_error()`` again the next time it changes.
            error = DependencyError(
                detail=f"LLM provider failure: {type(exc).__name__}",
                context={
                    "template_id": str(template_id),
                    "error_type": type(exc).__name__,
                },
            )
            error.__cause__ = exc
            return _LLMOutcome(
                status="llm_error",
                raw_output="",
                input_tokens=0,
                output_tokens=0,
                cost_estimate_usd=None,
                model_used=model,
                provider_used="",
                error=error,
            )
        return _LLMOutcome(
            status="success",
            raw_output=completion.text,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_estimate_usd=completion.cost_estimate_usd,
            model_used=completion.model,
            provider_used=completion.provider,
            error=None,
        )

    async def _publish_audit_event(
        self,
        *,
        template_id: UUID,
        tools_activated: int,
        duration_ms_total: int,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_estimate_usd: Any,
        model_used: str,
        provider_used: str,
        status: Literal["success", "llm_error", "tool_error", "cancelled"],
        tenant_id: UUID | None,
    ) -> None:
        """Publish ``playground.run.completed`` (AC5).

        Failures are logged at warning level — they MUST NOT shadow the
        original run result or the original LLM exception (P-08 Story 2.6
        alignment).

        P-16 (fix-batch 2026-08-31, reviewed — kept as-is) : the original
        code-review flagged reusing ``template_repo.with_tenant()`` here
        instead of a dedicated ``session_factory``, reading the spec's T4.3
        note as an architectural rule. It is not one : ``ToolHubService``
        does the exact same thing for its own audit-only transaction
        (``features/tool_hub/service.py`` — the ``session_factory_owner``
        parameter passed to ``with_tenant`` is itself an unrelated repo).
        This IS the established repo-holds-the-session-factory convention
        project-wide ; adding a second wiring path here would diverge from
        it, not align with it. No agent_instances/memory_chunks writes
        happen in this transaction — AC2 isolation is about WHAT gets
        written, not which repo's context manager opens the connection.
        """
        try:
            async with self._template_repo.with_tenant(tenant_id) as session:
                event = PlaygroundRunCompletedEvent(
                    template_id=template_id,
                    tools_activated=tools_activated,
                    duration_ms_total=duration_ms_total,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_estimate_usd=cost_estimate_usd,
                    model_used=model_used,
                    provider_used=provider_used,
                    status=status,
                    actor="system",
                    tenant_id=tenant_id,
                )
                # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
                event_id = await publish(
                    PlaygroundRunCompletedEvent.event_type, event, session=session
                )
        except Exception as audit_exc:
            _log.warning(
                "playground_audit_publish_failed",
                template_id=str(template_id),
                status=status,
                error_type=type(audit_exc).__name__,
                error_message=str(audit_exc)[:200],
            )
            return
        await notify_best_effort(event_id, PlaygroundRunCompletedEvent.event_type)


__all__ = ["PlaygroundService"]
