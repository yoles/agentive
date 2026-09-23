"""``/api/v1/workflows`` — Workflow Engine endpoints (Story 4.1, 4.2, 4.3, 4.4).

* ``POST /workflows`` — create a workflow from a client-submitted DAG
  (``{name, nodes, edges}``). Validates structural integrity, branching
  conditions, and surfaces a non-blocking Contrôleur/Producteur LLM-diversity
  warning (FR15, D84). No DAG-builder UI exists — the body is built by the
  caller (Postman, tests, or a future client, out of scope here).
  Idempotent on replay (Story 4.8 AC1): an identical body submitted twice
  creates one workflow and answers ``200`` the second time, not ``201``.
* ``POST /workflows/{workflow_id}/runs`` — start a run of an existing,
  active workflow (Story 4.2 AC1). Returns immediately — execution happens
  in a fire-and-forget background task.
* ``POST /workflows/{workflow_id}/dry-run`` — predictive path/cost estimate
  for a workflow, WITHOUT executing it or calling any LLM (Story 4.4 AC1).
* ``GET /workflows/{workflow_id}/routing-stats`` — hybrid-routing decisions
  aggregated in SQL over every run of the workflow (Story 4.3 AC3).
* ``GET /workflows/{workflow_id}/handoff-stats`` — handoff-summary token
  reduction aggregated in SQL over every run of the workflow (Story 4.7 AC3).
* ``GET /workflows/runs/{run_id}/events`` — SSE stream of a run's state
  transitions (Story 4.2 AC1).
* ``POST /workflows/runs/{run_id}/pause`` — ask a live run to suspend at its
  next superstep boundary (Story 4.6 AC1).
* ``POST /workflows/runs/{run_id}/resume`` — restart a paused run from its
  last checkpoint (Story 4.6 AC1).
* ``POST /workflows/runs/{run_id}/cancel`` — stop a run for good (Story 4.6
  AC1).

Declaration order matters: ``routing-stats``/``dry-run``/``handoff-stats``
are declared BEFORE the run-events route under the same prefix. None of the
four overlap (3 path segments vs 4, and ``dry-run``/``routing-stats``/
``handoff-stats`` are distinct literal segments), and
``test_router_dependencies.py`` locks that rather than leaving it to
inspection. The three Story 4.6 control routes join the same 4-segment
``/workflows/runs/{run_id}/...`` space as ``events``, each behind its own
distinct literal last segment — so they are unambiguous wherever they are
declared, and the test locks that too.

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
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError
from sse_starlette.sse import EventSourceResponse

from agentive_backend.features.workflow_engine.domain.run_control import (
    TERMINAL_EVENT_SUFFIXES,
    TERMINAL_STATUSES,
)
from agentive_backend.features.workflow_engine.dry_run import DryRunService, DryRunSettings
from agentive_backend.features.workflow_engine.recovery import derive_stale_threshold_s
from agentive_backend.features.workflow_engine.schemas import (
    AcknowledgementOut,
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    DryRunRequest,
    DryRunResponse,
    HandoffStatsResponse,
    MiseEnPlaceReportOut,
    NodeOutputOut,
    NodeOutputSource,
    NodeOutputsSource,
    ResumeRunRequest,
    RoutingStatsResponse,
    RunControlResponse,
    RunDetailResponse,
    RunNodeOutputResponse,
    StartRunRequest,
    StartRunResponse,
)
from agentive_backend.features.workflow_engine.service import (
    WorkflowExecutionService,
    WorkflowService,
)
from agentive_backend.shared.config import settings
from agentive_backend.shared.event_bus import subscribe
from agentive_backend.shared.exceptions import DependencyError, NotFoundError, ValidationError
from agentive_backend.shared.llm.redaction import redact_secrets
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
#
# Story 4.9 AC6/T6.6 — this ~1h floor and `recovery.derive_stale_threshold_s`
# (the recovery worker's own "how long can a healthy node stay silent"
# ceiling) were never reconciled. At the settings' `le` values in place when
# this was written — `workflow_retry_base_delay_s<=60`,
# `...max_delay_s<=300`, `routing_escalation_timeout_s<=60`,
# `workflow_handoff_summary_timeout_s<=45` — the derived stale threshold
# reaches **3675 s**, past the 3600 s this constant hardcoded: a client could
# receive `stream_timeout` with `status: "running"` on a run the recovery
# worker does not even consider orphaned yet. The 4% margin between 3450 and
# 3600 s the review measured before this fix was never chosen by anyone; it
# was what the settings happened to allow at the time.
#
# Reconciled by DERIVING this ceiling from the same formula rather than
# guarding it with a test alone — a test alone would need updating by hand
# every time either side's defaults move, exactly the drift that produced
# the 3675-vs-3600 inversion. `_stream_duration_headroom_s` on top of the
# derived threshold absorbs the recovery worker's own poll cadence
# (`interval_s`, default 30 s) plus scheduling slack, so the stream always
# outlives the point where a healthy run could first be reclaimed.
_MAX_STREAM_DURATION_FLOOR_S = 3600.0
_STREAM_DURATION_HEADROOM_S = 120.0


def _max_stream_duration_s() -> float:
    """The SSE ceiling for THIS process's actual settings (Story 4.9
    AC6/T6.6) — never less than the ~1h floor above, and never less than
    the recovery worker's own worst-case stale threshold plus headroom.

    Computed per call (settings do not change within a process's lifetime,
    but nothing here assumes a specific import order, unlike a module-level
    constant computed once at import time would).
    """
    stale_threshold_s = derive_stale_threshold_s(
        base_delay_s=settings.workflow_retry_base_delay_s,
        max_delay_s=settings.workflow_retry_max_delay_s,
        escalation_timeout_s=settings.routing_escalation_timeout_s,
        handoff_summary_timeout_s=settings.workflow_handoff_summary_timeout_s,
        # Story 5.0 — depuis la boucle d'outils, ce plafond plat REMPLACE
        # `NODE_TIMEOUT_S` dans la dérivation : un nœud n'est plus un appel
        # LLM mais N appels LLM et M appels d'outils, tous enfermés dedans.
        tool_loop_max_wall_clock_s=settings.tool_loop_max_wall_clock_s,
    )
    return max(_MAX_STREAM_DURATION_FLOOR_S, stale_threshold_s + _STREAM_DURATION_HEADROOM_S)


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
    responses={
        status.HTTP_200_OK: {"model": CreateWorkflowResponse},
        # Declared so the 409 the docstring documents is actually visible in
        # the OpenAPI schema — a generated client cannot handle a code the
        # spec never mentions (review P5). No `model`: the RFC 7807 body is
        # produced by the global `AgentiveError` handler, not by this route.
        status.HTTP_409_CONFLICT: {
            "description": "Fingerprint collision whose original row could not be re-read."
        },
    },
    summary="Create a workflow from a client-submitted DAG (Story 4.1)",
)
async def create_workflow(
    request: Request, response: Response, body: CreateWorkflowRequest
) -> CreateWorkflowResponse:
    """``201`` on creation, ``200`` on an idempotent replay (Story 4.8 AC1).

    ``warnings`` may be non-empty on either (never blocking, AC4).

    The two codes are not cosmetic, and the reasoning is the one already
    written for ``cancel_workflow_run`` below: the status code is what tells
    a client which of the two happened without inspecting the body. A replay
    created nothing, so announcing ``201 Created`` would make "I created a
    workflow" and "I got my earlier one back" indistinguishable — and the
    ``workflow_id`` returned is the FIRST request's, not a new one.
    ``idempotent_replay`` carries the same answer in the body.

    Errors :
    * 422 — duplicate ``node_id``, dangling edge, cycle, unknown
      ``agent_template_id``, invalid branching-condition syntax, or a
      condition referencing a variable not exposed by the emitting node's
      ``output_contract.core`` (RFC 7807). Note the order: since Story 4.8 the
      structural checks run before any DB read, so a DAG that is both cyclic
      and references an unknown template reports the cycle.
    * 409 — a fingerprint collision whose original row could not be re-read,
      i.e. it was deleted between the failed INSERT and the lookup. Surfaced
      rather than retried in a loop: a retry would race the same deletion
      forever. No DELETE endpoint exists today, so nothing is known to
      trigger it — and, since review P2, nothing ELSE can either: the repo
      translates only a `uq_workflow_request_fingerprint` unique violation
      into this path, and lets every other integrity failure surface as the
      500 it is instead of borrowing this code.
    * 503 — lifespan state missing (session factory), or (Story 4.14 AC2) a
      concurrent creation held the row lock this request was waiting on for
      longer than ``AGENTIVE_WORKFLOW_CREATE_LOCK_TIMEOUT_S`` — retriable,
      unlike the 409 above.
    """
    service = _build_workflow_service(request)
    result = await service.create_workflow(
        name=body.name,
        nodes=body.nodes,
        edges=body.edges,
        tenant_id=None,  # Story 4.1 anti-scope — single-tenant MVP.
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    return result


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
async def get_workflow_routing_stats(
    workflow_id: UUID,
    request: Request,
    window_days: int | None = Query(
        default=None,
        ge=1,
        le=3650,
        description=(
            "Trailing window, in days, over which to aggregate (Story 4.10 "
            "AC4). Defaults to AGENTIVE_WORKFLOW_ROUTING_STATS_WINDOW_DAYS "
            "(90) when omitted."
        ),
    ),
) -> RoutingStatsResponse:
    """``% routages déterministes vs LLM``, aggregated in SQL over runs of
    ``workflow_id`` started within the last ``window_days`` days — never a
    Prometheus label (unbounded cardinality, Dev Notes § Divergences
    assumées).

    Story 4.10 AC4 — previously aggregated over EVERY run ever, a cost
    proportional to a number the CALLER controls (how many times they hit
    ``POST /runs``), not the operator. Bounded to a trailing window instead
    of a materialized rollup: simpler, and honest about what the number now
    means (a recent proportion, not an all-time one) rather than papering
    over the change with a cache.

    Errors:
    * 404 — ``workflow_id`` unknown (the URL's primary resource, mirror the
      404-vs-422 decision of Story 4.2's ``POST /workflows/{workflow_id}/runs``,
      not Story 4.1's ``POST /workflows``).
    * 422 — ``window_days`` outside ``[1, 3650]``.
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

    effective_window_days = window_days or settings.workflow_routing_stats_window_days
    since = datetime.now(UTC) - timedelta(days=effective_window_days)

    workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
    runs_counted, deterministic, llm_escalated = await workflow_run_repo.aggregate_routing_modes(
        workflow_id, since=since
    )
    total = deterministic + llm_escalated
    deterministic_pct = round(deterministic / total * 100, 1) if total > 0 else None
    return RoutingStatsResponse(
        workflow_id=workflow_id,
        window_days=effective_window_days,
        runs_counted=runs_counted,
        deterministic=deterministic,
        llm_escalated=llm_escalated,
        deterministic_pct=deterministic_pct,
    )


@router.get(
    "/workflows/{workflow_id}/handoff-stats",
    response_model=HandoffStatsResponse,
    summary="Aggregate handoff-summary token reduction for a workflow (Story 4.7 AC3)",
)
async def get_workflow_handoff_stats(workflow_id: UUID, request: Request) -> HandoffStatsResponse:
    """Token-reduction from handoff summaries, aggregated in SQL over every
    run of ``workflow_id`` — mirror ``get_workflow_routing_stats`` exactly,
    never a Prometheus label (unbounded cardinality, same rule as above).

    Errors:
    * 404 — ``workflow_id`` unknown.
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
    (
        runs_counted,
        raw_tokens_replaced,
        summary_tokens,
    ) = await workflow_run_repo.aggregate_token_reduction(workflow_id)
    reduction_ratio_pct = (
        round((1 - summary_tokens / raw_tokens_replaced) * 100, 1)
        if raw_tokens_replaced > 0
        else None
    )
    return HandoffStatsResponse(
        workflow_id=workflow_id,
        runs_counted=runs_counted,
        raw_tokens_replaced=raw_tokens_replaced,
        summary_tokens=summary_tokens,
        reduction_ratio_pct=reduction_ratio_pct,
    )


# ─── Story 5.7 — lire ce qu'un run a produit, sans ouvrir `psql` ───
#
# `node_outputs` est une clé d'état LangGraph, et jusqu'à cette story elle
# n'était projetée NULLE PART en HTTP : ni dans la frame SSE (à dessein, cf
# `_state_event`), ni ailleurs. La sortie complète est LUE À LA DEMANDE dans
# le checkpointer plutôt que recopiée dans une colonne — arbitrage complet et
# options rejetées dans `docs/decisions/run-node-output-exposure.md`.
#
# Toute la connaissance de l'API LangGraph tient dans `_read_node_outputs`
# ci-dessous, et c'est délibéré : `langgraph` est pinné strictement
# (`==1.1.8`), donc si cette API bouge, un seul endroit bouge.


async def _read_node_outputs(request: Request, run_id: UUID) -> tuple[dict[str, Any] | None, bool]:
    """``(node_outputs, checkpointer_reachable)`` — trois états, pas deux.

    | Retour | Ce que ça veut dire |
    |---|---|
    | ``({...}, True)`` | le fil a été lu ; un dict vide est alors un FAIT (ce run n'a encore rien produit), pas une ignorance |
    | ``(None, True)``  | le checkpointer a répondu qu'il n'a **aucun** fil pour ce run : purgé par la rétention de la Story 4.10, ou run qui n'a pas atteint son premier superstep |
    | ``(None, False)`` | le checkpointer n'a **pas pu être consulté** : non câblé, appel en échec, ou réponse d'une forme qu'on ne sait pas lire |

    **Le second membre est la correction centrale de la revue.** La première
    version rendait un `None` unique pour les trois cas, et l'appelant
    choisissait ensuite entre `preview` et `unavailable` d'après la PRÉSENCE
    d'un aperçu en base — c'est-à-dire d'après une propriété de la row, pas
    d'après la cause. Conséquence : un pool saturé sur un run qui avait un
    aperçu se lisait « fil LangGraph purgé, rien n'est reprenable », et un
    opérateur cessait de réessayer alors qu'une requête une seconde plus tard
    aurait tout rendu. L'ADR décrivait déjà la bonne table ; le code ne la
    réalisait pas.

    Une forme inattendue (`channel_values` ou `node_outputs` qui ne sont pas
    des dicts) compte comme **non consultable** et non comme « vide » : c'est
    une ignorance, et la rendre comme une lecture réussie affirmerait qu'un
    run n'a rien produit alors qu'on n'en sait rien.

    L'exception est avalée pour la même raison que dans ``_reload_run_safely``
    : cette lecture est un CONFORT de diagnostic, et faire tomber la réponse
    entière — statut, métriques, Mise en Place — parce qu'un pool est saturé
    priverait l'opérateur de tout ce qui ne dépend pas d'elle. Le drapeau
    rendu ici est ce qui empêche cet aveuglement d'être silencieux.
    """
    checkpointer = getattr(request.app.state, "workflow_checkpointer", None)
    if checkpointer is None:
        _log.warning("workflow_engine.run_node_outputs_checkpointer_unwired", run_id=str(run_id))
        return None, False
    try:
        checkpoint = await checkpointer.aget({"configurable": {"thread_id": str(run_id)}})
    except Exception:
        _log.warning("workflow_engine.run_node_outputs_read_failed", run_id=str(run_id))
        return None, False
    if checkpoint is None:
        # Réponse NORMALE du checkpointer : il n'a pas de fil pour ce run.
        return None, True
    # JSONB/état libre : `isinstance` avant d'émettre, comme partout ailleurs
    # sur ce routeur. Une forme qu'on ne sait pas lire est journalisée — sans
    # quoi une divergence d'API rendrait `none` indéfiniment, sans signal.
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("channel_values"), dict):
        _log.warning(
            "workflow_engine.run_node_outputs_unexpected_shape",
            run_id=str(run_id),
            got=type(checkpoint).__name__,
        )
        return None, False
    node_outputs = checkpoint["channel_values"].get("node_outputs")
    if node_outputs is None:
        return {}, True
    if not isinstance(node_outputs, dict):
        _log.warning(
            "workflow_engine.run_node_outputs_unexpected_channel",
            run_id=str(run_id),
            got=type(node_outputs).__name__,
        )
        return None, False
    return node_outputs, True


#: Un fragment de clé d'API en FIN de chaîne, quelle que soit sa longueur.
#:
#: `redact_secrets` exige 30 caractères après le préfixe — un plancher assumé
#: chez lui, pour ne pas caviarder des jetons courts au hasard. Mais l'aperçu
#: applicatif est persisté par `_preview()` SANS caviardage puis coupé à 500
#: caractères : une clé que la coupe traverse arrive ici amputée, sous le
#: plancher, et ressortait donc en clair (jusqu'à 29 caractères). Ici la
#: position est l'indice qui manquait — en fin d'un texte qu'on sait tronqué,
#: un préfixe de clé n'est jamais un jeton complet.
#:
#: Le correctif de fond est de caviarder À L'ÉCRITURE, avant la coupe ;
#: il est porté par la story `9-10-caviardage-sorties-persistees`.
_TRUNCATED_KEY_TAIL = re.compile(r"(?:sk-ant-|sk-proj-|sk-|pa-)[A-Za-z0-9_\-]*$")


def _json_safe(value: Any, _seen: frozenset[int] = frozenset()) -> Any:
    """Rendre une valeur sérialisable sans jamais lever.

    Chemin de SECOURS : il ne s'exécute que lorsque `json.dumps` a échoué, ce
    qui n'arrive pas sur une sortie nominale (elle vient d'un `json.loads`,
    donc ses clés sont des chaînes). Trois cas connus, tous constatés par la
    revue :

    * un flottant non fini — `NaN`/`Infinity` sortaient en jetons nus, que
      `JSON.parse` refuse, alors que l'Epic 6 rendra ce champ dans une UI ;
    * une clé de dict non sérialisable (`tuple`, `frozenset`) — `default=`
      ne s'applique JAMAIS aux clés, donc le `TypeError` n'était pas rattrapé
      et faisait tomber la réponse ENTIÈRE : statut, métriques, Mise en Place
      et les sorties de tous les AUTRES nodes avec ;
    * une référence circulaire.

    `_seen` porte les `id()` des conteneurs en cours de visite, ce qui coupe
    un cycle sans fausser un partage légitime (le même sous-objet référencé
    deux fois côte à côte reste rendu deux fois).
    """
    if isinstance(value, float) and not isfinite(value):
        return str(value)
    if isinstance(value, dict):
        if id(value) in _seen:
            return "<circular>"
        nested = _seen | {id(value)}
        return {
            (key if isinstance(key, str) else str(key)): _json_safe(item, nested)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if id(value) in _seen:
            return "<circular>"
        nested = _seen | {id(value)}
        return [_json_safe(item, nested) for item in value]
    return value


def _render_node_output(value: Any) -> str:
    """Rendu JSON d'une sortie de node, caviardé — dans CET ordre.

    Caviardé AVANT toute troncature faite ICI, jamais après : `redact_secrets`
    change la longueur, donc l'appliquer après rendrait les `next_offset` d'une
    page à l'autre incohérents. (Une coupure faite EN AMONT par `_preview()`
    échappe par construction à cette garantie — c'est ce que
    `_TRUNCATED_KEY_TAIL` rattrape sur le chemin de l'aperçu.)

    NFR9 : la valeur vient d'un LLM, elle n'est ni exécutée, ni interprétée,
    ni re-parsée — seulement rendue.

    `allow_nan=False` plutôt que le défaut permissif : le défaut émet `NaN` et
    `Infinity`, qui ne sont pas du JSON et que `json.loads` de Python accepte
    quand même — de sorte qu'un test écrit en Python ne l'aurait jamais vu.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)
    except TypeError, ValueError:
        text = json.dumps(_json_safe(value), ensure_ascii=False, default=str, allow_nan=False)
    return redact_secrets(text)


