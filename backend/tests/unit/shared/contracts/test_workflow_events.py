"""Unit tests — workflow lifecycle events (Story 4.1 T5, Story 4.2 T6.7)."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentive_backend.shared.contracts.events import (
    WorkflowCreatedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunResumedEvent,
    WorkflowRunRoutingEscalatedEvent,
    WorkflowRunStartedEvent,
    WorkflowRunStepCompletedEvent,
)


def test_workflow_created_event_type_constant() -> None:
    assert WorkflowCreatedEvent.event_type == "workflow_engine.workflow.created"


def test_workflow_created_event_validates_minimal_payload() -> None:
    event = WorkflowCreatedEvent(
        workflow_id=uuid4(),
        name="ingest",
        version=1,
        node_count=2,
    )
    assert event.actor == "system"
    assert event.tenant_id is None


# ─── Story 4.2 T6 — run lifecycle events ────────────────────────────────


def test_workflow_run_started_event_type_and_defaults() -> None:
    assert WorkflowRunStartedEvent.event_type == "workflow_engine.workflow_run.started"
    event = WorkflowRunStartedEvent(run_id=uuid4(), workflow_id=uuid4())
    assert event.actor == "system"
    assert event.tenant_id is None


def test_workflow_run_step_completed_event_type_and_status() -> None:
    assert WorkflowRunStepCompletedEvent.event_type == "workflow_engine.workflow_run.step_completed"
    event = WorkflowRunStepCompletedEvent(
        run_id=uuid4(), workflow_id=uuid4(), node_id="a", duration_ms=120
    )
    assert event.status == "success"


def test_workflow_run_resumed_event_type_and_best_effort_node() -> None:
    assert WorkflowRunResumedEvent.event_type == "workflow_engine.workflow_run.resumed"
    event = WorkflowRunResumedEvent(run_id=uuid4(), workflow_id=uuid4())
    assert event.resumed_from_node_id is None
    event_with_node = WorkflowRunResumedEvent(
        run_id=uuid4(), workflow_id=uuid4(), resumed_from_node_id="b"
    )
    assert event_with_node.resumed_from_node_id == "b"


def test_workflow_run_completed_event_type_and_optional_cost() -> None:
    assert WorkflowRunCompletedEvent.event_type == "workflow_engine.workflow_run.completed"
    event = WorkflowRunCompletedEvent(
        run_id=uuid4(),
        workflow_id=uuid4(),
        total_duration_ms=5000,
        total_cost_usd=Decimal("0.0123"),
    )
    assert event.total_cost_usd == Decimal("0.0123")
    event_no_cost = WorkflowRunCompletedEvent(
        run_id=uuid4(), workflow_id=uuid4(), total_duration_ms=5000
    )
    assert event_no_cost.total_cost_usd is None


def test_workflow_run_failed_event_type_and_required_error_summary() -> None:
    assert WorkflowRunFailedEvent.event_type == "workflow_engine.workflow_run.failed"
    event = WorkflowRunFailedEvent(
        run_id=uuid4(),
        workflow_id=uuid4(),
        failed_node_id="b",
        error_summary="LLM provider failure: DependencyError",
    )
    assert event.failed_node_id == "b"
    assert event.error_summary == "LLM provider failure: DependencyError"


def test_workflow_run_failed_event_rejects_a_missing_error_summary() -> None:
    """The test above is named "required error_summary" but only ever built a
    VALID event — it would have passed just as green with the field optional.
    A `failed` event with no reason is useless to both the SSE client and the
    Trace Explorer, so the requirement is worth pinning."""
    with pytest.raises(PydanticValidationError):
        WorkflowRunFailedEvent(run_id=uuid4(), workflow_id=uuid4(), failed_node_id="b")  # type: ignore[call-arg]


def test_workflow_run_failed_event_enforces_the_error_summary_length_cap() -> None:
    """`max_length=500` mirrors `_mark_failed`'s own `[:500]` truncation. If
    the two ever drift apart, a long provider error would be truncated by the
    service but REJECTED by the event — losing the `failed` event entirely on
    exactly the runs that failed hardest."""
    WorkflowRunFailedEvent(
        run_id=uuid4(), workflow_id=uuid4(), failed_node_id="b", error_summary="x" * 500
    )
    with pytest.raises(PydanticValidationError):
        WorkflowRunFailedEvent(
            run_id=uuid4(), workflow_id=uuid4(), failed_node_id="b", error_summary="x" * 501
        )


def test_workflow_run_failed_event_allows_an_unknown_failed_node() -> None:
    """`failed_node_id` is best-effort (T6.3): a run that dies before any node
    is scheduled — or before the graph even compiles — has none to report."""
    event = WorkflowRunFailedEvent(
        run_id=uuid4(), workflow_id=uuid4(), failed_node_id=None, error_summary="boom"
    )
    assert event.failed_node_id is None


# ─── Story 4.3 T7.2 — routing_escalated event ───────────────────────────


def test_workflow_run_routing_escalated_event_type_and_defaults() -> None:
    assert (
        WorkflowRunRoutingEscalatedEvent.event_type
        == "workflow_engine.workflow_run.routing_escalated"
    )
    event = WorkflowRunRoutingEscalatedEvent(
        run_id=uuid4(),
        workflow_id=uuid4(),
        node_id="a",
        llm_model="claude-haiku-4-5",
        llm_latency_ms=42,
        reason="best guess",
    )
    assert event.candidates == []
    assert event.decision_target == []
    assert event.confidence_best is None
    assert event.rule_id_best is None
    assert event.context == {}
    assert event.tenant_id is None


def test_workflow_run_routing_escalated_event_full_payload() -> None:
    event = WorkflowRunRoutingEscalatedEvent(
        run_id=uuid4(),
        workflow_id=uuid4(),
        node_id="a",
        candidates=["b", "c"],
        decision_target=["b"],
        confidence_best=0.5,
        rule_id_best="weak-rule",
        reason="the LLM picked b",
        llm_model="claude-haiku-4-5",
        llm_latency_ms=120,
        context={"candidate_count": 2, "has_parsable_output": True},
    )
    assert event.candidates == ["b", "c"]
    assert event.decision_target == ["b"]
    assert event.confidence_best == 0.5
    assert event.rule_id_best == "weak-rule"


def test_workflow_run_routing_escalated_event_enforces_reason_length_cap() -> None:
    """`max_length=500` mirrors `hybrid_router._REASON_MAX_CHARS`."""
    WorkflowRunRoutingEscalatedEvent(
        run_id=uuid4(),
        workflow_id=uuid4(),
        node_id="a",
        reason="x" * 500,
        llm_model="claude-haiku-4-5",
        llm_latency_ms=1,
    )
    with pytest.raises(PydanticValidationError):
        WorkflowRunRoutingEscalatedEvent(
            run_id=uuid4(),
            workflow_id=uuid4(),
            node_id="a",
            reason="x" * 501,
            llm_model="claude-haiku-4-5",
            llm_latency_ms=1,
        )


def test_workflow_run_routing_escalated_event_never_carries_own_output() -> None:
    """T7.3 — the event schema simply has no field for a node's raw output;
    this test pins the field set so a future change cannot reintroduce one
    without a reviewer noticing."""
    fields = set(WorkflowRunRoutingEscalatedEvent.model_fields)
    assert "own_output" not in fields
    assert "node_output" not in fields
