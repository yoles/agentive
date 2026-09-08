"""Unit tests for ``PlaygroundService`` — Story 2.7 T4.6.

Pattern : AsyncMock repos + monkeypatch ``llm_router.complete`` to
control LLM behavior + assert audit event publish count == 1 (no other
events published — AC2 isolation).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.features.playground import service as svc_module
from agentive_backend.features.playground.service import PlaygroundService
from agentive_backend.shared.contracts.memory import MemorizedChunkView
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
    push_memory_provider: Any | None = None,
) -> tuple[PlaygroundService, MagicMock]:
    """Build a PlaygroundService with all-AsyncMock repos + a fake
    LLMRouter that returns ``completion`` (or raises ``completion_raises``).

    ``push_memory_provider`` (Story 3.5 T12.5) defaults to ``None`` — every
    test written before this story keeps constructing (and exercising) a
    Playground with Push Memory unwired, by construction.

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

    llm_router = MagicMock()
    if completion_raises is not None:
        llm_router.complete = AsyncMock(side_effect=completion_raises)
    else:
        llm_router.complete = AsyncMock(return_value=completion)

    service = PlaygroundService(
        template_repo=template_repo,
        assignment_repo=assignment_repo,
        llm_router=llm_router,
        push_memory_provider=push_memory_provider,
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


def _memorized_chunk(*, score: float, content: str = "remembered fact") -> MemorizedChunkView:
    return MemorizedChunkView(chunk_id=uuid4(), namespace="ns", content=content, score=score)


def _push_memory_provider(
    *, chunks: list[MemorizedChunkView] | None = None, raises: Exception | None = None
) -> MagicMock:
    provider = MagicMock()
    if raises is not None:
        provider.relevant_chunks = AsyncMock(side_effect=raises)
    else:
        provider.relevant_chunks = AsyncMock(return_value=chunks or [])
    return provider


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
        "llm_model": "claude-sonnet-4-6",
        "llm_params": {"max_tokens": 100, "temperature": 0.5},
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
    assert result.tokens.input_tokens == 10
    assert result.tokens.output_tokens == 20
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
    template.config = {"system_prompt": "do {x}", "llm_model": "m"}

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
async def test_run_raw_non_llmerror_exception_translates_to_dependency_error_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-02 (fix-batch 2026-09-02) — ``error_classifier.classify_error()``
    buckets ``TypeError``/``ValueError``/``AttributeError``/``KeyError``/
    ``pydantic.ValidationError`` as "fatal", and ``LLMRouter.complete()``
    re-raises a "fatal"-classified exception RAW (never wrapped in
    ``LLMError``). A narrower ``except LLMError`` in ``_complete`` let any
    of these escape uncaught : zero audit event (AC5 broken) and a raw
    non-RFC-7807 500. This asserts a raw ``TypeError`` from the router is
    still converted to ``DependencyError`` 503 + audited exactly once,
    exactly like a typed ``LLMError`` would be."""
    template = MagicMock()
    template.config = {"system_prompt": "do {x}", "llm_model": "m"}

    # A raw, un-wrapped TypeError — simulates classify_error()'s "fatal,
    # not LLMError" bucket (e.g. a malformed SDK call re-raised as-is).
    service, _ = _make_service(
        template=template, completion_raises=TypeError("unexpected keyword argument")
    )

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


@pytest.mark.asyncio
async def test_run_prompt_amplification_via_format_spec_raises_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-03 (fix-batch 2026-09-02) — a template whose ``system_prompt``
    format spec requests an absurd width (``{a:>999999999}``) must be
    rejected with a sanitized 422 BEFORE the ~1 GB allocation that
    ``format()`` would otherwise perform from a 15-byte spec — reachable
    without any LLM call, hence without a valid provider API key."""
    template = MagicMock()
    template.config = {"system_prompt": "{a:>999999999}"}

    service, llm_router = _make_service(template=template)

    with pytest.raises(ValidationError):
        await service.run(
            template_id=uuid4(),
            arguments={"a": "x"},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    llm_router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_run_prompt_amplification_via_field_repetition_raises_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-03 (fix-batch 2026-09-02) — repeating a field enough times
    (``"{a}" * 500`` against a large ``a``) must also be rejected, not
    just a single oversized format spec — the cumulative resolved prompt
    length is bounded, not just each individual substitution."""
    template = MagicMock()
    template.config = {"system_prompt": "{a}" * 500}

    service, llm_router = _make_service(template=template)

    with pytest.raises(ValidationError):
        await service.run(
            template_id=uuid4(),
            arguments={"a": "x" * 200},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    llm_router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_run_marks_otel_span_playground_run_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-01 (fix-batch 2026-09-02) — AC2 requires ``playground.run=true``
    on the current OTel span for every run. Previously declared "Fixed" in
    the story with zero corresponding code (``opentelemetry`` was not even
    importable in this environment) ; this test would have caught that."""
    template = MagicMock()
    template.config = {"system_prompt": "x"}

    service, _ = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    fake_trace = MagicMock()
    monkeypatch.setattr(svc_module, "trace", fake_trace)

    await service.run(
        template_id=uuid4(),
        arguments={},
        enabled_tool_ids=None,
        timeout_seconds=5.0,
    )
    fake_trace.get_current_span.return_value.set_attribute.assert_called_once_with(
        "playground.run", True
    )


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
    # P-24 (fix-batch 2026-08-31) — tool_repo was injected but never used.
    assert "tool_repo" not in param_names


@pytest.mark.asyncio
async def test_run_invalid_max_tokens_raises_422_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-10 — a template storing a non-numeric ``config.llm_params.max_tokens``
    (e.g. ``"unbounded"``) must surface as ValidationError 422, not an
    uncaught ValueError → 500. No LLM call, no audit event."""
    template = MagicMock()
    template.config = {"system_prompt": "x", "llm_params": {"max_tokens": "unbounded"}}

    service, llm_router = _make_service(template=template, completion=_completion())

    publish_calls: list[Any] = []

    async def _spy_publish(*a: Any, **k: Any) -> Any:
        publish_calls.append(a)
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)

    with pytest.raises(ValidationError) as exc_info:
        await service.run(
            template_id=uuid4(),
            arguments={},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    assert "max_tokens" in str(exc_info.value.detail)
    assert publish_calls == []
    llm_router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_run_invalid_temperature_raises_422_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-10 — same guard for ``config.llm_params.temperature``."""
    template = MagicMock()
    template.config = {"system_prompt": "x", "llm_params": {"temperature": "hot"}}

    service, llm_router = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock())

    with pytest.raises(ValidationError) as exc_info:
        await service.run(
            template_id=uuid4(),
            arguments={},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    assert "temperature" in str(exc_info.value.detail)
    llm_router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_run_max_tokens_clamped_to_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-19 — a template requesting an unbounded max_tokens is clamped to
    ``MAX_TOKENS_HARD_CAP`` before the LLM call, not rejected outright
    (the run still proceeds — cost-DoS guard, not a validation error)."""
    template = MagicMock()
    template.config = {"system_prompt": "x", "llm_params": {"max_tokens": 200_000}}

    service, llm_router = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    await service.run(
        template_id=uuid4(),
        arguments={},
        enabled_tool_ids=None,
        timeout_seconds=5.0,
    )
    _, kwargs = llm_router.complete.call_args
    assert kwargs["max_tokens"] == svc_module.MAX_TOKENS_HARD_CAP


@pytest.mark.asyncio
async def test_run_system_prompt_explicit_null_becomes_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-12 — ``config.system_prompt: null`` (explicit, key present) must
    resolve to an empty string, never the literal text "None" sent to the
    LLM as its system prompt."""
    template = MagicMock()
    template.config = {"system_prompt": None}

    service, llm_router = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(),
        arguments={},
        enabled_tool_ids=None,
        timeout_seconds=5.0,
    )
    assert result.prompt_resolved == ""
    _, kwargs = llm_router.complete.call_args
    assert kwargs["system"] == ""


@pytest.mark.asyncio
async def test_run_cancelled_during_llm_call_audits_then_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H-01/IG-01 (fix-batch 2026-09-02) — a REAL asyncio.Task cancellation
    (not a mocked ``side_effect``) mid-LLM-call must still publish exactly
    1 audit event with status="cancelled" and None token counts, even if
    cancellation is re-delivered at the audit publish's very first
    checkpoint — the behavior of a LEVEL-TRIGGERED cancel scope (e.g.
    anyio's, used internally by Starlette's ``BaseHTTPMiddleware``), unlike
    a bare ``asyncio.Task.cancel()`` which only fires once. Without
    ``asyncio.shield()`` around the publish call in ``run()``, this second
    cancellation would abort the audit transaction before ``publish()`` is
    ever reached — 0 events instead of exactly 1 (AC5). The previous
    version of this test injected ``CancelledError`` via a mock
    ``side_effect``, which never actually cancelled the task and could not
    have caught this."""
    template = MagicMock()
    template.config = {"system_prompt": "x", "llm_model": "m"}

    llm_call_started = asyncio.Event()

    async def _hang_until_cancelled(*_a: Any, **_k: Any) -> Any:
        llm_call_started.set()
        await asyncio.Event().wait()  # never set — only cancellation ends this

    service, llm_router = _make_service(template=template)
    llm_router.complete = AsyncMock(side_effect=_hang_until_cancelled)

    publish_calls: list[Any] = []
    task_ref: dict[str, asyncio.Task[Any]] = {}
    aenter_calls = {"n": 0}
    real_aenter = service._template_repo.with_tenant.return_value.__aenter__

    async def _aenter_recancel_on_audit_publish() -> Any:
        # Call #1 = _load_config_and_tools' own with_tenant() (before the
        # LLM call even starts) — must behave normally. Call #2 = the
        # AUDIT publish's with_tenant() — this is where we simulate the
        # level-triggered re-cancellation.
        aenter_calls["n"] += 1
        if aenter_calls["n"] == 2:
            task_ref["task"].cancel()
            await asyncio.sleep(0)  # yield so the re-cancellation is delivered
        return await real_aenter()

    service._template_repo.with_tenant.return_value.__aenter__ = AsyncMock(
        side_effect=_aenter_recancel_on_audit_publish
    )

    async def _spy_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        publish_calls.append((event_type, event))
        return uuid4()

    background_done = asyncio.Event()

    async def _spy_notify(*_a: Any, **_k: Any) -> None:
        background_done.set()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)
    monkeypatch.setattr(svc_module, "notify_best_effort", _spy_notify)

    task = asyncio.create_task(
        service.run(
            template_id=uuid4(),
            arguments={"x": "y"},
            enabled_tool_ids=None,
            timeout_seconds=5.0,
        )
    )
    task_ref["task"] = task
    await llm_call_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The shielded publish keeps running in the background even though
    # run()'s own await was re-cancelled — wait for it deterministically
    # rather than sleep-looping (a broken shield leaves this hanging until
    # the timeout, which fails the test with a clear signal instead of
    # a silent false pass).
    await asyncio.wait_for(background_done.wait(), timeout=1.0)

    assert len(publish_calls) == 1
    assert publish_calls[0][1].status == "cancelled"
    assert publish_calls[0][1].input_tokens is None
    assert publish_calls[0][1].output_tokens is None


@pytest.mark.asyncio
async def test_run_empty_raw_output_yields_parsed_output_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-30 — an empty ``raw_output`` (LLM returned an empty string) parses
    to ``parsed_output=None`` via the same best-effort path as any other
    non-JSON text, not a crash on ``json.loads("")``."""
    template = MagicMock()
    template.config = {"system_prompt": "x"}

    service, _ = _make_service(template=template, completion=_completion(text=""))
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(),
        arguments={},
        enabled_tool_ids=None,
        timeout_seconds=5.0,
    )
    assert result.raw_output == ""
    assert result.parsed_output is None


