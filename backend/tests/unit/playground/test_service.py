"""Unit tests for ``PlaygroundService`` — Story 2.7 T4.6.

Pattern : AsyncMock repos + monkeypatch ``llm_router.complete`` to
control LLM behavior + assert audit event publish count == 1 (no other
events published — AC2 isolation).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.features.playground import service as svc_module
from agentive_backend.features.playground.service import PlaygroundService
from agentive_backend.shared.exceptions import (
    DependencyError,
    NotFoundError,
    ValidationError,
)
from agentive_backend.shared.llm.exceptions import LLMError
from agentive_backend.shared.llm.types import Completion


def _make_service(
    *,
    template: Any | None,
    assigned_tools: list[tuple[Any, Any]] | None = None,
    completion: Completion | None = None,
    completion_raises: Exception | None = None,
) -> tuple[PlaygroundService, MagicMock]:
    """Build a PlaygroundService with all-AsyncMock repos + a fake
    LLMRouter that returns ``completion`` (or raises ``completion_raises``).

    Returns ``(service, llm_router_mock)`` so tests can inspect calls.
    """
    template_repo = MagicMock()
    template_repo.with_tenant = MagicMock()
    template_repo.with_tenant.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    template_repo.with_tenant.return_value.__aexit__ = AsyncMock(return_value=False)
    # A-07 — service resolves the template via the lookup-or-404 helper.
    if template is None:
        template_repo.require_by_id_in_session = AsyncMock(
            side_effect=NotFoundError(detail="Agent template not found", context={})
        )
    else:
        template_repo.require_by_id_in_session = AsyncMock(return_value=template)

    assignment_repo = MagicMock()
    assignment_repo.list_by_template_in_session = AsyncMock(return_value=assigned_tools or [])

    tool_repo = MagicMock()

    llm_router = MagicMock()
    if completion_raises is not None:
        llm_router.complete = AsyncMock(side_effect=completion_raises)
    else:
        llm_router.complete = AsyncMock(return_value=completion)

    service = PlaygroundService(
        template_repo=template_repo,
        tool_repo=tool_repo,
        assignment_repo=assignment_repo,
        llm_router=llm_router,
    )
    return service, llm_router


def _completion(text: str = "ok", **overrides: Any) -> Completion:
    """Build a minimal Completion for tests."""
    return Completion(
        text=text,
        model=overrides.get("model", "claude-sonnet-4-6"),
        provider=overrides.get("provider", "anthropic"),
        input_tokens=overrides.get("input_tokens", 10),
        output_tokens=overrides.get("output_tokens", 20),
        finish_reason=overrides.get("finish_reason", "stop"),
        latency_ms=overrides.get("latency_ms", 100.0),
        cost_estimate_usd=overrides.get("cost_estimate_usd", Decimal("0.001")),
    )


@pytest.mark.asyncio
async def test_run_happy_path_returns_full_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6 (1) — happy path : LLM responds with JSON, service parses it,
    returns full payload + publishes 1 audit event."""
    template_id = uuid4()
    template = MagicMock()
    template.config = {
        "system_prompt": "You produce {topic}",
        "llm": {"model": "claude-sonnet-4-6", "max_tokens": 100, "temperature": 0.5},
    }

    completion = _completion(text='{"summary": "hi"}')
    service, _llm_router = _make_service(template=template, completion=completion)

    publish_calls: list[Any] = []

    async def _spy_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        publish_calls.append((event_type, event))
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=template_id,
        arguments={"topic": "tests"},
        enabled_tool_ids=None,
        timeout_seconds=10.0,
    )
    assert result.prompt_resolved == "You produce tests"
    assert result.raw_output == '{"summary": "hi"}'
    assert result.parsed_output == {"summary": "hi"}
    assert result.tokens == {"input_tokens": 10, "output_tokens": 20}
    assert result.model_used == "claude-sonnet-4-6"
    assert result.provider_used == "anthropic"
    assert result.tool_invocations == []
    assert result.duration_ms_total >= 0
    # AC5 — exactly 1 audit event published.
    assert len(publish_calls) == 1
    assert publish_calls[0][0] == "playground.run.completed"
    assert publish_calls[0][1].status == "success"


