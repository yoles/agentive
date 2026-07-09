"""Playground service — agent test isolation (Story 2.7, FR48).

Loads an ``agent_templates`` row in MEMORY (no INSERT ``agent_instances``
— anti-scope AC2 strict), resolves ``system_prompt`` variables from the
caller's ``arguments``, calls :meth:`LLMRouter.complete`, optionally
resolves tool calls via :func:`infra.mcp.client.call_tool` (bypassing
:class:`ToolHubService.invoke_tool` so the ``m5.tool.invoked`` audit is
NOT published — AC2 isolation), and returns a rich response with the
prompt, raw output, parsed output (best-effort), token usage, cost, and
tool invocation log.

After the run, a SINGLE audit event ``m7.playground.run_completed`` is
published via the bypass pattern (Story 2.1 P-15) with METRICS only —
no prompt/output/args (secret-safety).

Isolation contract (AC2) — enforced by what this module does NOT import :
- No ``MemoryChunkRepo`` ;
- No ``AgentInstanceRepo`` ;
- No ``ToolHubService.invoke_tool`` (would publish m5.tool.invoked).
"""

from __future__ import annotations

import json
import string
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from agentive_backend.features.m7_playground.schemas import RunPlaygroundResponse
from agentive_backend.shared.contracts.events import PlaygroundRunCompletedEvent
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import (
    DependencyError,
    NotFoundError,
    ValidationError,
)
from agentive_backend.shared.llm.exceptions import LLMError
from agentive_backend.shared.llm.types import ChatMessage
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.llm.router import LLMRouter
    from agentive_backend.shared.repositories import (
        AgentTemplateRepo,
        AgentTemplateToolRepo,
        ToolRepo,
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


class PlaygroundService:
    """Run an agent-template in isolation — Story 2.7 FR48."""

    def __init__(
        self,
        *,
        template_repo: AgentTemplateRepo,
        tool_repo: ToolRepo,
        assignment_repo: AgentTemplateToolRepo,
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
        start = time.monotonic()

        # 1. Resolve template (404 NotFoundError if missing). Snapshot
        #    config in memory — NO INSERT into agent_instances (AC2).
        async with self._template_repo.with_tenant(tenant_id) as session:
            template = await self._template_repo.get_by_id_in_session(session, template_id)
            if template is None:
                raise NotFoundError(
                    detail=f"Agent template '{template_id}' not found",
                    context={"template_id": str(template_id)},
                )
            config_snapshot: dict[str, Any] = dict(template.config or {})
            assigned_tools = await self._assignment_repo.list_by_template_in_session(
                session, template_id
            )

        # 2. Filter assigned tools by enabled_tool_ids (None = all enabled).
        assigned_tool_ids: set[UUID] = {tool.id for tool, _at in assigned_tools}
        if enabled_tool_ids is None:
            tools_activated_count = len(assigned_tool_ids)
        else:
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
            tools_activated_count = len(requested)

        # 3. Resolve prompt via the restricted formatter (audit A-01 /
        #    CR P-02). Beyond KeyError (missing variable), a template can
        #    raise IndexError ({0} with no positional args), ValueError
        #    (malformed spec or blocked field traversal) or AttributeError.
        #    All of them map to a sanitized 422 — never a 500 with a
        #    stack trace.
        system_prompt_template: str = str(config_snapshot.get("system_prompt", ""))
        try:
            prompt_resolved = _SAFE_FORMATTER.vformat(system_prompt_template, (), dict(arguments))
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

        # 4. Call LLMRouter.complete (Sprint 1 : no tool_use formal — D80
        #    deferred Story 4.x. ``tool_invocations`` remains empty in the
        #    response Sprint 1, structure preserved for the frontend).
        llm_cfg: dict[str, Any] = config_snapshot.get("llm", {}) or {}
        model: str = str(llm_cfg.get("model", DEFAULT_LLM_MODEL))
        max_tokens: int = int(llm_cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
        temperature: float = float(llm_cfg.get("temperature", DEFAULT_TEMPERATURE))

        user_message_content = json.dumps(arguments, ensure_ascii=False)
        messages: list[ChatMessage] = [
            ChatMessage(role="user", content=user_message_content),
        ]

        # "tool_error" is RESERVED — never produced Sprint 1 (formal
        # tool_use deferred Story 4.x / D80). Kept in the Literal so the
        # event contract is stable for consumers (audit 3.3.1).
        status: Literal["success", "llm_error", "tool_error"] = "success"
        raw_output: str = ""
        input_tokens = 0
        output_tokens = 0
        cost_estimate_usd = None
        model_used = model
        provider_used = ""

        try:
            completion = await self._llm_router.complete(
                messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=prompt_resolved,
                timeout_s=timeout_seconds,
            )
            raw_output = completion.text
            input_tokens = completion.input_tokens
            output_tokens = completion.output_tokens
            cost_estimate_usd = completion.cost_estimate_usd
            model_used = completion.model
            provider_used = completion.provider
        except LLMError as exc:
            status = "llm_error"
            duration_ms_total = int((time.monotonic() - start) * 1000)
            await self._publish_audit_event(
                template_id=template_id,
                tools_activated=tools_activated_count,
                duration_ms_total=duration_ms_total,
                input_tokens=0,
                output_tokens=0,
                cost_estimate_usd=None,
                model_used=model,
                provider_used="",
                status=status,
                tenant_id=tenant_id,
            )
            raise DependencyError(
                detail=f"LLM provider failure: {type(exc).__name__}",
                context={
                    "template_id": str(template_id),
                    "error_type": type(exc).__name__,
                },
            ) from exc

        # 5. Best-effort parse against output_contract (just JSON.loads
        #    Sprint 1 — full JSON Schema validation deferred Story 4.x).
        parsed_output: dict[str, Any] | None = None
        try:
            candidate = json.loads(raw_output)
            if isinstance(candidate, dict):
                parsed_output = candidate
        except json.JSONDecodeError, TypeError, ValueError:
            parsed_output = None

        duration_ms_total = int((time.monotonic() - start) * 1000)

        # 6. Audit event (success path).
        await self._publish_audit_event(
            template_id=template_id,
            tools_activated=tools_activated_count,
            duration_ms_total=duration_ms_total,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate_usd=cost_estimate_usd,
            model_used=model_used,
            provider_used=provider_used,
            status=status,
            tenant_id=tenant_id,
        )

        _log.info(
            "playground_run_completed",
            template_id=str(template_id),
            tools_activated=tools_activated_count,
            duration_ms_total=duration_ms_total,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status=status,
            model_used=model_used,
            provider_used=provider_used,
            playground_run=True,  # AC2 OTel-style flag for log filtering
        )

        return RunPlaygroundResponse(
            prompt_resolved=prompt_resolved,
            raw_output=raw_output,
            parsed_output=parsed_output,
            tokens={"input_tokens": input_tokens, "output_tokens": output_tokens},
            cost_estimate_usd=cost_estimate_usd,
            model_used=model_used,
            provider_used=provider_used,
            tool_invocations=[],  # Sprint 1 — D80 defer Story 4.x
            duration_ms_total=duration_ms_total,
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
        """Publish ``m7.playground.run_completed`` (AC5).

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