@pytest.mark.asyncio
async def test_run_audit_publish_failure_does_not_break_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-36 — if the audit publish itself raises (poisoned session, DB
    hiccup), the Playground run still returns its result to the caller ;
    the failure is logged, not propagated (P-08 Story 2.6 alignment)."""
    template = MagicMock()
    template.config = {"system_prompt": "You produce {topic}"}

    completion = _completion(text='{"summary": "hi"}')
    service, _ = _make_service(template=template, completion=completion)

    async def _boom_publish(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(svc_module, "publish", _boom_publish)
    notify_mock = AsyncMock()
    monkeypatch.setattr(svc_module, "notify_best_effort", notify_mock)

    result = await service.run(
        template_id=uuid4(),
        arguments={"topic": "tests"},
        enabled_tool_ids=None,
        timeout_seconds=10.0,
    )
    assert result.raw_output == '{"summary": "hi"}'
    # Audit publish failed before an event_id existed — notify must not be
    # reached (would otherwise crash on an undefined event_id).
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_publishes_before_notifying(monkeypatch: pytest.MonkeyPatch) -> None:
    """P-20 — the outbox INSERT (inside ``with_tenant``, committed on
    ``__aexit__``) must complete before ``notify_best_effort`` fires, so a
    NOTIFY never races ahead of the row it announces."""
    template = MagicMock()
    template.config = {"system_prompt": "x"}

    service, _ = _make_service(template=template, completion=_completion())

    call_order: list[str] = []

    async def _spy_publish(*_a: Any, **_k: Any) -> Any:
        call_order.append("publish")
        return uuid4()

    async def _spy_notify(*_a: Any, **_k: Any) -> None:
        call_order.append("notify")

    monkeypatch.setattr(svc_module, "publish", _spy_publish)
    monkeypatch.setattr(svc_module, "notify_best_effort", _spy_notify)

    await service.run(
        template_id=uuid4(),
        arguments={},
        enabled_tool_ids=None,
        timeout_seconds=5.0,
    )
    assert call_order == ["publish", "notify"]


# ─── Push Memory (Story 3.5, AC1/AC2/AC3, FR20) ───────────────────


@pytest.mark.asyncio
async def test_push_memory_provider_none_is_non_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 last point — an environment where Push Memory isn't wired
    (``push_memory_provider=None``, the default) runs exactly like before
    this story : no injection attempted, ``push_memory: null`` in the
    response, even when the template DOES configure a namespace."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {
        "system_prompt": "do {x}",
        "push_memory": {"namespace": "team-alpha"},
    }

    service, llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=None
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={"x": "y"}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert result.push_memory is None
    assert result.prompt_resolved == "do y"
    _, kwargs = llm_router.complete.call_args
    assert kwargs["system"] == "do y"


@pytest.mark.asyncio
async def test_push_memory_controleur_without_optin_never_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 — a `controleur` template does NOT activate Push Memory by
    default, even with a `namespace` configured."""
    template = MagicMock()
    template.archetype = "controleur"
    template.config = {"system_prompt": "x", "push_memory": {"namespace": "team-alpha"}}

    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9)])
    service, llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    provider.relevant_chunks.assert_not_called()
    assert result.push_memory is None
    _, kwargs = llm_router.complete.call_args
    assert kwargs["system"] == "x"