def _slice_node_output(
    node_id: str,
    text: str,
    *,
    source: NodeOutputSource,
    offset: int,
    limit: int,
    engine_truncated: bool = False,
) -> NodeOutputOut:
    """Une page de sortie de node, qui DIT ce qu'elle a coupé.

    Deux questions distinctes, et les confondre est le défaut que la revue a
    trouvé ici :

    * **reste-t-il quelque chose dans ce que le serveur tient ?** → ``next_offset``.
      La réponse ne dépend PAS de la provenance : un aperçu applicatif se
      pagine aussi bien qu'une sortie de checkpointer, puisque dans les deux
      cas le texte entier est en main au moment du découpage.
    * **l'original avait-il déjà été coupé avant d'arriver ici ?** →
      ``engine_truncated``, vrai seulement pour un aperçu que ``_preview()``
      a tronqué à 500 caractères.

    La première version liait ``next_offset`` à la provenance : un aperçu de
    501 caractères demandé avec ``?limit=64`` répondait « il manque la suite
    et elle n'est plus lisible » alors que ``?offset=64`` la servait
    parfaitement. La réponse mentait sur une coupure que le serveur venait de
    faire lui-même.

    Les trois lignes du contrat :

    ===================  ===============  ==================================
    ``truncated``        ``next_offset``  Ce que ça veut dire
    ===================  ===============  ==================================
    ``False``            ``None``          c'est tout, il ne manque rien
    ``True``             ``N``             il reste de quoi lire, reprendre à N
    ``True``             ``None``          tout ce que le serveur tient a été
                                           rendu, et l'ORIGINAL avait déjà été
                                           coupé avant d'arriver (``source``
                                           vaut ``preview`` : le fil LangGraph
                                           n'est plus là)
    ===================  ===============  ==================================

    ``engine_truncated`` est borné par ``offset < total`` : au-delà de la fin
    du texte, une page vide n'a rien coupé et ne doit rien annoncer. Sans
    cette garde, ``?offset=600`` sur un aperçu de 501 caractères rendait une
    page vide qui se déclarait tronquée.
    """
    total = len(text)
    chunk = text[offset : offset + limit]
    consumed = offset + len(chunk)
    more_here = consumed < total
    return NodeOutputOut(
        node_id=node_id,
        output=chunk,
        source=source,
        total_chars=total,
        returned_chars=len(chunk),
        offset=offset,
        truncated=more_here or (engine_truncated and offset < total),
        next_offset=consumed if more_here else None,
    )


