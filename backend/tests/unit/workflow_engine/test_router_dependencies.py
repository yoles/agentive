"""Unit: `_build_workflow_service` dependency guard (Story 4.1 code-review patch).

Mirrors `tests/unit/memory_manager/test_router_dependencies.py` (Story 3.1 P12):
the 503 branch was prescribed by the spec and implemented, but nothing
protected it from a regression (every e2e fixture pre-seeds `app.state`, so
the guard never ran).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentive_backend.features.workflow_engine.router import _build_workflow_service
from agentive_backend.shared.exceptions import DependencyError


def _request(**state: Any) -> Any:
    """A minimal stand-in for `Request` (only `request.app.state` is read)."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def test_missing_session_factory_raises_503() -> None:
    with pytest.raises(DependencyError) as exc:
        _build_workflow_service(_request())
    assert exc.value.status == 503
    assert exc.value.context["missing"] == ["session_factory"]


def test_fully_wired_state_builds_the_service() -> None:
    from agentive_backend.features.workflow_engine.service import WorkflowService

    service = _build_workflow_service(_request(session_factory=object()))
    assert isinstance(service, WorkflowService)


# ─── Lot 3 — finding #37 : the shared execution service ────────────────


def test_execution_service_comes_from_app_state() -> None:
    """T9.3's comment claimed this function read the lifespan's shared
    instance. It did not — it rebuilt a service and three repos on EVERY
    request while `app.state.workflow_execution_service` sat unread, leaving
    the router and the recovery worker on two different objects."""
    from agentive_backend.features.workflow_engine.router import _build_execution_service
    from agentive_backend.features.workflow_engine.service import WorkflowExecutionService

    shared = WorkflowExecutionService(
        workflow_repo=SimpleNamespace(),  # type: ignore[arg-type]
        workflow_run_repo=SimpleNamespace(),  # type: ignore[arg-type]
        template_repo=SimpleNamespace(),  # type: ignore[arg-type]
        llm_router=SimpleNamespace(),  # type: ignore[arg-type]
        checkpointer=SimpleNamespace(),  # type: ignore[arg-type]
    )
    request = _request(
        workflow_execution_service=shared,
        session_factory=object(),
        llm_router=object(),
        workflow_checkpointer=object(),
    )

    assert _build_execution_service(request) is shared
    # Same object across calls — no per-request rebuild.
    assert _build_execution_service(request) is _build_execution_service(request)


def test_unwired_execution_service_reports_the_missing_lifespan_resources() -> None:
    """The 503 must still name WHICH startup step failed — that is what an
    operator reading it needs, not an opaque "service is None"."""
    from agentive_backend.features.workflow_engine.router import _build_execution_service

    with pytest.raises(DependencyError) as exc:
        _build_execution_service(_request(session_factory=object()))
    assert exc.value.status == 503
    assert exc.value.context["missing"] == ["llm_router", "workflow_checkpointer"]