@pytest.mark.asyncio
async def test_push_memory_controleur_with_optin_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 — `config.push_memory.optin=true` forces activation for a
    `controleur` template."""
    template = MagicMock()
    template.archetype = "controleur"
    template.config = {
        "system_prompt": "x",
        "push_memory": {"namespace": "team-alpha", "optin": True},
    }

    provider = _push_memory_provider(chunks=[])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    provider.relevant_chunks.assert_awaited_once()
    assert result.push_memory is not None
    assert result.push_memory.chunks_injected == 0


@pytest.mark.asyncio
async def test_push_memory_non_controleur_without_namespace_never_calls_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-`controleur` archetype with NO `namespace` configured never
    attempts a lookup — ``push_memory: null``."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {"system_prompt": "x"}

    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9)])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    provider.relevant_chunks.assert_not_called()
    assert result.push_memory is None


@pytest.mark.asyncio
async def test_push_memory_chunks_above_threshold_injected_into_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 — chunks returned by the provider are injected in front of the
    resolved prompt, marked with ``<contexte_memorise source="...">``, and
    the ENRICHED prompt (not the original) is what reaches the LLM call."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {
        "system_prompt": "do {x}",
        "push_memory": {"namespace": "team-alpha"},
        "llm_params": {"max_tokens": 4000},
    }

    chunk = _memorized_chunk(score=0.9, content="remembered fact")
    provider = _push_memory_provider(chunks=[chunk])
    service, llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={"x": "y"}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    # P10 (revue 3.5) : the fixture chunk carries `namespace="ns"` while the
    # template asked for `"team-alpha"`, so this assertion is what pins the
    # marker to the chunk's OWN namespace. Reading the requested one instead
    # would produce a `source` the chunk does not come from.
    expected_block = (
        f'<contexte_memorise source="{chunk.namespace}/{chunk.chunk_id}">'
        "remembered fact</contexte_memorise>"
    )
    assert expected_block in result.prompt_resolved
    assert result.prompt_resolved.endswith("do y")
    _, kwargs = llm_router.complete.call_args
    assert kwargs["system"] == result.prompt_resolved
    assert result.push_memory is not None
    assert result.push_memory.chunks_injected == 1
    provider.relevant_chunks.assert_awaited_once()
    call_kwargs = provider.relevant_chunks.await_args.kwargs
    assert call_kwargs["namespace"] == "team-alpha"
    assert call_kwargs["similarity_threshold"] == svc_module.DEFAULT_SIMILARITY_THRESHOLD
    assert call_kwargs["top_k"] == svc_module.PUSH_MEMORY_CANDIDATE_TOP_K


@pytest.mark.asyncio
async def test_push_memory_budget_exceeded_truncates_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 — a chunk that alone exceeds the 15% budget is NEVER included
    partially ; packing stops at the first chunk that would overflow,
    preserving score order rather than skipping ahead to a smaller one."""
    template = MagicMock()
    template.archetype = "producteur"
    # max_tokens=400 -> budget_chars = int(400 * 0.15) * 4 = 240 chars.
    # A wrapped block (namespace + a UUID + tags + the trailing separator)
    # costs ~95 chars for "short" and ~94 for "tail", so the two SHORT chunks
    # would fit together (189 <= 240) while the huge one (~590) never does.
    # That gap is what makes the assertion below discriminating: at a tighter
    # budget the trailing chunk would be excluded for lack of room rather
    # than by the stop-at-first-overflow rule this test is about.
    template.config = {
        "system_prompt": "x",
        "push_memory": {"namespace": "ns"},
        "llm_params": {"max_tokens": 400},
    }

    small_chunk = _memorized_chunk(score=0.95, content="short")
    huge_chunk = _memorized_chunk(score=0.9, content="y" * 500)
    # P12 (revue 3.5) : a THIRD chunk, short enough to fit in the remaining
    # budget, placed after the oversized one. Without it the test could not
    # tell "stop at the first overflow" from "skip ahead to a smaller one":
    # both behaviors produced exactly the same one-chunk result.
    trailing_chunk = _memorized_chunk(score=0.88, content="tail")
    provider = _push_memory_provider(chunks=[small_chunk, huge_chunk, trailing_chunk])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert result.push_memory is not None
    assert result.push_memory.chunks_injected == 1
    assert "short" in result.prompt_resolved
    assert "y" * 500 not in result.prompt_resolved
    assert "tail" not in result.prompt_resolved, "packing must stop, not skip ahead"