def _engine_cut_it(text: str) -> bool:
    """Vrai si ``_preview()`` a tronqué ce texte avant de le persister.

    Le marqueur est fiable, et c'est vérifiable plutôt que supposé : l'aperçu
    est un rendu ``json.dumps``, qui se termine toujours par ``"``, ``}``,
    ``]`` ou un littéral (``42``, ``null``, ``true``). Une valeur non tronquée
    ne peut donc pas finir par ``…`` — même une chaîne qui en contient un, car
    le rendu la referme par un guillemet. Seul ``_preview`` ajoute ce
    caractère en dernière position (``service.py:714``).
    """
    return text.endswith("…")


def _preview_of(run: WorkflowRun) -> dict[str, Any]:
    """L'aperçu applicatif de la row, ou ``{}`` — jamais une exception.

    ``checkpoint`` est un JSONB libre écrit par plusieurs versions du moteur :
    ``isinstance`` avant de lire, comme partout ailleurs sur ce routeur.
    """
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    preview = checkpoint.get("node_outputs_preview")
    return preview if isinstance(preview, dict) else {}


def _preview_text(raw: Any) -> str:
    """Un aperçu tel qu'il doit sortir : caviardé, et rendu s'il ne l'est pas.

    ``_preview()`` produit toujours une chaîne (un rendu ``json.dumps``), mais
    ce JSONB a été écrit par plusieurs versions du moteur — d'où le repli.
    """
    if not isinstance(raw, str):
        return _render_node_output(raw)
    return _TRUNCATED_KEY_TAIL.sub("[REDACTED]", redact_secrets(raw))


