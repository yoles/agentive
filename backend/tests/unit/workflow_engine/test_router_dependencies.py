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


# ─── Story 4.3 T10.4 — route-collision lock ──────────────────────────────


def _resolve_endpoint_name(path: str) -> str | None:
    """Name of the endpoint FastAPI would dispatch ``GET path`` to."""
    from fastapi import FastAPI
    from starlette.routing import Match

    from agentive_backend.features.workflow_engine.router import router as workflows_router

    app = FastAPI()
    app.include_router(workflows_router, prefix="/api/v1")
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "query_string": b"",
        "root_path": "",
    }
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            name: str | None = getattr(route, "name", None)
            return name
    return None


def test_routing_stats_route_does_not_capture_the_run_events_route() -> None:
    """T10.4 was checked off without this lock. FastAPI dispatches on
    DECLARATION ORDER, and `/workflows/{workflow_id}/routing-stats` is
    declared BEFORE `/workflows/runs/{run_id}/events` under the same prefix.
    The two happen not to overlap (3 path segments vs 4), but nothing
    expressed that — so re-declaring either one with a different arity, or
    adding `/workflows/{workflow_id}/{action}`, would silently reroute a
    live SSE endpoint.
    """
    run_id = "0199d0a1-0000-7000-8000-000000000000"
    workflow_id = "0199d0a1-1111-7000-8000-000000000000"

    assert _resolve_endpoint_name(f"/api/v1/workflows/runs/{run_id}/events") == (
        "stream_workflow_run_events"
    )
    assert _resolve_endpoint_name(f"/api/v1/workflows/{workflow_id}/routing-stats") == (
        "get_workflow_routing_stats"
    )


def test_a_workflow_literally_named_runs_does_not_steal_the_events_route() -> None:
    """The inverse direction of the same collision: `runs` is a valid UUID
    path position for `{workflow_id}`, so the events route must stay
    reachable regardless."""
    assert _resolve_endpoint_name("/api/v1/workflows/runs/routing-stats") == (
        "get_workflow_routing_stats"
    )


# ─── Story 4.4 T5.3 — dry-run route-collision lock ───────────────────────


def _resolve_post_endpoint_name(path: str) -> str | None:
    """Same as :func:`_resolve_endpoint_name` but for ``POST`` — `dry-run`
    is declared as a POST route, unlike `routing-stats`/`events` (GET)."""
    from fastapi import FastAPI
    from starlette.routing import Match

    from agentive_backend.features.workflow_engine.router import router as workflows_router

    app = FastAPI()
    app.include_router(workflows_router, prefix="/api/v1")
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
        "root_path": "",
    }
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            name: str | None = getattr(route, "name", None)
            return name
    return None


def test_dry_run_route_does_not_collide_with_runs_route() -> None:
    """`POST /workflows/{workflow_id}/dry-run` and
    `POST /workflows/{workflow_id}/runs` are distinct literal segments
    after the same `{workflow_id}` prefix — neither must capture the
    other."""
    workflow_id = "0199d0a1-2222-7000-8000-000000000000"

    assert _resolve_post_endpoint_name(f"/api/v1/workflows/{workflow_id}/dry-run") == (
        "dry_run_workflow"
    )
    assert _resolve_post_endpoint_name(f"/api/v1/workflows/{workflow_id}/runs") == (
        "start_workflow_run"
    )


def test_a_workflow_literally_named_dry_run_does_not_steal_the_runs_route() -> None:
    assert _resolve_post_endpoint_name("/api/v1/workflows/dry-run/runs") == ("start_workflow_run")


def test_dry_run_route_does_not_collide_with_routing_stats_route() -> None:
    """Review fix P12 — T5.3 names `routing-stats` AND `runs/{run_id}/events`
    as the two routes to check `dry-run` against, and `router.py`'s module
    docstring claims `test_router_dependencies.py` "locks that". Only the
    `runs` pair was actually asserted. `routing-stats` sits at the same
    depth as `dry-run` under the same `{workflow_id}` prefix, so it is the
    closest neighbour of the three."""
    workflow_id = "0199d0a1-3333-7000-8000-000000000000"

    assert _resolve_endpoint_name(f"/api/v1/workflows/{workflow_id}/routing-stats") == (
        "get_workflow_routing_stats"
    )
    # Same path, different verb: `dry-run` is POST-only and must not answer
    # GET, nor may `routing-stats` (GET-only) answer the POST.
    assert _resolve_post_endpoint_name(f"/api/v1/workflows/{workflow_id}/routing-stats") is None
    assert _resolve_endpoint_name(f"/api/v1/workflows/{workflow_id}/dry-run") is None


def test_dry_run_route_does_not_collide_with_run_events_route() -> None:
    """Review fix P12 — the third pair T5.3 names. `runs/{run_id}/events` is
    4 segments deep against `dry-run`'s 3, so they cannot overlap; this
    locks it rather than leaving it to inspection, which is precisely what
    the module docstring already promised."""
    run_id = "0199d0a1-4444-7000-8000-000000000000"

    assert _resolve_endpoint_name(f"/api/v1/workflows/runs/{run_id}/events") == (
        "stream_workflow_run_events"
    )
    # A workflow literally named `runs` must not let `dry-run` swallow the
    # events stream, nor vice versa.
    assert _resolve_post_endpoint_name(f"/api/v1/workflows/runs/{run_id}/events") is None


def test_missing_session_factory_raises_503() -> None:
    with pytest.raises(DependencyError) as exc:
        _build_workflow_service(_request())
    assert exc.value.status == 503
    assert exc.value.context["missing"] == ["session_factory"]


def test_fully_wired_state_builds_the_service() -> None:
    from agentive_backend.features.workflow_engine.service import WorkflowService

    service = _build_workflow_service(_request(session_factory=object()))
    assert isinstance(service, WorkflowService)


# ─── Story 4.4 — `_build_dry_run_service` dependency guard ───────────────


def test_dry_run_service_missing_session_factory_raises_503() -> None:
    from agentive_backend.features.workflow_engine.router import _build_dry_run_service

    with pytest.raises(DependencyError) as exc:
        _build_dry_run_service(_request())
    assert exc.value.status == 503
    assert exc.value.context["missing"] == ["session_factory"]


def test_dry_run_service_fully_wired_state_builds_the_service() -> None:
    from agentive_backend.features.workflow_engine.dry_run import DryRunService
    from agentive_backend.features.workflow_engine.router import _build_dry_run_service

    service = _build_dry_run_service(_request(session_factory=object()))
    assert isinstance(service, DryRunService)


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
        routing_rules=(),
    )
    request = _request(
        workflow_execution_service=shared,
        session_factory=object(),
        llm_router=object(),
        workflow_checkpointer=object(),
        routing_rules=object(),
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
    assert exc.value.context["missing"] == [
        "llm_router",
        "workflow_checkpointer",
        "routing_rules",
    ]