@pytest.mark.asyncio
async def test_push_memory_provider_failure_does_not_break_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 last point — a Push Memory provider failure never fails the run.
    Response carries ``push_memory: {chunks_injected: 0, tokens_used: 0}``
    (attempted, found nothing) — NOT ``null`` (never attempted)."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {"system_prompt": "x", "push_memory": {"namespace": "ns"}}

    provider = _push_memory_provider(raises=RuntimeError("boom"))
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    publish_calls: list[Any] = []

    async def _spy_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        publish_calls.append((event_type, event))
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert result.push_memory is not None
    assert result.push_memory.chunks_injected == 0
    assert result.push_memory.tokens_used == 0
    # Exactly one audit event still published (no regression on AC5 2.7).
    assert len(publish_calls) == 1
    assert publish_calls[0][1].status == "success"


@pytest.mark.asyncio
async def test_push_memory_audit_event_carries_usage_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 — the audit event's two additive fields carry the real usage."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {
        "system_prompt": "x",
        "push_memory": {"namespace": "ns"},
        "llm_params": {"max_tokens": 4000},
    }

    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9, content="fact")])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    publish_calls: list[Any] = []

    async def _spy_publish(event_type: str, event: Any, **_kw: Any) -> Any:
        publish_calls.append((event_type, event))
        return uuid4()

    monkeypatch.setattr(svc_module, "publish", _spy_publish)
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert len(publish_calls) == 1
    event = publish_calls[0][1]
    assert event.push_memory_chunks_injected == result.push_memory.chunks_injected
    assert event.push_memory_tokens_used == result.push_memory.tokens_used
    assert event.push_memory_chunks_injected == 1