def _node_outputs_page(
    run: WorkflowRun,
    node_outputs: dict[str, Any] | None,
    *,
    limit: int,
) -> tuple[list[NodeOutputOut], NodeOutputsSource]:
    """Les sorties de TOUS les nodes exécutés, plus d'où elles viennent.

    Ordre d'insertion préservé : ``node_outputs`` est un canal d'état que
    LangGraph alimente dans l'ordre d'achèvement (la même propriété dont
    ``_latest_node_id`` dépend déjà dans ``service.py``).

    **Les deux sources sont FUSIONNÉES par node, pas choisies globalement.**
    Un node dont la décision de routage a échoué a bien produit une sortie —
    ``RoutingDecisionFailedError`` la reporte dans l'aperçu applicatif
    (``service.py``, IG3) — mais LangGraph jette l'update d'un node qui lève,
    donc elle n'est PAS dans le canal d'état. La version précédente n'itérait
    que le canal : la route de détail taisait cette sortie alors que
    ``node_statuses``, dans la même réponse, annonçait le node, et que la
    route par node la servait. Chaque entrée porte sa propre ``source``, ce
    qui rend la fusion lisible plutôt que silencieuse.
    """
    preview = _preview_of(run)
    pages: list[NodeOutputOut] = []
    seen: set[str] = set()
    for node_id, output in (node_outputs or {}).items():
        key = str(node_id)
        seen.add(key)
        pages.append(
            _slice_node_output(
                key,
                _render_node_output(output),
                source="checkpointer",
                offset=0,
                limit=limit,
            )
        )
    for node_id, raw in preview.items():
        key = str(node_id)
        if key in seen:
            continue
        text = _preview_text(raw)
        pages.append(
            _slice_node_output(
                key,
                text,
                source="preview",
                offset=0,
                limit=limit,
                engine_truncated=_engine_cut_it(text),
            )
        )
    if node_outputs is not None:
        return pages, "checkpointer" if node_outputs else ("preview" if pages else "none")
    if preview:
        return pages, "preview"
    return [], "none"