@pytest.mark.asyncio
async def test_run_template_not_found_raises_404_without_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6 (2) — template_id ghost → NotFoundError 404. NO audit event
    published (AC6)."""
    service, _ = _make_service(template=None)

    publish_calls: list[Any] = []

    async def _spy_publish(*a: Any, **k: Any) -> Any:
        publish_calls.append((a, k))
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)

    with pytest.raises(NotFoundError):
        await service.run(
            template_id=uuid4(),
            arguments={},
            enabled_tool_ids=None,
            timeout_seconds=10.0,
        )
    assert publish_calls == [], "404 path MUST NOT publish audit event"


@pytest.mark.asyncio
async def test_run_llm_error_translates_to_dependency_error_and_audits_llm_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6 (3) — LLM provider failure → DependencyError 503 + audit event
    with status='llm_error'."""
    template = MagicMock()
    template.config = {"system_prompt": "do {x}", "llm": {"model": "m"}}

    service, _ = _make_service(template=template, completion_raises=LLMError("boom"))

    publish_calls: list[Any] = []

    async def _spy_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        publish_calls.append((event_type, event))
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    with pytest.raises(DependencyError):
        await service.run(
            template_id=uuid4(),
            arguments={"x": "y"},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    assert len(publish_calls) == 1
    assert publish_calls[0][1].status == "llm_error"
    assert publish_calls[0][1].input_tokens == 0
    assert publish_calls[0][1].output_tokens == 0


@pytest.mark.asyncio
async def test_run_missing_variable_raises_422_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6 (4) — system_prompt references {missing_var} not in arguments
    → ValidationError 422 (no LLM call, no audit event)."""
    template = MagicMock()
    template.config = {"system_prompt": "do {missing_var}"}

    service, llm_router = _make_service(template=template, completion=_completion())

    publish_calls: list[Any] = []

    async def _spy_publish(*a: Any, **k: Any) -> Any:
        publish_calls.append(a)
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)

    with pytest.raises(ValidationError) as exc_info:
        await service.run(
            template_id=uuid4(),
            arguments={"other": "x"},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    assert "missing_var" in str(exc_info.value.detail)
    assert publish_calls == []
    llm_router.complete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile_prompt",
    [
        "{0}",  # positional field → IndexError with empty args
        "{}",  # auto-numbered field → IndexError with empty args
        "{x.__class__}",  # attribute traversal → info-disclosure vector
        "{x[0]}",  # index traversal
        "{x:!r}",  # malformed format spec → ValueError
    ],
)
async def test_run_hostile_template_raises_422_not_500(
    monkeypatch: pytest.MonkeyPatch,
    hostile_prompt: str,
) -> None:
    """Audit A-01 (5.5 / CR P-02) — a template with positional fields,
    attribute/index traversal or a malformed format spec MUST surface as a
    sanitized ValidationError 422 (not a 500 with a stack trace), with no
    LLM call and no audit event."""
    template = MagicMock()
    template.config = {"system_prompt": hostile_prompt}

    service, llm_router = _make_service(template=template, completion=_completion())

    publish_calls: list[Any] = []

    async def _spy_publish(*a: Any, **k: Any) -> Any:
        publish_calls.append(a)
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)

    with pytest.raises(ValidationError) as exc_info:
        await service.run(
            template_id=uuid4(),
            arguments={"x": "value"},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    # Sanitized detail — no template content, no raw exception message.
    assert hostile_prompt not in str(exc_info.value.detail)
    assert "__class__" not in str(exc_info.value.detail)
    assert publish_calls == []
    llm_router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_run_format_spec_still_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit A-01 regression guard — plain ``{variable}`` substitution and
    benign format specs keep working through the restricted formatter."""
    template = MagicMock()
    template.config = {"system_prompt": "pad: {topic:>8}."}

    service, _ = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(),
        arguments={"topic": "tests"},
        enabled_tool_ids=None,
        timeout_seconds=5.0,
    )
    assert result.prompt_resolved == "pad:    tests."


@pytest.mark.asyncio
async def test_run_enabled_tool_ids_filter_rejects_unassigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6 (5) — enabled_tool_ids must be a subset of assigned tools.
    Otherwise → ValidationError 422, no audit event."""
    template = MagicMock()
    template.config = {"system_prompt": "x"}
    tool_a_id = uuid4()
    tool_a = MagicMock()
    tool_a.id = tool_a_id

    service, _ = _make_service(
        template=template,
        assigned_tools=[(tool_a, MagicMock())],
        completion=_completion(),
    )

    publish_calls: list[Any] = []

    async def _spy_publish(*a: Any, **k: Any) -> Any:
        publish_calls.append(a)
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)

    ghost_tool_id = uuid4()
    with pytest.raises(ValidationError) as exc_info:
        await service.run(
            template_id=uuid4(),
            arguments={},
            enabled_tool_ids=[ghost_tool_id],
            timeout_seconds=5.0,
        )
    assert "not assigned" in str(exc_info.value.detail).lower()
    assert publish_calls == []


@pytest.mark.asyncio
async def test_run_parsed_output_none_when_raw_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6 (6) — if LLM returns non-JSON text, ``parsed_output=None``
    (best-effort parsing, not a fatal error)."""
    template = MagicMock()
    template.config = {"system_prompt": "x"}

    service, _ = _make_service(template=template, completion=_completion(text="hello world"))

    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(),
        arguments={},
        enabled_tool_ids=None,
        timeout_seconds=5.0,
    )
    assert result.raw_output == "hello world"
    assert result.parsed_output is None


@pytest.mark.asyncio
async def test_run_isolation_no_instance_repo_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T4.6 (7) — AC2 isolation : the service does NOT have any reference
    to AgentInstanceRepo (verified at class level — not present in
    ``__init__`` signature)."""
    import inspect

    sig = inspect.signature(PlaygroundService.__init__)
    param_names = set(sig.parameters.keys())
    # Must NOT reference instance_repo (AC2 strict).
    assert "instance_repo" not in param_names
    # Must NOT reference memory_chunk_repo (AC2 strict).
    assert "memory_chunk_repo" not in param_names
    # Must NOT reference event_bus directly (only via ``publish`` import).
    assert "event_bus" not in param_names