@pytest.mark.asyncio
async def test_llm_settings_read_from_canonical_config_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BS1 (revue Story 3.5) : the three LLM settings are read from the keys
    that ``AgentConfig.to_mapping()`` actually writes (``llm_model`` at the
    top level, ``temperature`` / ``max_tokens`` under ``llm_params``), NOT
    from the phantom ``config.llm`` mapping no write path ever produces."""
    template = MagicMock()
    template.config = {
        "system_prompt": "x",
        "llm_model": "gpt-4o",
        "llm_params": {"temperature": 0.1, "max_tokens": 12_345},
    }

    service, llm_router = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    await service.run(template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0)
    _, kwargs = llm_router.complete.call_args
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["temperature"] == 0.1
    assert kwargs["max_tokens"] == 12_345


@pytest.mark.asyncio
async def test_legacy_llm_key_is_ignored_and_defaults_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BS1 : a hand-seeded row carrying the old ``config.llm`` shape is NOT a
    supported config surface. It must fall back to the named defaults rather
    than silently resurrect the phantom key."""
    template = MagicMock()
    template.config = {
        "system_prompt": "x",
        "llm": {"model": "ghost", "temperature": 0.99, "max_tokens": 999},
    }

    service, llm_router = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    await service.run(template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0)
    _, kwargs = llm_router.complete.call_args
    assert kwargs["model"] == svc_module.DEFAULT_LLM_MODEL
    assert kwargs["temperature"] == svc_module.DEFAULT_TEMPERATURE
    assert kwargs["max_tokens"] == svc_module.DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
