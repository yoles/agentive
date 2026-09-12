"""``/api/v1/workflows`` — Workflow Engine endpoints (Story 4.1, 4.2, 4.3, 4.4).

* ``POST /workflows`` — create a workflow from a client-submitted DAG
  (``{name, nodes, edges}``). Validates structural integrity, branching
  conditions, and surfaces a non-blocking Contrôleur/Producteur LLM-diversity
  warning (FR15, D84). No DAG-builder UI exists — the body is built by the
  caller (Postman, tests, or a future client, out of scope here).
* ``POST /workflows/{workflow_id}/runs`` — start a run of an existing,
  active workflow (Story 4.2 AC1). Returns immediately — execution happens
  in a fire-and-forget background task.
* ``POST /workflows/{workflow_id}/dry-run`` — predictive path/cost estimate
  for a workflow, WITHOUT executing it or calling any LLM (Story 4.4 AC1).
* ``GET /workflows/{workflow_id}/routing-stats`` — hybrid-routing decisions
  aggregated in SQL over every run of the workflow (Story 4.3 AC3).
* ``GET /workflows/runs/{run_id}/events`` — SSE stream of a run's state
  transitions (Story 4.2 AC1).
* ``POST /workflows/runs/{run_id}/pause`` — ask a live run to suspend at its
  next superstep boundary (Story 4.6 AC1).
* ``POST /workflows/runs/{run_id}/resume`` — restart a paused run from its
  last checkpoint (Story 4.6 AC1).
* ``POST /workflows/runs/{run_id}/cancel`` — stop a run for good (Story 4.6
  AC1).

Declaration order matters: ``routing-stats``/``dry-run`` are declared BEFORE
the run-events route under the same prefix. None of the three overlap (3
path segments vs 4, and ``dry-run``/``routing-stats`` are distinct literal
segments), and ``test_router_dependencies.py`` locks that rather than
leaving it to inspection. The three Story 4.6 control routes join the same
4-segment ``/workflows/runs/{run_id}/...`` space as ``events``, each behind
its own distinct literal last segment — so they are unambiguous wherever
they are declared, and the test locks that too.

**202 vs 200 on the control routes.** ``pause`` and ``cancel`` on a LIVE run
return ``202``: the request is RECORDED, and the driver applies it at its
next superstep boundary — bounded by a node's duration, never instant. The
one path that IS immediate — ``cancel`` on an already-``paused`` run, which
has no driver alive to observe anything — returns ``200``. The status code
is the message about whether the effect has already happened, so the routes
declare ``202`` and that single path overrides it on the ``Response``.

Sits behind ``AuthTokenMiddleware`` (Story 1.7). ``AgentiveError`` is raised
for domain failures and converted to RFC 7807 by the global handler in
``app.main``.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from sse_starlette.sse import EventSourceResponse

from agentive_backend.features.workflow_engine.domain.run_control import (
    TERMINAL_EVENT_SUFFIXES,
    TERMINAL_STATUSES,
)
from agentive_backend.features.workflow_engine.dry_run import DryRunService, DryRunSettings
from agentive_backend.features.workflow_engine.schemas import (
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    DryRunRequest,
    DryRunResponse,
    ResumeRunRequest,
    RoutingStatsResponse,
    RunControlResponse,
    StartRunRequest,
    StartRunResponse,
)
from agentive_backend.features.workflow_engine.service import (
    WorkflowExecutionService,
    WorkflowService,
)
from agentive_backend.shared.config import settings
from agentive_backend.shared.event_bus import subscribe
from agentive_backend.shared.exceptions import DependencyError, NotFoundError
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo, WorkflowRunRepo

if TYPE_CHECKING:
    from agentive_backend.infra.db.models import WorkflowRun
    from agentive_backend.shared.event_bus import Event

_log = get_logger(__name__)

router = APIRouter(tags=["workflows"])

# Matches every run lifecycle event published by `WorkflowExecutionService`/
# `WorkflowRecoveryWorker` — `started`, `step_completed`, `resumed`,
# `completed`, `failed` (T6). Subscriptions are process-wide (not scoped by
# run), so the handler filters on `payload["run_id"]` itself.
_RUN_EVENT_PATTERN = re.compile(r"workflow_engine\.workflow_run\.\w+")

# Terminal statuses / event-type suffixes — a stream never outlives these.
#
# The two vocabularies differ ON PURPOSE, and AC1's own wording ("`completed`,
# `error`") conflates them. `workflow_runs.status` uses `error`, and every
# `state` frame this endpoint emits carries that value verbatim. The SSE
# EVENT NAME stays `failed`, because `error` is a reserved event in the
# browser `EventSource` API: it is what fires on a transport-level failure,
# so a server-sent event of the same name would be indistinguishable from a
# dropped connection in `es.addEventListener("error", ...)`. Divergence from
# the AC letter, deliberate — cf Dev Notes.
#
# Story 4.6 T5.4 — BOTH sets are now IMPORTED from `domain.run_control`
# rather than redefined here, because a second definition is how `cancelled`
# came to be missing from one of them: a cancelled run stayed `running` as
# far as this loop was concerned, so every SSE client hung until the
# one-hour `_MAX_STREAM_DURATION_S` ceiling.
#
# T5.4 moved only the STATUS set and declared the duplication closed. The
# event set stayed a literal right underneath the import (review lot 8,
# P-E) — so a fourth terminal status still had to be remembered in two
# places, which is exactly the failure the move was meant to end. It is
# derived from a table there rather than aliased, because the mapping is
# not the identity: status `error` publishes event `failed`.
#
# `paused` is in NEITHER set, and that is deliberate: a paused run can
# resume, so its stream must stay open to carry the rest of the run.
# `_MAX_STREAM_DURATION_S` bounds the case where it never does. This is the
# kind of asymmetry a hurried reader "fixes" in the wrong direction.
_TERMINAL_STATUSES = TERMINAL_STATUSES
_TERMINAL_EVENT_SUFFIXES = TERMINAL_EVENT_SUFFIXES

# How long one queue-poll blocks before the loop re-checks its exit
# conditions (deadline, DB status re-poll).
_QUEUE_POLL_INTERVAL_S = 1.0

# WALL-CLOCK ceiling on a subscription, not an iteration count. The previous
# `for _ in range(3600)` was advertised as "~1h" but only holds when every
# iteration times out: an iteration that RECEIVES an event returns
# immediately, so a chatty run burned the whole budget in seconds — and then
# the generator returned with no terminal event and no error, leaving the
# client waiting forever on a stream that was already closed.
_MAX_STREAM_DURATION_S = 3600.0

# How often the loop re-reads `workflow_runs.status` while idle. The run's
# authoritative status lives in the row, not in the event stream (cf
# `WorkflowExecutionService`'s class docstring, which accepts that a
# lifecycle event may be dropped) — so the stream must not depend solely on
# receiving a terminal EVENT to learn that the run is over.
_STATUS_REPOLL_INTERVAL_S = 15.0

# Bounded per-client buffer. Unbounded, a client that stopped reading grew
# this queue for the entire run. `put_nowait` + drop (below) is what keeps a
# slow consumer from back-pressuring the shared event-bus dispatcher onto
# every OTHER subscriber; the `_STATUS_REPOLL_INTERVAL_S` poll is what makes
# a dropped event recoverable.
_EVENT_QUEUE_MAXSIZE = 256


def _build_workflow_service(request: Request) -> WorkflowService:
    """Return a :class:`WorkflowService` wired from ``app.state``.

    No ``archetype_registry`` needed here (unlike ``agent_registry``) — the
    archetype is read from the ``agent_templates.archetype`` DB column, not
    the in-process YAML registry.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Workflow engine not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    return WorkflowService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
    )