def _resolved_output_limit(limit: int | None) -> int:
    """Le plafond effectif d'une réponse, ou un ``422`` qui NOMME le réglage.

    Rogner en silence un `limit` trop grand rendrait une page plus courte que
    demandée sans le dire — la troncature silencieuse, encore, déguisée en
    politesse. Le refus nomme la variable d'environnement, comme le fait la
    garde `AGENTIVE_ALLOW_MCP_REGISTRATION` du provisioning.
    """
    ceiling = settings.run_node_output_max_chars
    if limit is None:
        return ceiling
    if limit > ceiling:
        raise ValidationError(
            detail=(
                f"limit ({limit}) exceeds this deployment's per-page ceiling "
                f"({ceiling}); raise AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS or "
                f"paginate with next_offset"
            ),
            context={"limit": limit, "ceiling": ceiling},
        )
    return limit


def _redacted_jsonb(value: dict[str, Any]) -> dict[str, Any]:
    """Un JSONB libre, caviardé avant de sortir (NFR9).

    `metrics` porte `tool_names`, `model_used` et `contract_problems`, tous
    d'origine LLM ou fournisseur : le diff proclamait NFR9 sur les sorties de
    node et l'abandonnait sur ce champ de la MÊME réponse. (`last_error`, lui,
    est bien caviardé à la source par `_mark_failed`, `service.py` — il n'a
    pas besoin d'un second passage.)

    Aller-retour par le rendu JSON plutôt qu'un parcours maison : c'est
    `redact_secrets`, déjà éprouvé, qui fait le travail, et le coût est
    négligeable sur un objet de cette taille. Un échec rend `{}` et le dit :
    perdre des métriques de diagnostic est préférable à laisser passer un
    secret, et le cas est de toute façon inatteignable sur un `metrics`
    produit par `_aggregate_metrics`.
    """
    try:
        rendered = json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)
        coerced = json.loads(redact_secrets(rendered))
    except TypeError, ValueError:
        _log.warning("workflow_engine.run_detail_metrics_unrenderable")
        return {}
    return coerced if isinstance(coerced, dict) else {}