async def test_non_mapping_llm_params_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BS1 : ``config`` is free-form JSONB, so ``llm_params`` can hold a
    scalar or a list. ``_llm_params_cfg`` must degrade to the defaults, not
    raise an ``AttributeError`` (raw 500 outside RFC 7807)."""
    template = MagicMock()
    template.config = {"system_prompt": "x", "llm_params": "max_tokens=8000"}

    service, llm_router = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    await service.run(template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0)
    _, kwargs = llm_router.complete.call_args
    assert kwargs["max_tokens"] == svc_module.DEFAULT_MAX_TOKENS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_max_tokens", "expected_injected"),
    [(16_000, 1), (None, 0)],
)
async def test_push_memory_budget_scales_with_configured_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
    config_max_tokens: int | None,
    expected_injected: int,
) -> None:
    """AC1 : the 15% budget really tracks the template's configured
    ``max_tokens``. The exact same ~5 kB chunk fits at 16 000 tokens
    (budget = int(16000 * 0.15) * 4 = 9600 chars) and does not at the
    default 4096 (budget = 2456 chars).

    This is the regression guard for BS1 : while the code read the phantom
    ``config.llm``, both rows of this table injected 0 chunk, because every
    template ran on ``DEFAULT_MAX_TOKENS`` whatever it had configured."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {"system_prompt": "x", "push_memory": {"namespace": "ns"}}
    if config_max_tokens is not None:
        template.config["llm_params"] = {"max_tokens": config_max_tokens}

    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9, content="z" * 5_000)])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert result.push_memory is not None
    assert result.push_memory.chunks_injected == expected_injected


