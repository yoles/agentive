"""Workflow lifecycle events — Epic 4 Workflow Orchestration Engine.

Naming convention follows Story 1.7 pattern ``{module}.{object}.{verb}``.
Events shipped to date:

* ``workflow_engine.workflow.created`` (Story 4.1) — emitted after a new
  ``workflows`` row is committed via ``WorkflowService.create_workflow``.
* ``workflow_engine.workflow_run.started`` (Story 4.2 AC1) — a run starts
  executing (``WorkflowExecutionService.start_run``).
* ``workflow_engine.workflow_run.step_completed`` (Story 4.2 AC2) — one node
  finishes successfully.
* ``workflow_engine.workflow_run.resumed`` (Story 4.2 AC3) — the recovery
  worker resumes an orphaned run from its last LangGraph checkpoint.
* ``workflow_engine.workflow_run.completed`` (Story 4.2 AC4) — a run reaches
  ``END`` on every branch.
* ``workflow_engine.workflow_run.failed`` (Story 4.2 AC4) — a node raised a
  non-recoverable error, terminating the run.
* ``workflow_engine.workflow_run.routing_escalated`` (Story 4.3 AC2/AC3) —
  a routing decision point could not be resolved deterministically (DSL
  silent, no rule cleared the confidence threshold) and escalated to the
  lightweight LLM.
* ``workflow_engine.workflow_run.mise_en_place_bypassed`` (Story 4.5 AC3) —
  the caller started a run with ``force=true`` while one or more pre-workflow
  checks were failing. The first event in this repo representing an explicit
  user bypass with a stated reason (grep-confirmed at story time — no prior
  ``_bypassed``/``_forced`` event existed).
* ``workflow_engine.workflow_run.pause_requested`` / ``.cancel_requested``
  (Story 4.6 AC1) — a caller ASKED for a live run to pause or stop. The run
  is still ``running`` at this point: interruption is cooperative, so the
  driver settles the request at its next superstep boundary. These two exist
  separately from ``paused``/``cancelled`` precisely because the gap between
  request and effect is real and bounded by a node's duration — an audit
  trail that collapsed them would hide it.
* ``workflow_engine.workflow_run.paused`` (Story 4.6 AC2) — the driver
  observed a pause request and suspended the run at a committed checkpoint.
  NOT terminal: a ``resume`` restarts it from exactly there.
* ``workflow_engine.workflow_run.cancelled`` (Story 4.6 AC2) — terminal,
  either observed by the driver or applied directly to an already-``paused``
  run (which has no driver alive to observe anything).
* ``workflow_engine.llm.fallback_triggered`` (Story 1.6, typed in Story 4.6
  T4.3) — the LLM router moved to the next provider in the chain. The only
  event of this module NOT under the ``workflow_run`` entity.
* ``workflow_engine.workflow_run.mise_en_place_refused`` (Story 4.5 AC2,
  review BS2) — the symmetric case: a launch REFUSED because a check failed
  and no ``force`` was given. It exists because AC2 forbids creating a
  ``workflow_runs`` row for a refused launch, so the report has nowhere to
  be persisted on that table; the outbox is where the refusal is traced
  instead. Optional ``run_id`` (Story 4.12 AC4) — ``None`` from
  ``start_run`` (there is no run), populated from ``resume`` (Story 4.6
  ``IG2`` re-runs this same gate on an existing, ``paused`` run).
* ``workflow_engine.workflow_run.resume_mise_en_place_evaluated`` (Story
  4.12 AC3) — every ``resume`` that actually proceeds (cleanly or via
  ``force``) publishes its OWN gate outcome here. ``workflow_runs
  .mise_en_place`` (Story 4.5 AC1) stays the run's immutable LAUNCH
  record — Story 4.12 decided against overwriting it on each resume,
  which would destroy that audit artifact — so this is where "the report
  that actually authorized THIS resume" lives instead, mirroring how a
  refusal already lives in the outbox rather than on the row.

Naming note (D91 point 3): the epic's literal AC3 wording ("workflow_resumed")
does not fit ``shared/event_bus/naming.py``'s strict 3-segment
``module.entity.action`` grammar — ``workflow_engine.workflow_run.resumed``
is the compliant form, mirror of the ``workflow.created`` rename already done
in Story 4.1.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class WorkflowCreatedEvent(BaseModel):
    """Published after a new ``workflows`` row is durably committed (Story 4.1 AC2).

    Same audit-bypass pattern as the other Sprint 1-3 lifecycle events — lives
    only in ``outbox_events`` until Story 9.1 wires ``AuditEventRepo``.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow.created"

    workflow_id: UUID
    name: str = Field(min_length=1, max_length=255)
    version: int = Field(ge=1)
    node_count: int = Field(ge=0)
    actor: str = Field(default="system", description="user_id or 'system' for unattended creation")
    tenant_id: UUID | None = None