def _coerce_or_none[ModelT: BaseModel](model: type[ModelT], payload: Any) -> ModelT | None:
    """Valider un JSONB persisté contre son schéma, ou rendre ``None``.

    Les colonnes `mise_en_place` et `acknowledgement` sont `NULL` pour tout run
    antérieur aux Stories 4.5 et 5.1, et rien n'empêche une row écrite par une
    version plus ancienne du moteur de ne plus valider. Faire tomber la
    réponse ENTIÈRE — statut, métriques, sorties de nodes — pour un rapport
    annexe illisible serait exactement le défaut que la revue de la 5.2 a
    nommé sur `_aggregate_metrics`. La clé reste présente à `null`, jamais
    omise (leçon de l'accusé de réception, Story 5.1).
    """
    if not isinstance(payload, dict):
        return None
    try:
        return model.model_validate(payload)
    except PydanticValidationError:
        _log.warning("workflow_engine.run_detail_payload_unreadable", schema=model.__name__)
        return None


async def _require_run(request: Request, run_id: UUID) -> WorkflowRun:
    """Le run du tenant appelant, ou un ``404`` — jamais un ``403``.

    Un ``403`` confirmerait l'existence de la row à quelqu'un qui n'a pas le
    droit de la voir : run inconnu et run d'un autre tenant sont
    INDISTINCTS ici, et la RLS fait le filtrage (``with_tenant``), pas une
    comparaison en Python qui aurait pu être oubliée.
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
    return run


@router.get(
    "/workflows/runs/{run_id}",
    response_model=RunDetailResponse,
    summary="Read a run's state, metrics and node outputs (Story 5.7 AC1/AC2)",
)
async def get_workflow_run(
    run_id: UUID,
    request: Request,
    limit: int | None = Query(
        default=None,
        ge=1,
        description=(
            "Characters of EACH node's output to return. Defaults to this "
            "deployment's AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS. A value above "
            "that ceiling is a 422 naming the setting and its value, never "
            "silently clamped — paginate on the per-node route instead."
        ),
    ),
) -> RunDetailResponse:
    """Ce qu'un run a fait, et ce que chaque agent a réellement produit.

    **Ce qu'elle rend** : l'état du run (`status`, `started_at`, `ended_at`,
    `correlation_id`), le bloc `metrics` — dont `per_node`, qui porte les
    quatre compteurs d'outils de l'AC2 de la Story 5.2 (`tool_calls`,
    `tool_loop_iterations`, `tool_failures`, `tool_names`) —, le rapport de
    Mise en Place, l'accusé de réception, la progression applicative
    (`last_node_id`, `node_statuses`, `control_signal`, `last_error`), et un
    extrait de la sortie de chaque node exécuté.

    **Ce qu'elle NE rend PAS** : le checkpoint technique LangGraph, les blocs
    `routing_decisions`/`handoffs` de `workflow_runs.checkpoint`
    (`/routing-stats` et `/handoff-stats` les agrègent), et la sortie
    complète d'un node — celle-là se lit page par page sur
    `GET /workflows/runs/{run_id}/nodes/{node_id}/output`.

    **Cette route ne change pas le flux SSE.** `node_outputs_preview` reste
    hors de la frame `state` (cf `_state_event`) : c'est ce qui garde vraie la
    promesse « première frame en moins de 2 s » de l'AC2 de la Story 4.2. Le
    détail vit ici, dans une route dédiée qu'on appelle quand on le veut.

    Errors:
    * 404 — `run_id` inconnu OU appartenant à un autre tenant, indistinctement.
    * 422 — `limit` au-delà du plafond du déploiement.
    * 503 — état de lifespan manquant (session factory).
    """
    effective_limit = _resolved_output_limit(limit)
    # ⚠️ ORDRE VOLONTAIRE : le checkpointer est lu AVANT la row.
    #
    # Les deux lectures sont deux transactions, donc deux instants. Dans
    # l'ordre inverse, un run qui achève un superstep entre les deux rendait
    # une réponse où `node_outputs` portait un node absent de `node_statuses`
    # — une incohérence visible, dans le même corps. Lire la row en second
    # garantit qu'elle est au moins aussi fraîche que les sorties : la
    # progression applicative peut être EN AVANCE sur elles, jamais en
    # retard. Le coût est une lecture de checkpointer sur le chemin d'un
    # `run_id` inconnu, ce que le `404` d'après rattrape sans rien divulguer.
    node_outputs, checkpointer_reachable = await _read_node_outputs(request, run_id)
    run = await _require_run(request, run_id)
    outputs, source = _node_outputs_page(run, node_outputs, limit=effective_limit)

    # JSONB libres, écrits par plusieurs versions du moteur : `isinstance`
    # avant d'émettre. La revue de la Story 5.2 a trouvé `_aggregate_metrics`
    # qui faisait tomber l'agrégation d'un run ENTIER sur une valeur
    # malformée ; une projection HTTP qui ferait tomber la réponse entière
    # pour la même raison serait la même faute, une couche plus haut.
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    metrics = run.metrics if isinstance(run.metrics, dict) else {}
    last_node_id = checkpoint.get("last_node_id")
    node_statuses = checkpoint.get("node_statuses")
    last_error = checkpoint.get("last_error")
    control_signal = getattr(run, "control_signal", None)
    mise_en_place = run.mise_en_place if isinstance(run.mise_en_place, dict) else None
    acknowledgement = getattr(run, "acknowledgement", None)

    return RunDetailResponse(
        run_id=run.id,
        workflow_id=run.workflow_id,
        status=run.status,
        started_at=run.started_at,
        ended_at=run.ended_at,
        correlation_id=run.correlation_id,
        metrics=_redacted_jsonb(metrics),
        # `model_validate` et non une construction champ par champ : le
        # rapport persisté a la forme du schéma, et une row antérieure à la
        # Story 4.5 (ou malformée) doit rendre `null` plutôt que faire tomber
        # une réponse dont tout le reste est bon.
        mise_en_place=_coerce_or_none(MiseEnPlaceReportOut, mise_en_place),
        acknowledgement=_coerce_or_none(AcknowledgementOut, acknowledgement),
        last_node_id=last_node_id if isinstance(last_node_id, str) else None,
        # `isinstance` avant d'émettre, pas `str()` : stringifier faisait
        # sortir un `repr` Python (`"{'retries': 2}"`, `"None"`) dans un champ
        # typé `dict[str, str]` — une conversion silencieuse là où le
        # commentaire d'à côté revendique un filtre. Une valeur non scalaire
        # est omise, ce qui est le comportement que `_state_event` a déjà.
        node_statuses=(
            {str(nid): value for nid, value in node_statuses.items() if isinstance(value, str)}
            if isinstance(node_statuses, dict)
            else {}
        ),
        control_signal=control_signal if isinstance(control_signal, str) else None,
        last_error=last_error if isinstance(last_error, str) else None,
        node_outputs=outputs,
        node_outputs_source=source,
        checkpointer_reachable=checkpointer_reachable,
        checkpoint_purged_at=getattr(run, "checkpoint_purged_at", None),
    )


@router.get(
    "/workflows/runs/{run_id}/nodes/{node_id}/output",
    response_model=RunNodeOutputResponse,
    summary="Read ONE node's output, page by page (Story 5.7 AC2)",
)
async def get_workflow_run_node_output(
    run_id: UUID,
    node_id: str,
    request: Request,
    offset: int = Query(
        default=0,
        ge=0,
        description="Character offset to start at — feed back the previous page's next_offset.",
    ),
    limit: int | None = Query(
        default=None,
        ge=1,
        description=(
            "Characters to return. Defaults to this deployment's "
            "AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS; above that ceiling is a 422 "
            "naming the setting and its value. The response echoes the bound "
            "it actually applied in `limit`."
        ),
    ),
) -> RunNodeOutputResponse:
    """La route qui rend vraie « la sortie complète est atteignable » : page
    après page, `next_offset` après `next_offset`, sans plafond sur le total
    lu — seulement sur ce qu'une réponse porte.

    `truncated: true` avec `next_offset: null` n'est pas une contradiction :
    c'est le cas où tout ce que le serveur tient a été rendu ET où l'original
    avait déjà été coupé avant d'arriver — `output.source` vaut alors
    `preview`, le fil LangGraph n'étant plus là pour servir la suite.

    Sert aussi les nodes que le canal d'état LangGraph n'a pas : un node dont
    la décision de routage a échoué a produit une sortie que seul l'aperçu
    applicatif porte. Ce repli n'implique donc PAS que le checkpointer soit
    injoignable — `checkpointer_reachable` est le champ qui le dit.

    Errors:
    * 404 — `run_id` inconnu/d'un autre tenant, ou `node_id` sans sortie dans
      ce run. Quand le checkpointer n'a pas pu être consulté, le message le
      DIT au lieu d'affirmer une absence qu'on n'est pas en position de
      constater.
    * 422 — `limit` au-delà du plafond du déploiement.
    * 503 — état de lifespan manquant.
    """
    effective_limit = _resolved_output_limit(limit)
    # Même ordre que la route de détail, pour la même raison — cf son corps.
    node_outputs, checkpointer_reachable = await _read_node_outputs(request, run_id)
    run = await _require_run(request, run_id)

    source: NodeOutputSource = "checkpointer"
    engine_truncated = False
    if node_outputs is not None and node_id in node_outputs:
        text = _render_node_output(node_outputs[node_id])
    else:
        preview = _preview_of(run)
        if node_id not in preview:
            raise NotFoundError(
                detail=(
                    f"Node '{node_id}' has no recorded output in run '{run_id}'"
                    if checkpointer_reachable
                    else (
                        f"Node '{node_id}' cannot be read: the workflow checkpointer "
                        f"could not be consulted, and run '{run_id}' carries no "
                        f"applicative preview for it — this is an outage, not an "
                        f"empty node."
                    )
                ),
                context={
                    "run_id": str(run_id),
                    "node_id": node_id,
                    "checkpointer_reachable": checkpointer_reachable,
                },
            )
        text = _preview_text(preview[node_id])
        source = "preview"
        engine_truncated = _engine_cut_it(text)

    return RunNodeOutputResponse(
        run_id=run.id,
        node_id=node_id,
        limit=effective_limit,
        output=_slice_node_output(
            node_id,
            text,
            source=source,
            offset=offset,
            limit=effective_limit,
            engine_truncated=engine_truncated,
        ),
        checkpointer_reachable=checkpointer_reachable,
        # Rendu INCONDITIONNELLEMENT, comme sur la route de détail : c'est un
        # fait sur le RUN, pas sur cette réponse. La version précédente le
        # conditionnait à `source == "preview"`, ce qui donnait deux
        # sémantiques à la même colonne sur deux routes de la même story —
        # exactement le motif que T2.2 invoquait pour l'éviter.
        checkpoint_purged_at=getattr(run, "checkpoint_purged_at", None),
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


@router.post(
    "/workflows/runs/{run_id}/retract",
    response_model=RunControlResponse,
    status_code=status.HTTP_200_OK,
    summary="Retract a pending, not-yet-observed pause/cancel request (Story 4.9 AC6)",
)
async def retract_workflow_run_control(run_id: UUID, request: Request) -> RunControlResponse:
    """``200`` — applied immediately: unlike ``pause``/``cancel``, there is no
    driver-side effect to defer, only a column to clear before anyone reads it.

    Legal only from ``running`` AND only while a ``pause``/``cancel`` is
    still pending (``control_signal IS NOT NULL``) — an operator who asked
    for the wrong thing a moment ago and wants to undo it before the driver
    ever observes the request.

    Errors:
    * 404 — ``run_id`` unknown.
    * 409 — the run is not ``running``, or nothing is currently pending (the
      driver already observed it, or none was ever requested). The RFC 7807
      ``context`` carries the same fields the other three control routes do.
    * 503 — lifespan state missing.
    """
    service = _build_execution_service(request)
    return await service.request_run_control(run_id=run_id, action="retract", tenant_id=None)


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

    Story 5.1 AC2 — carries ``acknowledgement`` as well. This frame is sent
    the instant a client connects, which is what makes "première frame en
    moins de 2 s" true regardless of how far the run has already got.
    """
    checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
    payload: dict[str, Any] = {"run_id": str(run.id), "status": run.status}
    # Story 5.1 AC2 — l'accusé de réception, sur le chemin de RATTRAPAGE.
    # Lu sur la colonne dédiée et non dans `checkpoint` : ce dernier est
    # remplacé en entier par `_sync_checkpoint` dès le premier node, donc un
    # accusé qui y aurait vécu aurait disparu exactement au moment où ce
    # rattrapage sert. Même posture défensive que les champs ci-dessous —
    # `isinstance` avant d'émettre, JSONB libre.
    # La clé est TOUJOURS présente, `null` si la colonne l'est — et non
    # omise. Les trois surfaces qui rendent cet accusé (le 201, l'event
    # `started`, cette frame) doivent avoir la même FORME, sinon un client
    # écrit sur celle du 201 prend un `KeyError` au lieu d'un `null` sur tout
    # run antérieur à la migration. Omettre la clé rendait aussi les tests
    # illisibles : ils tombaient en `KeyError` plutôt qu'en échec nommé.
    acknowledgement = getattr(run, "acknowledgement", None)
    payload["acknowledgement"] = acknowledgement if isinstance(acknowledgement, dict) else None
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
        # Story 4.9 AC6/T6.1 — `control_requested_at` was written on every
        # pause/cancel request and read by NOTHING: no SSE frame, no 409
        # context, no metric. A client watching this stream had no way to
        # tell "just asked" from "asked 25 minutes ago and the driver may be
        # dead" (the ~1548s worst case documented in the run-control
        # runbook). Only emitted alongside `control_signal` — the two are
        # always written and cleared together (`clear_control`), so one
        # without the other never happens.
        control_requested_at = getattr(run, "control_requested_at", None)
        if control_requested_at is not None:
            # Explicit `.isoformat()`: the `default=str` fallback below would
            # render a `datetime` space-separated with no `T`, diverging from
            # the ISO-8601 the 409 context on this same surface emits.
            payload["control_requested_at"] = control_requested_at.isoformat()
    if reason is not None:
        payload["reason"] = reason
    return {"event": "state", "data": json.dumps(payload, default=str)}