def _counters() -> tuple[float, float]:
    """Current value of the two Push Memory counters (AC3)."""
    return (
        svc_module.PLAYGROUND_PUSH_MEMORY_CHUNKS_INJECTED_TOTAL._value.get(),
        svc_module.PLAYGROUND_PUSH_MEMORY_TOKENS_USED_TOTAL._value.get(),
    )


def _push_memory_template(
    *, namespace: str = "ns", archetype: str = "producteur", **config: Any
) -> MagicMock:
    template = MagicMock()
    template.archetype = archetype
    template.config = {"system_prompt": "x", "push_memory": {"namespace": namespace}, **config}
    return template


@pytest.mark.asyncio
async def test_push_memory_preserves_descending_score_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 point 4 — the injected blocks keep the provider's descending-score
    order. P13 (revue 3.5): the guarantee rested entirely on a comment and on
    `rerank_rows` being correct upstream, with nothing asserting it here."""
    template = _push_memory_template(llm_params={"max_tokens": 16_000})
    chunks = [
        _memorized_chunk(score=0.99, content="first"),
        _memorized_chunk(score=0.95, content="second"),
        _memorized_chunk(score=0.90, content="third"),
    ]
    provider = _push_memory_provider(chunks=chunks)
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    prompt = result.prompt_resolved
    assert prompt.index("first") < prompt.index("second") < prompt.index("third")


@pytest.mark.asyncio
async def test_push_memory_counters_incremented_by_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 / P11 (revue 3.5) : the counters are driven by ``run()`` itself.
    Deleting the two ``.inc()`` calls from the service used to leave the whole
    suite green: nothing but ``test_metrics.py`` named them, and that test
    incremented them by hand."""
    template = _push_memory_template(llm_params={"max_tokens": 16_000})
    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9, content="fact")])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    before_chunks, before_tokens = _counters()
    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    after_chunks, after_tokens = _counters()

    assert result.push_memory is not None
    assert after_chunks - before_chunks == result.push_memory.chunks_injected == 1
    assert after_tokens - before_tokens == result.push_memory.tokens_used > 0


@pytest.mark.asyncio
async def test_push_memory_counters_untouched_when_never_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 — a run that never attempts a lookup must leave both counters
    alone, which is the point of the ``push_memory_usage is not None`` guard
    (T9.8) and was asserted nowhere."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {"system_prompt": "x"}  # no push_memory block at all
    service, _llm_router = _make_service(template=template, completion=_completion())
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    before = _counters()
    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )

    assert result.push_memory is None
    assert _counters() == before


@pytest.mark.asyncio
async def test_push_memory_counters_not_incremented_when_run_fails_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P9 (revue 3.5) : ``_complete`` still raises inline for an invalid
    stored ``temperature`` (422). The counters used to be incremented before
    that, booking chunks injected into a prompt that was never sent, with no
    audit event to correlate against."""
    template = _push_memory_template(llm_params={"max_tokens": 16_000, "temperature": "hot"})
    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9, content="fact")])
    service, llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))

    before = _counters()
    with pytest.raises(ValidationError):
        await service.run(
            template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
        )

    assert _counters() == before
    llm_router.complete.assert_not_called()


@pytest.mark.asyncio
async def test_push_memory_escapes_the_namespace_in_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 (revue 3.5) : the namespace reaches the ``source="..."`` attribute
    from free-form config, and namespace names are only length-constrained.
    An embedded quote used to close the attribute and inject arbitrary
    pseudo-attributes into the marker the model is meant to trust."""
    template = _push_memory_template(llm_params={"max_tokens": 16_000})
    hostile = MemorizedChunkView(
        chunk_id=uuid4(),
        namespace='a" injecte="oui',
        content="payload",
        score=0.9,
    )
    provider = _push_memory_provider(chunks=[hostile])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert 'injecte="oui"' not in result.prompt_resolved
    assert "&quot;" in result.prompt_resolved
    # Exactly one opening marker: the attribute was not broken out of.
    assert result.prompt_resolved.count("<contexte_memorise ") == 1