def _build_dry_run_service(request: Request) -> DryRunService:
    """Return a :class:`DryRunService` wired from ``app.state`` (Story 4.4).

    Mirrors :func:`_build_workflow_service`, NOT :func:`_build_execution_service`
    below: only ``session_factory`` is needed — no ``llm_router``/
    ``workflow_checkpointer``/``routing_rules`` (AC1's structural "zero LLM
    call" guarantee — this service is never given the means to call one).

    ``DryRunSettings`` is built here, in the assembly layer, because the
    golden rule forbids reading ``settings`` from inside the domain/service
    modules themselves (mirror ``RoutingSettings``, Story 4.3 T5.1). That
    it happens per request is incidental, not a feature: ``settings`` is a
    process-wide singleton populated from the environment at startup, so a
    rebuild yields the same values every time — the comment here used to
    claim freshness this buys nothing toward (review fix P18).
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Workflow engine not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    return DryRunService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        workflow_run_repo=WorkflowRunRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
        settings=DryRunSettings(
            history_limit=settings.dry_run_history_limit,
            fallback_input_tokens=settings.dry_run_fallback_input_tokens,
            fallback_output_tokens=settings.dry_run_fallback_output_tokens,
            budget_cap_usd=settings.dry_run_budget_cap_usd,
        ),
    )


def _build_execution_service(request: Request) -> WorkflowExecutionService:
    """Return the shared :class:`WorkflowExecutionService` from ``app.state``
    (Story 4.2 T9.3) — 503 if the lifespan hasn't wired it yet.

    The lifespan builds exactly one instance and shares it with the recovery
    worker; T9.3's comment already claimed this function read it. It did not:
    it rebuilt a fresh service and three fresh repos on EVERY request, while
    ``app.state.workflow_execution_service`` sat unread. Harmless in effect,
    but it meant the router and the recovery worker were quietly running on
    two different objects — so any future per-instance state (a cache, a
    semaphore, a concurrency cap) would have silently applied to only one of
    them.
    """
    # `app.state` is untyped (`Any`), hence the explicit isinstance narrowing
    # rather than a bare truthiness check.
    service = getattr(request.app.state, "workflow_execution_service", None)
    if isinstance(service, WorkflowExecutionService):
        return service

    # Not wired — report WHICH lifespan resource is missing rather than the
    # single opaque "service is None", since that is what an operator reading
    # the 503 needs in order to find the failed startup step.
    missing = [
        name
        for name in ("session_factory", "llm_router", "workflow_checkpointer", "routing_rules")
        if getattr(request.app.state, name, None) is None
    ] or ["workflow_execution_service"]
    raise DependencyError(
        detail="Workflow execution engine not initialised — check lifespan startup logs.",
        context={"missing": missing},
    )


@router.post(
    "/workflows",
    response_model=CreateWorkflowResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow from a client-submitted DAG (Story 4.1)",
)
async def create_workflow(request: Request, body: CreateWorkflowRequest) -> CreateWorkflowResponse:
    """201 on success — ``warnings`` may be non-empty (never blocking, AC4).

    Errors :
    * 422 — duplicate ``node_id``, dangling edge, unknown ``agent_template_id``,
      cycle, invalid branching-condition syntax, or a condition referencing a
      variable not exposed by the emitting node's ``output_contract.core``
      (RFC 7807).
    * 503 — lifespan state missing (session factory).
    """
    service = _build_workflow_service(request)
    return await service.create_workflow(
        name=body.name,
        nodes=body.nodes,
        edges=body.edges,
        tenant_id=None,  # Story 4.1 anti-scope — single-tenant MVP.
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=StartRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a workflow run (Story 4.2 AC1)",
)
async def start_workflow_run(
    workflow_id: UUID, request: Request, body: StartRunRequest
) -> StartRunResponse:
    """201 returned IMMEDIATELY — execution runs in a background task, the
    request never blocks on the run's progress (AC1).

    Errors:
    * 404 — ``workflow_id`` unknown (URL's primary resource, unlike 4.1's
      ``POST /workflows`` where an invalid id lives in the body — cf Dev
      Notes § "404 vs 422").
    * 422 — the workflow exists but ``status != "active"``; ``force=true``
      with a blank/absent or over-long (>2000 chars) ``reason``; or a
      ``reason`` supplied WITHOUT ``force=true``, which would otherwise be
      accepted and silently discarded (Story 4.5 AC3, review P7/P21).
      A Mise en Place check failed without ``force`` and at least one of the
      failures is PERMANENT — a missing API key, an absent namespace, a
      budget overrun, a tool no longer exposed (Story 4.5 AC2, review BS5):
      retrying cannot help, so this is a 422 rather than a 503.
    * 503 — lifespan state missing (session factory / llm_router / checkpointer),
      or a Mise en Place check failed without ``force`` and EVERY failure is
      transient (an MCP server that is down, a check that timed out), i.e.
      an identical retry may genuinely succeed (Story 4.5 AC2, review BS5).
    """
    service = _build_execution_service(request)
    return await service.start_run(
        workflow_id=workflow_id,
        run_input=body.input,
        tenant_id=None,  # Story 4.2 anti-scope — single-tenant MVP.
        force=body.force,
        reason=body.reason,
    )


@router.post(
    "/workflows/{workflow_id}/dry-run",
    response_model=DryRunResponse,
    status_code=status.HTTP_200_OK,
    summary="Dry Run predictif d'un workflow (Story 4.4 AC1)",
)
async def dry_run_workflow(
    workflow_id: UUID, request: Request, body: DryRunRequest
) -> DryRunResponse:
    """200 OK, not 201: unlike ``POST /workflows`` and
    ``POST /workflows/{id}/runs``, this endpoint creates no resource — it
    is a pure estimation (no ``workflow_runs`` row, ZERO real LLM call —
    ``DryRunService`` is never given an ``LLMRouter``, cf Dev Notes).

    Errors:
    * 404 — ``workflow_id`` unknown (URL's primary resource, mirror 4.2/4.3).
    * 422 — the workflow exists but ``status != "active"``.
    * 503 — lifespan state missing (session factory).
    """
    service = _build_dry_run_service(request)
    return await service.dry_run(
        workflow_id=workflow_id,
        task_input=body.input,
        tenant_id=None,  # Story 4.4 anti-scope — single-tenant MVP (Story 4.9).
    )


@router.get(
    "/workflows/{workflow_id}/routing-stats",
    response_model=RoutingStatsResponse,
    summary="Aggregate hybrid-routing decisions for a workflow (Story 4.3 AC3)",
)
async def get_workflow_routing_stats(workflow_id: UUID, request: Request) -> RoutingStatsResponse:
    """``% routages déterministes vs LLM``, aggregated in SQL over every run
    of ``workflow_id`` — never a Prometheus label (unbounded cardinality,
    Dev Notes § Divergences assumées).

    Errors:
    * 404 — ``workflow_id`` unknown (the URL's primary resource, mirror the
      404-vs-422 decision of Story 4.2's ``POST /workflows/{workflow_id}/runs``,
      not Story 4.1's ``POST /workflows``).
    * 503 — lifespan state missing (session factory).
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Workflow engine not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    workflow_repo = WorkflowRepo(session_factory=session_factory)
    await workflow_repo.require_by_id(workflow_id)

    workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
    runs_counted, deterministic, llm_escalated = await workflow_run_repo.aggregate_routing_modes(
        workflow_id
    )
    total = deterministic + llm_escalated
    deterministic_pct = round(deterministic / total * 100, 1) if total > 0 else None
    return RoutingStatsResponse(
        workflow_id=workflow_id,
        runs_counted=runs_counted,
        deterministic=deterministic,
        llm_escalated=llm_escalated,
        deterministic_pct=deterministic_pct,
    )


@router.get(
    "/workflows/runs/{run_id}/events",
    summary="Stream a run's state transitions via SSE (Story 4.2 AC1)",
)
async def stream_workflow_run_events(run_id: UUID, request: Request) -> EventSourceResponse:
    """Sends the current run state immediately on connect (catch-up for a
    run that already progressed), then live ``started``/``step_completed``/
    ``resumed``/``completed``/``failed`` events until the run reaches a
    terminal state or the client disconnects.

    Errors:
    * 404 — ``run_id`` unknown.
    * 503 — lifespan state missing (session factory).
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise DependencyError(
            detail="Workflow engine not initialised — check lifespan startup logs.",
            context={"missing": ["session_factory"]},
        )
    workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
    run = await workflow_run_repo.get_by_id(run_id)
    if run is None:
        raise NotFoundError(
            detail=f"Workflow run '{run_id}' not found", context={"run_id": str(run_id)}
        )
    return EventSourceResponse(_stream_run_events(run, workflow_run_repo))


@router.post(
    "/workflows/runs/{run_id}/pause",
    response_model=RunControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ask a running workflow run to pause (Story 4.6 AC1)",
)
async def pause_workflow_run(run_id: UUID, request: Request) -> RunControlResponse:
    """``202`` — ACCEPTED, not applied.

    The response still reads ``status="running"``, with
    ``control_signal="pause"``: interruption is cooperative, so the run stops
    at its driver's next superstep boundary. That delay is bounded by a
    node's duration (``NODE_TIMEOUT_S``, plus any ``error_policy`` retries)
    and is the price of an interruption that loses no work and cuts no
    already-billed LLM call.

    Legal only from ``running``.

    Errors, shared by all three control routes — ``resume`` adds ``422``
    on top, because it alone re-runs the Mise en Place pre-flight (review
    IG2); ``pause`` and ``cancel`` stop a run and can never be blocked by
    the state of the environment it would have run in.

    * 404 — ``run_id`` unknown (the URL's primary resource, mirror
      ``POST /workflows/{workflow_id}/runs``).
    * 409 — the run is not in a status this action accepts, or a concurrent
      caller won the compare-and-set (a control request was already pending,
      or the run reached a terminal status between the read and the write).
      The RFC 7807 ``context`` names ``run_id``, ``current_status``,
      ``allowed_from``, ``action`` and ``pending_control_signal`` on BOTH
      paths — the last of these added by the review (BS1), so a caller told
      "a request is already pending" can see WHICH, and therefore whether
      escalating to ``cancel`` would get through; ``null`` means the refusal
      is about the status alone and no escalation will help. Nothing is
      published on this path.
    * 503 — lifespan state missing.
    """
    service = _build_execution_service(request)
    return await service.request_run_control(
        run_id=run_id,
        action="pause",
        tenant_id=None,  # Story 4.6 anti-scope — single-tenant MVP (Story 4.9).
    )


@router.post(
    "/workflows/runs/{run_id}/resume",
    response_model=RunControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Resume a paused workflow run (Story 4.6 AC1)",
)
async def resume_workflow_run(
    run_id: UUID, request: Request, body: ResumeRunRequest | None = None
) -> RunControlResponse:
    """``202`` — the row moves to ``running`` synchronously, but execution
    itself restarts in a background task, exactly like ``start_run``.

    Resumption goes through the path Story 4.2 already delivered
    (``astream(None, config)``), so LangGraph picks up from its own last
    committed checkpoint and **no node that already completed is
    re-executed**.

    Legal only from ``paused``.

    Re-runs the Mise en Place pre-flight (Story 4.6 review, `IG2`), so this
    route answers ``422``/``503`` on a failing check exactly like
    ``POST /workflows/{id}/runs`` — and, critically, leaves the run
    ``paused`` rather than letting it die ``error`` on its first node in an
    environment that decayed while it waited. The optional body carries the
    same ``force``/``reason`` bypass; ``pause`` and ``cancel`` take no body
    because neither resumes execution.
    """
    service = _build_execution_service(request)
    control = body or ResumeRunRequest()
    return await service.request_run_control(
        run_id=run_id,
        action="resume",
        tenant_id=None,
        force=control.force,
        reason=control.reason,
    )


@router.post(
    "/workflows/runs/{run_id}/cancel",
    response_model=RunControlResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={status.HTTP_200_OK: {"model": RunControlResponse}},
    summary="Cancel a workflow run (Story 4.6 AC1)",
)
async def cancel_workflow_run(
    run_id: UUID, request: Request, response: Response
) -> RunControlResponse:
    """``202`` from ``running`` (deferred to the next superstep boundary),
    ``200`` from ``paused`` (terminal immediately).

    The difference is not cosmetic: a paused run has no driver alive to
    observe a signal, so the request applies on the spot and the response
    already reads ``status="cancelled"``. The status code is what tells a
    client which of the two happened without inspecting the body.

    Legal from ``running`` or ``paused``.
    """
    service = _build_execution_service(request)
    result = await service.request_run_control(run_id=run_id, action="cancel", tenant_id=None)
    if result.status == "cancelled":
        response.status_code = status.HTTP_200_OK
    return result


def _state_event(run: WorkflowRun, *, reason: str | None = None) -> dict[str, str]:
    """Build the ``state`` frame — AC1's catch-up payload.

    AC1 asks for "l'état courant [...] rattrapage si le run a déjà
    progressé", and a bare ``status`` does not say how far the run got: a
    client attaching to a run already on its fourth node received exactly
    the same frame as one attaching at node zero. The applicative checkpoint
    summary already holds the answer, so surface the two fields that
    describe progress, plus the failure reason for a run that is already
    terminal.

    Deliberately NOT forwarded: ``node_outputs_preview`` (up to 500 chars
    per node — this frame must stay small) and ``recovery_attempts`` (an
    internal bookkeeping counter, not run state).
    """
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    payload: dict[str, Any] = {"run_id": str(run.id), "status": run.status}
    last_node_id = checkpoint.get("last_node_id")
    if isinstance(last_node_id, str):
        payload["last_node_id"] = last_node_id
    node_statuses = checkpoint.get("node_statuses")
    if isinstance(node_statuses, dict):
        payload["node_statuses"] = node_statuses
    last_error = checkpoint.get("last_error")
    if isinstance(last_error, str):
        # Already redacted at the source (`_mark_failed`), never re-derived here.
        payload["last_error"] = last_error
    # Story 4.6 T5.5 — a run that has been ASKED to pause still reads
    # `status="running"` (interruption is cooperative), so without this a
    # client watching the stream sees nothing at all between the request and
    # the effect. Read straight off the row, no hardcoded enumeration.
    control_signal = getattr(run, "control_signal", None)
    if isinstance(control_signal, str):
        payload["control_signal"] = control_signal
    if reason is not None:
        payload["reason"] = reason
    return {"event": "state", "data": json.dumps(payload, default=str)}


def _state_signature(run: WorkflowRun) -> tuple[str, str | None]:
    """The part of a row a `state` frame actually carries, for comparison.

    `status` alone is not enough: a pause REQUEST changes nothing but
    `control_signal`, and that is exactly what the client is waiting to see
    between asking for a pause and getting one. Reads the row the same
    defensive way :func:`_state_event` does, so the two can never disagree
    about what "changed" means.
    """
    control_signal = getattr(run, "control_signal", None)
    return run.status, control_signal if isinstance(control_signal, str) else None


async def _stream_run_events(
    run: WorkflowRun, workflow_run_repo: WorkflowRunRepo
) -> AsyncIterator[dict[str, str]]:
    run_id = run.id

    if run.status in _TERMINAL_STATUSES:
        yield _state_event(run)
        return

    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)

    async def _handler(event: Event) -> None:
        if str(event.payload.get("run_id")) != str(run_id):
            return
        try:
            # NOT `await queue.put(...)`: this handler runs on the shared
            # event-bus dispatcher, so blocking here would make one
            # unresponsive SSE client stall delivery for every other
            # subscriber in the process. Dropping is safe because the
            # periodic status re-poll below reconciles the client anyway.
            queue.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning(
                "workflow_engine.sse_event_dropped",
                run_id=str(run_id),
                event_type=event.event_type,
            )

    # Subscribe BEFORE re-checking status — closes the race window where the
    # run could transition between the route handler's initial load and here.
    sub = await subscribe(_RUN_EVENT_PATTERN, _handler)
    try:
        current = await workflow_run_repo.get_by_id(run_id)
        latest = current if current is not None else run
        yield _state_event(latest)
        if latest.status in _TERMINAL_STATUSES:
            return
        # What the client has been told VIA A `state` FRAME. Lifecycle
        # events deliberately do NOT update it: deducing a status from an
        # event name is the mapping that is not 1:1 (status `error` is event
        # `failed`), and getting it wrong here would suppress the very frame
        # this exists to send. The cost of not deducing is one redundant —
        # and idempotent — `state` frame after a change that WAS delivered
        # normally, and only once the run has gone quiet. That is the right
        # side to err on.
        last_state = _state_signature(latest)

        # NOT `request.is_disconnected()` here. That call re-issues a
        # `receive()` on the ASGI channel, which conflicts with
        # `sse-starlette`'s own consumption of it in some arrangements —
        # including under the pure-ASGI middlewares this story converted
        # `AuthTokenMiddleware`/`CorrelationIdMiddleware` to (an earlier
        # version of this comment blamed `BaseHTTPMiddleware`, which this
        # very story had already removed; the conflict is with
        # `sse-starlette`, not with the middleware base class). No polling
        # is needed: `sse-starlette` stops iterating this generator on a
        # real client disconnect, raising `GeneratorExit` — caught by
        # nothing here, so the `finally` still unsubscribes.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MAX_STREAM_DURATION_S
        next_repoll = loop.time() + _STATUS_REPOLL_INTERVAL_S

        while loop.time() < deadline:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_QUEUE_POLL_INTERVAL_S)
            except TimeoutError:
                # Idle tick — the cheapest place to reconcile against the
                # row, which is the authoritative status. Without this, a
                # single dropped terminal event left the client hanging
                # until the deadline.
                #
                # Reconciles ANY divergence, not just a terminal one. The
                # terminal-only version fixed the case it was written for and
                # left the worse one open: `paused` is deliberately NOT
                # terminal (the stream must survive a resume), so a dropped
                # `paused` event — and `put_nowait` above drops by design
                # under a slow consumer — left the client watching a silent
                # stream, showing "running", for up to the full hour. Nothing
                # else would ever arrive: a paused run emits no events, so the
                # queue never wakes up. The one mechanism that could tell it
                # was this poll, and it was looking only for the end.
                if loop.time() >= next_repoll:
                    next_repoll = loop.time() + _STATUS_REPOLL_INTERVAL_S
                    current = await workflow_run_repo.get_by_id(run_id)
                    if current is not None and _state_signature(current) != last_state:
                        last_state = _state_signature(current)
                        yield _state_event(current, reason="status_repoll")
                        if current.status in _TERMINAL_STATUSES:
                            return
                continue
            action = event.event_type.rsplit(".", 1)[-1]
            yield {"event": action, "data": json.dumps(event.payload, default=str)}
            if action in _TERMINAL_EVENT_SUFFIXES:
                return

        # Deadline reached. Always close on a definitive frame rather than
        # just returning — a silent end is indistinguishable, client-side,
        # from a stream still waiting for the next event.
        current = await workflow_run_repo.get_by_id(run_id)
        if current is not None:
            yield _state_event(current, reason="stream_timeout")
        else:
            yield {
                "event": "state",
                "data": json.dumps(
                    {"run_id": str(run_id), "status": "unknown", "reason": "stream_timeout"}
                ),
            }
    finally:
        sub.unsubscribe()


__all__ = ["router"]