async def _reload_run_safely(
    workflow_run_repo: WorkflowRunRepo, run_id: UUID
) -> WorkflowRun | None:
    """Re-read the row for the SSE loop, never raising into the generator.

    This is the ONLY database call on the streaming path, and it was the only
    unguarded one (review of 2026-09-12). The loop's single `except` catches
    the BUILTIN `TimeoutError` around `queue.get()`, and
    `sqlalchemy.exc.TimeoutError` — what a pool exhaustion raises — is NOT a
    subclass of it (`SQLAlchemyError` -> `Exception`, verified). Neither is
    `OperationalError` from a failover or a pgbouncer restart. So a blip
    propagated straight out of the generator, long after the response headers
    were sent: no RFC 7807 body is possible there, the client just sees the
    connection end — precisely the silent end the deadline block below
    declares unacceptable.

    Worse, it was fleet-wide and self-amplifying: every open stream re-polls
    on the same cadence, so one ten-second hiccup dropped all of them at once,
    and browser `EventSource` reconnects them ~3 s later into the route
    handler's own read.

    Swallowing is right here because the poll is a RECONCILIATION, not the
    source of truth: the next tick redoes it, and the events keep flowing in
    the meantime.
    """
    try:
        return await workflow_run_repo.get_by_id(run_id)
    except Exception:
        _log.warning("workflow_engine.sse_status_repoll_failed", run_id=str(run_id))
        return None


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
        current = await _reload_run_safely(workflow_run_repo, run_id)
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
        deadline = loop.time() + _max_stream_duration_s()
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
                    current = await _reload_run_safely(workflow_run_repo, run_id)
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
        current = await _reload_run_safely(workflow_run_repo, run_id)
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