@pytest.mark.asyncio
async def test_push_memory_accounts_for_the_injected_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P8 (revue 3.5) : ``tokens_used`` must measure exactly the text that is
    injected, blank-line separators included. Summing per block under-counted
    by two characters per chunk and applied the ``max(1, ...)`` floor once per
    block, and this number is the only consumption figure AC3 exposes."""
    template = _push_memory_template(llm_params={"max_tokens": 16_000})
    provider = _push_memory_provider(
        chunks=[
            _memorized_chunk(score=0.95, content="alpha"),
            _memorized_chunk(score=0.90, content="beta"),
        ]
    )
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    injected = result.prompt_resolved.removesuffix("x")
    assert injected.endswith("\n\n")
    assert result.push_memory is not None
    assert result.push_memory.tokens_used == max(1, len(injected) // 4)


@pytest.mark.asyncio
async def test_push_memory_lookup_is_time_boxed(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2 (revue 3.5) : ``timeout_seconds`` only ever reached ``_complete``,
    so a hanging provider (the embedder alone defaults to 30 s, plus two
    unbounded DB round-trips) could blow far past the budget the caller set
    for the whole run. The run still succeeds, with a degraded ``{0, 0}``."""
    template = _push_memory_template(llm_params={"max_tokens": 16_000})

    async def _hang(**_kw: Any) -> Any:
        await asyncio.Event().wait()  # never set

    provider = MagicMock()
    provider.relevant_chunks = AsyncMock(side_effect=_hang)
    service, llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=0.05
    )
    assert result.push_memory is not None
    assert result.push_memory.chunks_injected == 0
    assert result.push_memory.tokens_used == 0
    llm_router.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_push_memory_skipped_when_the_resolved_prompt_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3 (revue 3.5) : ``_resolve_prompt`` returns ``""`` by design when the
    template has no ``system_prompt`` (P-12), and a blank query is rejected by
    the memory layer, so the lookup could only ever 400 and degrade. Nothing
    to search on counts as NOT attempted."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {"push_memory": {"namespace": "ns"}}  # no system_prompt
    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9)])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert result.push_memory is None
    provider.relevant_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_memory_query_is_capped_to_the_search_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3 — a resolved prompt may reach ``_MAX_RESOLVED_PROMPT_CHARS``
    (65_536), twice what the memory search route accepts, so the oversized
    query was rejected downstream and every run degraded to ``{0, 0}``."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {
        "system_prompt": "z" * 40_000,
        "push_memory": {"namespace": "ns"},
    }
    provider = _push_memory_provider(chunks=[])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    await service.run(template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0)
    sent = provider.relevant_chunks.await_args.kwargs["query"]
    assert len(sent) == svc_module.PUSH_MEMORY_MAX_QUERY_CHARS


@pytest.mark.asyncio
async def test_push_memory_config_of_an_unexpected_shape_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P5 (revue 3.5) : ``config`` is free-form JSONB and only the ``PUT``
    route validates it. A seeded scalar used to raise ``AttributeError``
    OUTSIDE the guarded block, i.e. a raw 500 outside RFC 7807, on a path
    whose stated invariant is that it never fails the run."""
    template = MagicMock()
    template.archetype = "producteur"
    template.config = {"system_prompt": "x", "push_memory": "team-alpha"}
    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9)])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert result.push_memory is None
    provider.relevant_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_memory_optin_must_be_a_real_boolean_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P6 (revue 3.5) : ``bool("false")`` is ``True``, so a stored
    ``optin: "false"`` lifted the Contrôleur opt-out, the exact inverse of
    AC2."""
    template = _push_memory_template(archetype="controleur")
    template.config["push_memory"]["optin"] = "false"
    provider = _push_memory_provider(chunks=[_memorized_chunk(score=0.9)])
    service, _llm_router = _make_service(
        template=template, completion=_completion(), push_memory_provider=provider
    )
    monkeypatch.setattr(svc_module, "publish", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(svc_module, "notify_best_effort", AsyncMock())

    result = await service.run(
        template_id=uuid4(), arguments={}, enabled_tool_ids=None, timeout_seconds=5.0
    )
    assert result.push_memory is None
    provider.relevant_chunks.assert_not_awaited()