class WorkflowRunStartedEvent(BaseModel):
    """Published when a run starts executing (Story 4.2 AC1).

    ``acknowledgement`` (Story 5.1 AC2) — « Compris. Je mobilise [agents].
    ETA ~[X] min. », sous la forme ``{message, agents[], eta_minutes,
    eta_source}``. Même contenu que la colonne ``workflow_runs.acknowledgement``
    et que la frame SSE ``state`` : un client déjà attaché le reçoit ici, un
    client qui s'attache plus tard le reçoit en rattrapage. Les deux chemins
    doivent dire la même chose, sinon le client voit l'accusé changer sous
    ses yeux selon l'instant où il s'est connecté.

    ``None`` pour un run antérieur à cette story.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.started"

    run_id: UUID
    workflow_id: UUID
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    acknowledgement: dict[str, Any] | None = None
    tenant_id: UUID | None = None


class WorkflowRunStepCompletedEvent(BaseModel):
    """Published after each node terminates successfully (Story 4.2 AC2).

    ``status`` is always ``"success"`` — a node failure is a separate
    ``workflow_run.failed`` event (T6.5), not a "step in error" state.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.step_completed"

    run_id: UUID
    workflow_id: UUID
    node_id: str = Field(min_length=1, max_length=100)
    status: Literal["success"] = "success"
    duration_ms: int = Field(ge=0)
    tenant_id: UUID | None = None


class WorkflowRunResumedEvent(BaseModel):
    """Published by the recovery worker before relaunching an orphaned run
    (Story 4.2 AC3)."""

    event_type: ClassVar[str] = "workflow_engine.workflow_run.resumed"

    run_id: UUID
    workflow_id: UUID
    resumed_from_node_id: str | None = Field(
        default=None, description="best-effort — None if not determinable"
    )
    tenant_id: UUID | None = None


class WorkflowRunCompletedEvent(BaseModel):
    """Published when a run reaches ``END`` on every branch (Story 4.2 AC4)."""

    event_type: ClassVar[str] = "workflow_engine.workflow_run.completed"

    run_id: UUID
    workflow_id: UUID
    total_duration_ms: int = Field(ge=0)
    total_cost_usd: Decimal | None = None
    tenant_id: UUID | None = None


