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

import json
import string
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from agentive_backend.features.playground.schemas import RunPlaygroundResponse
from agentive_backend.shared.contracts.events import PlaygroundRunCompletedEvent
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import (
    DependencyError,
    ValidationError,
)
from agentive_backend.shared.llm.exceptions import LLMError
from agentive_backend.shared.llm.types import ChatMessage
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.llm.router import LLMRouter
    from agentive_backend.shared.repositories.ports import (
        AgentTemplateRepository,
        AgentTemplateToolRepository,
        ToolRepository,
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
DEFAULT_LLM_MODEL: Final = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS: Final = 4096
DEFAULT_TEMPERATURE: Final = 0.7


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
    """

    def get_field(self, field_name: str, args: Sequence[Any], kwargs: Mapping[str, Any]) -> Any:
        if "." in field_name or "[" in field_name:
            raise ValueError(f"unsupported field reference: {field_name!r}")
        return super().get_field(field_name, args, kwargs)


_SAFE_FORMATTER = _SafeFormatter()


def _best_effort_json(raw_output: str) -> dict[str, Any] | None:
    """Parse ``raw_output`` as a JSON object, best-effort (Sprint 1 — full JSON
    Schema validation against the output_contract deferred Story 4.x). Returns
    ``None`` on any parse failure or a non-object payload."""
    try:
        candidate = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return candidate if isinstance(candidate, dict) else None


@dataclass(frozen=True, slots=True)
class _LLMOutcome:
    """Result of the LLM completion step — the value ``run`` audits once then
    either raises (``error``) or turns into the response. Audit A-09: lets the
    success and llm_error paths converge on a single audit call."""

    status: Literal["success", "llm_error"]
    raw_output: str
    input_tokens: int
    output_tokens: int
    cost_estimate_usd: Decimal | None
    model_used: str
    provider_used: str
    error: Exception | None


class PlaygroundService:
    """Run an agent-template in isolation — Story 2.7 FR48."""

    def __init__(
        self,
        *,
        template_repo: AgentTemplateRepository,
        tool_repo: ToolRepository,
        assignment_repo: AgentTemplateToolRepository,
        llm_router: LLMRouter,
    ) -> None:
        self._template_repo = template_repo
        self._tool_repo = tool_repo
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

        # 5. Single audit-publish path (AC5 — exactly one event) for both
        #    the success and llm_error outcomes.
        await self._publish_audit_event(
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
            tokens={
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
            },
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
        system_prompt_template: str = str(config_snapshot.get("system_prompt", ""))
        try:
            return _SAFE_FORMATTER.vformat(system_prompt_template, (), dict(arguments))
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
        """Call ``LLMRouter.complete`` and capture the outcome. On ``LLMError``
        the outcome carries ``status="llm_error"`` (zeroed usage, requested
        model, no provider) plus the :class:`DependencyError` for the caller to
        re-raise after the single audit publish — so the failure is always
        audited exactly once (Sprint 1: no formal tool_use — D80 Story 4.x)."""
        llm_cfg: dict[str, Any] = config_snapshot.get("llm", {}) or {}
        model: str = str(llm_cfg.get("model", DEFAULT_LLM_MODEL))
        max_tokens: int = int(llm_cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
        temperature: float = float(llm_cfg.get("temperature", DEFAULT_TEMPERATURE))

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
        except LLMError as exc:
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
        input_tokens: int,
        output_tokens: int,
        cost_estimate_usd: Any,
        model_used: str,
        provider_used: str,
        status: Literal["success", "llm_error", "tool_error"],
        tenant_id: UUID | None,
    ) -> None:
        """Publish ``playground.run.completed`` (AC5).

        Failures are logged at warning level — they MUST NOT shadow the
        original run result or the original LLM exception (P-08 Story 2.6
        alignment).
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