class WorkflowRunFailedEvent(BaseModel):
    """Published when a node raises a non-recoverable error (Story 4.2 AC4).

    Partial metrics from already-completed nodes remain aggregated in
    ``workflow_runs.metrics`` — this event only carries the failure summary.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.failed"

    run_id: UUID
    workflow_id: UUID
    failed_node_id: str | None = None
    error_summary: str = Field(max_length=500)
    #: Story 4.6 AC3 — the exception's class name (e.g.
    #: ``"LLMAllProvidersFailedError"``). Added so an alerting consumer can
    #: filter on the KIND of failure without parsing ``error_summary``'s
    #: prose, which is a redacted human string with no stable shape. This is
    #: what carries the epic's "alerte Dashboard" (Epic 7 owns the UI).
    #: Defaults to ``None`` — backward-compatible with every event already
    #: sitting in ``outbox_events``.
    error_type: str | None = Field(default=None, max_length=200)
    tenant_id: UUID | None = None


class WorkflowRunRoutingEscalatedEvent(BaseModel):
    """Published when a routing decision point escalates to the lightweight
    LLM (Story 4.3 AC2/AC3 — T7.1).

    Streamed as-is to SSE clients by the Story 4.2 endpoint's
    ``_RUN_EVENT_PATTERN`` (``workflow_engine\\.workflow_run\\.\\w+``) —
    deliberately NOT added to ``router.py``'s ``_TERMINAL_EVENT_SUFFIXES``
    (T7.4), and deliberately WITHOUT ``own_output`` in the payload (T7.3): a
    node's output can be arbitrarily large, and the SSE queue is bounded
    with drop journalisé — a fat payload here would turn an escalation burst
    into dropped frames. ``context`` carries only the scalar counts of
    :class:`~agentive_backend.features.workflow_engine.domain.routing_rules.RoutingContext`
    (never ``own_output``), which is exactly the raw material the Growth-phase
    rule-learning innovation (MVP #1, anti-scope here) needs.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.routing_escalated"

    run_id: UUID
    workflow_id: UUID
    node_id: str = Field(min_length=1, max_length=100)
    candidates: list[str] = Field(default_factory=list)
    decision_target: list[str] = Field(default_factory=list)
    confidence_best: float | None = None
    rule_id_best: str | None = None
    reason: str = Field(max_length=500)
    llm_model: str = Field(min_length=1)
    llm_latency_ms: int = Field(ge=0)
    context: dict[str, object] = Field(default_factory=dict)
    tenant_id: UUID | None = None


class WorkflowRunMiseEnPlaceBypassedEvent(BaseModel):
    """Published when a run starts with ``force=true`` despite one or more
    failing Mise en Place checks (Story 4.5 AC3) — audit trail for an
    explicit human bypass (NFR8).

    ``actor``, mirror :class:`WorkflowCreatedEvent`/:class:`WorkflowRunStartedEvent`
    — defaults to ``"system"`` under the same Sprint-1 convention (no
    authenticated-actor propagation anywhere yet), but this is precisely the
    event where a real actor will matter most once available.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.mise_en_place_bypassed"

    run_id: UUID
    workflow_id: UUID
    reason: str = Field(min_length=1, max_length=2000)
    failed_checks: list[str] = Field(default_factory=list)
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


class WorkflowRunMiseEnPlaceRefusedEvent(BaseModel):
    """Published when a launch is REFUSED because a Mise en Place check
    failed and the caller did not pass ``force`` (Story 4.5 AC2, review BS2).

    Why an event and not a row: AC2 requires that no ``workflow_runs`` row
    exist for a refused launch, so the report has nowhere to live on that
    table — AC1's "persisted whether the workflow starts or not" cannot hold
    there, and AC2 is the stronger requirement. Inventing a ``refused`` run
    row would leak a phantom run into run listings, the recovery worker's
    stale sweep, and routing stats. The outbox already carries the audit
    trail Trace Explorer consumes (NFR8/NFR15), and this story already
    publishes its sibling ``mise_en_place_bypassed`` there — a refusal is
    exactly as much an auditable decision as a bypass.

    ``run_id`` (Story 4.12 AC4) — ``None`` on the ``start_run`` path this
    event was designed for (there really is no run, exactly as the
    paragraph above says), but POPULATED on the ``resume`` path (Story 4.6
    ``IG2`` re-runs this same gate on an EXISTING, still-``paused`` run):
    without it, a workflow with several paused runs published
    indistinguishable refusals — an alerting consumer could see that
    something was refused, never which run stayed stuck. Optional rather
    than a second event type, because the two refusals differ in exactly
    one fact (whether a run already exists), not in shape or meaning.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.mise_en_place_refused"

    workflow_id: UUID
    run_id: UUID | None = None
    #: Codes of the checks that failed, i.e. why the launch was refused.
    failed_checks: list[str] = Field(default_factory=list)
    #: Human-readable summary — the same text the caller received in the
    #: RFC 7807 `detail`.
    detail: str = Field(default="", max_length=4000)
    #: Whether EVERY failing check was transient (review BS5) — mirrors the
    #: status the caller got: `True` → 503, `False` → 422.
    retryable: bool = False
    #: The full report as persisted-shaped JSON, so a consumer sees exactly
    #: what a started run's `workflow_runs.mise_en_place` would have held.
    mise_en_place: dict[str, Any] = Field(default_factory=dict)
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


class WorkflowRunResumeMiseEnPlaceEvaluatedEvent(BaseModel):
    """Published every time a ``resume`` re-runs the Mise en Place gate and
    the run actually proceeds — cleanly, or via ``force`` (Story 4.12 AC3).

    ``workflow_runs.mise_en_place`` (Story 4.5 AC1) is written exactly once,
    at LAUNCH, and Story 4.12 decided to keep it that way rather than
    overwrite it on every resume: doing so would destroy the launch record
    Story 4.5 built as an audit artifact, silently replacing "what let this
    run start" with "what let it last resume". A resumed run authorized by
    a DIFFERENT report three days and two red checks later than its launch
    is common (an environment decays while a run sits ``paused``), and that
    report needs somewhere to exist — this event, mirroring how a refused
    launch already lives in the outbox rather than on the row
    (``mise_en_place_refused``) because AC2 forbade a row for it too.

    Distinct from ``mise_en_place_bypassed``, not a replacement for it: that
    event stays reserved for the explicit-human-override audit trail
    (NFR8) and fires only when ``bypassed`` is true. This one fires on
    EVERY successful resume, bypassed or not, because "which report
    authorized this" is a question worth answering even when nothing was
    overridden.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.resume_mise_en_place_evaluated"

    run_id: UUID
    workflow_id: UUID
    all_passed: bool
    bypassed: bool = False
    failed_checks: list[str] = Field(default_factory=list)
    #: The full report as persisted-shaped JSON — same shape
    #: `workflow_runs.mise_en_place` uses, so a consumer already reading
    #: that column recognises this without learning a second format.
    mise_en_place: dict[str, Any] = Field(default_factory=dict)
    tenant_id: UUID | None = None


class WorkflowRunPauseRequestedEvent(BaseModel):
    """Published when a caller asks a LIVE run to pause (Story 4.6 AC1).

    The run is STILL ``running`` when this lands, and stays so until the
    driver reaches its next superstep boundary — bounded by a node's
    duration, never instantaneous. That gap is the whole reason this event is
    distinct from :class:`WorkflowRunPausedEvent`: an audit trail that only
    recorded the effect could not show that someone asked at ``t0`` and the
    run stopped at ``t0 + 40s``, nor that a request was never honoured
    because the run finished first.

    ``actor`` mirrors :class:`WorkflowRunMiseEnPlaceBypassedEvent` — ``"system"``
    under the Sprint-1 convention (no authenticated-actor propagation exists
    yet), and like there, this is an event where a real actor will matter.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.pause_requested"

    run_id: UUID
    workflow_id: UUID
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


class WorkflowRunCancelRequestedEvent(BaseModel):
    """Published when a caller asks a LIVE run to stop (Story 4.6 AC1).

    Symmetric to :class:`WorkflowRunPauseRequestedEvent` — see there for why
    the request and the effect are two events.

    A ``cancel`` on an ALREADY-``paused`` run publishes
    :class:`WorkflowRunCancelledEvent` directly instead: no driver is alive
    to observe a signal, so there is no gap to record.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.cancel_requested"

    run_id: UUID
    workflow_id: UUID
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


class WorkflowRunControlRetractedEvent(BaseModel):
    """Published when a caller withdraws a control request before the driver
    observed it.

    Counterpart to the ``*_requested`` events above, and the reason this one
    exists: those are durable outbox rows, so without a matching record an
    auditor replaying the bus sees a cancellation requested on a run that
    then completed normally, with nothing saying anyone withdrew it or who.
    The disappearance of ``control_signal`` from the SSE ``state`` frame
    reaches only a client connected at that moment — not an audit.

    ``retracted_signal`` is what was pending, not what was asked for: the
    request being undone is the payload's subject.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.control_retracted"

    run_id: UUID
    workflow_id: UUID
    retracted_signal: str
    actor: str = Field(default="system", description="user_id or 'system' for unattended runs")
    tenant_id: UUID | None = None


class WorkflowRunPausedEvent(BaseModel):
    """Published when the driver suspends a run at a superstep boundary
    (Story 4.6 AC2).

    NOT terminal, and deliberately absent from
    ``router.py``'s ``_TERMINAL_EVENT_SUFFIXES``: a paused run can resume, so
    its SSE clients must stay connected. (``cancelled`` IS in that set —
    without it a cancelled run left every client hanging for an hour.)

    ``paused_at_node_id`` is the last node that COMPLETED, read from the
    applicative checkpoint — not the node that would have run next. The run
    resumes from LangGraph's own committed checkpoint, so no node is ever
    re-executed and this field is a trace, not a resume cursor.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.paused"

    run_id: UUID
    workflow_id: UUID
    paused_at_node_id: str | None = Field(
        default=None, description="last COMPLETED node — best-effort, None if not determinable"
    )
    tenant_id: UUID | None = None


class WorkflowRunCancelledEvent(BaseModel):
    """Published when a run stops for good at the caller's request
    (Story 4.6 AC2) — a terminal status, like ``completed`` and ``error``.

    Carries ``total_duration_ms`` because a cancelled run has REAL partial
    spend: every node that completed before the cancellation was billed.
    Mirror :meth:`_mark_failed`'s posture, not :meth:`_mark_completed`'s —
    partial metrics are aggregated and kept, never dropped.
    """

    event_type: ClassVar[str] = "workflow_engine.workflow_run.cancelled"

    run_id: UUID
    workflow_id: UUID
    cancelled_at_node_id: str | None = Field(
        default=None, description="last COMPLETED node — best-effort, None if not determinable"
    )
    total_duration_ms: int = Field(ge=0)
    tenant_id: UUID | None = None


class LLMFallbackTriggeredEvent(BaseModel):
    """The LLM router moved to the next provider in the chain (Story 1.6).

    **A typisation, not a new event.** ``app.lifespan._publish_fallback`` has
    published this payload as an anonymous ``dict`` since Story 1.6, and
    consumers (the ``llm-usage`` runbook, the Story 1.6 test suite) know it
    by that exact name and shape. Neither may change here — Story 4.6 T4.3
    only gives it a contract so the payload is validated at the publish site
    instead of being trusted.

    Lives in this module despite being an ``llm.*`` event because its module
    prefix is ``workflow_engine.`` — the router publishes through the
    workflow engine's bus namespace.

    Deliberately carries NO ``run_id``/``node_id``: joining a fallback to a
    run is done on ``correlation_id`` (NFR15). Adding ``run_id`` would force
    ``shared/llm`` — a kernel package — to know what a workflow run is, an
    inverted dependency. If Epic 8's Trace Explorer proves correlation alone
    is not enough, the clean fix is a neutral ``ContextVar`` in
    ``shared/llm``, not a business field here.
    """

    event_type: ClassVar[str] = "workflow_engine.llm.fallback_triggered"

    failed_provider: str
    next_provider: str
    error_class: str
    error_type: str
    model_attempted: str
    model_fallback: str
    correlation_id: str


__all__ = [
    "LLMFallbackTriggeredEvent",
    "WorkflowCreatedEvent",
    "WorkflowRunCancelRequestedEvent",
    "WorkflowRunCancelledEvent",
    "WorkflowRunCompletedEvent",
    "WorkflowRunFailedEvent",
    "WorkflowRunMiseEnPlaceBypassedEvent",
    "WorkflowRunMiseEnPlaceRefusedEvent",
    "WorkflowRunPauseRequestedEvent",
    "WorkflowRunPausedEvent",
    "WorkflowRunResumedEvent",
    "WorkflowRunRoutingEscalatedEvent",
    "WorkflowRunStartedEvent",
    "WorkflowRunStepCompletedEvent",
]
