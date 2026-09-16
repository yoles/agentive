"""Pydantic HTTP schemas — Workflow Engine (Story 4.1 AC1-AC4)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentive_backend.features.workflow_engine.domain.mise_en_place import (
    CHECK_CODES,
    CheckCode,
)

# Defensive caps — no DAG-builder UI exists yet (Story 4.1 anti-scope) and no
# workflow this MVP targets needs more; mirrors the `tool_ids` precedent in
# `agent_registry/schemas.py` (max_length=100, "Sprint 1 we don't expect
# more than a...").
_MAX_NODES = 100
_MAX_EDGES = 200

# Story 4.9 AC2/T2.1 — `StartRunRequest.input` had no size bound at all: a
# client could submit an arbitrarily large `task_input` JSON body, which
# every node in the workflow then carries in its prompt. 256 KiB is
# generous against any realistic seed input (structured task parameters,
# not file payloads — those belong in the memory/knowledge system, Epic 3)
# while still turning an unbounded body into a typed 422 rather than an
# oversized LLM prompt discovered only at run time.
_MAX_RUN_INPUT_BYTES = 262_144


def _reject_oversized_input(value: dict[str, Any]) -> dict[str, Any]:
    # `ensure_ascii=False` measures the JSON as it actually TRAVELS. The
    # default expands each non-ASCII character to a 6-byte `\uXXXX` escape
    # before `.encode()` weighs it, so 200 KB of accented text measured
    # 600 KB and was refused well under the documented cap.
    try:
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        # pydantic does not convert a `TypeError` raised in a validator into
        # a validation error — unconverted it escapes as a 500.
        raise ValueError(f"input is not JSON-serializable: {exc}") from exc
    if size > _MAX_RUN_INPUT_BYTES:
        raise ValueError(f"input is too large ({size} bytes; max {_MAX_RUN_INPUT_BYTES} bytes)")
    return value


# LangGraph reserves `__start__`/`__end__` (and other dunder-wrapped names)
# for its own graph sentinels: `StateGraph.add_node` REFUSES them. Without
# this guard a workflow naming a node `__start__` was accepted at creation
# (Story 4.1 only bounded the length) and then blew up at `build_state_graph`
# time — i.e. at RUN time, on a background task, for every run of that
# workflow forever. Rejecting at creation turns a permanently broken workflow
# into an immediate, actionable 422.
_RESERVED_NODE_ID_RE = re.compile(r"^__.*__$")

# Story 5.7 review — un `node_id` porteur d'un `/` (ou d'un blanc, ou d'un
# caractère de contrôle) rendait `GET /workflows/runs/{run_id}/nodes/{node_id}
# /output` INATTEIGNABLE : le segment se scinde, aucune route ne matche, et le
# client reçoit un 404 générique — pas même la forme RFC 7807 du module. La
# sortie du node restait listée par la route de détail et illisible page par
# page, c'est-à-dire exactement la promesse de l'AC2 rendue fausse par un nom.
#
# Refusé à la CRÉATION plutôt que contourné au routage : un `{node_id:path}`
# avalerait le segment `/output` qui le suit, et la contrainte réelle est
# qu'un identifiant de node doit pouvoir voyager dans une URL.
_URL_UNSAFE_NODE_ID_RE = re.compile(r"[/\\?#\s\x00-\x1f]")


def _reject_reserved_node_id(value: str) -> str:
    if _RESERVED_NODE_ID_RE.match(value):
        raise ValueError(
            f"node_id {value!r} is reserved by the workflow engine "
            "(names wrapped in double underscores are not allowed)"
        )
    if _URL_UNSAFE_NODE_ID_RE.search(value):
        raise ValueError(
            f"node_id {value!r} cannot travel in a URL path segment "
            "(no '/', '\\', '?', '#', whitespace or control characters): it would "
            "make GET /workflows/runs/{run_id}/nodes/{node_id}/output unreachable"
        )
    return value


class WorkflowNodeRequest(BaseModel):
    """One participant node in the submitted DAG."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=100)
    agent_template_id: UUID

    _check_node_id = field_validator("node_id", mode="after")(_reject_reserved_node_id)


class WorkflowEdgeRequest(BaseModel):
    """One directed edge in the submitted DAG, with an optional branching condition."""

    model_config = ConfigDict(extra="forbid")

    from_node_id: str = Field(min_length=1, max_length=100)
    to_node_id: str = Field(min_length=1, max_length=100)
    condition: str | None = Field(default=None, max_length=500)

    # Edges pointing at a reserved name would otherwise surface as the much
    # vaguer "edge references undeclared node" error.
    _check_endpoints = field_validator("from_node_id", "to_node_id", mode="after")(
        _reject_reserved_node_id
    )


class CreateWorkflowRequest(BaseModel):
    """Body of ``POST /api/v1/workflows``.

    A single-node DAG with no edges is a valid mono-agent workflow.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    nodes: list[WorkflowNodeRequest] = Field(min_length=1, max_length=_MAX_NODES)
    edges: list[WorkflowEdgeRequest] = Field(default_factory=list, max_length=_MAX_EDGES)

    @field_validator("name", mode="after")
    @classmethod
    def _strip_and_revalidate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank or whitespace-only")
        return stripped


class DiversityWarning(BaseModel):
    """Non-blocking Contrôleur/Producteur LLM-diversity alert (AC4, FR15, D84)."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["llm_diversity"] = "llm_diversity"
    controller_node_id: str
    producer_node_id: str
    controller_template_id: UUID
    producer_template_id: UUID
    reason: str


class CreateWorkflowResponse(BaseModel):
    """Response of ``POST /api/v1/workflows`` — 201 Created, or 200 on replay."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    version: int
    warnings: list[DiversityWarning] = Field(default_factory=list)
    idempotent_replay: bool = Field(
        default=False,
        description=(
            "True when this request was a replay of an earlier, identical one "
            "(Story 4.8 AC1): nothing was created, and `workflow_id` is the id "
            "of the workflow the FIRST request produced. The route answers 200 "
            "rather than 201 in that case, so a client can tell without reading "
            "the body. Defaults to False, which keeps every existing "
            "construction of this model valid."
        ),
    )


class StartRunRequest(BaseModel):
    """Body of ``POST /api/v1/workflows/{workflow_id}/runs`` (Story 4.2 AC1).

    ``input`` is free-form JSON — the workflow's initial ``task_input``,
    passed to the first node(s) unmodified.

    ``force``/``reason`` (Story 4.5 AC3) — explicit bypass of a failing Mise
    en Place pre-workflow check. ``extra="forbid"`` means these are real,
    intentional fields rather than passthrough, so the informal "--force"
    notation of the epic maps onto the one vector this REST-only repo has
    (Dev Notes § Divergences assumées).
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)
    force: bool = False
    reason: str | None = Field(default=None, max_length=2000)

    _check_input_size = field_validator("input", mode="after")(_reject_oversized_input)

    @model_validator(mode="after")
    def _reason_requires_force(self) -> StartRunRequest:
        """AC3 — ``force=true`` with a blank/absent ``reason`` is a 422,
        unconditionally (whether or not any check actually fails): a bypass
        with no stated reason defeats the audit trail this field exists
        for (NFR8).

        The converse is rejected too (review P21): a ``reason`` sent WITHOUT
        ``force`` used to be accepted and then silently discarded — never
        persisted, never published, never echoed back. An operator who
        mistypes the bypass and believes they filed a justification is worse
        off than one who gets a 422, so the useless combination is refused
        rather than swallowed.
        """
        if self.force and not (self.reason or "").strip():
            raise ValueError("reason is required when force=true")
        if not self.force and (self.reason or "").strip():
            raise ValueError("reason is only meaningful with force=true")
        return self


class ResumeRunRequest(BaseModel):
    """Optional body of ``POST /api/v1/workflows/runs/{run_id}/resume``.

    Story 4.6 review, `IG2` — a resume re-runs the Mise en Place pre-flight,
    so it needs the same bypass vector as a launch. An environment can decay
    while a run sits paused (a rotated API key, a decommissioned MCP server,
    an exhausted budget), and an ungated resume walked straight into it: the
    row moved to ``running``, the first node raised, and the run ended
    ``error`` with its checkpoint gone — a run that was still recoverable a
    second earlier. Refusing keeps it ``paused``, hence resumable once the
    environment is fixed.

    ``pause`` and ``cancel`` deliberately take NO body: neither resumes
    execution, so neither can walk into a broken environment.

    Same coupling as :class:`StartRunRequest`, deliberately duplicated rather
    than shared by inheritance — the two bodies have nothing else in common,
    and a common base would invite ``input`` onto this route.
    """

    model_config = ConfigDict(extra="forbid")

    force: bool = False
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _reason_requires_force(self) -> ResumeRunRequest:
        """Mirror of :meth:`StartRunRequest._reason_requires_force`, both
        directions: a bypass with no stated reason defeats the audit trail
        (NFR8), and a reason without a bypass would be silently discarded."""
        if self.force and not (self.reason or "").strip():
            raise ValueError("reason is required when force=true")
        if not self.force and (self.reason or "").strip():
            raise ValueError("reason is only meaningful with force=true")
        return self


class MiseEnPlaceCheckOut(BaseModel):
    """One of the four pre-workflow checks (Story 4.5 AC1), as returned/persisted.

    Distinct from the domain ``CheckResult`` dataclass (mirror the
    ``ProbablePathResult``/``DryRunResponse`` split of Story 4.4) — this is
    the API/JSONB-facing shape, the domain class stays framework-free.
    """

    model_config = ConfigDict(extra="forbid")

    #: The domain ``Literal`` itself, not a copy of its four strings — the
    #: closed set has one definition (``domain.mise_en_place.CheckCode``) and
    #: adding a fifth check cannot leave this schema silently behind
    #: (review P19).
    code: CheckCode
    passed: bool
    detail: str
    suggested_action: str | None = None
    #: Whether an identical retry could succeed without a configuration
    #: change (review BS5). Drives the refusal's HTTP status — 503 only when
    #: EVERY failing check is retryable, else 422 — and lets a client decide
    #: whether backing off is worth anything.
    retryable: bool = False


class MiseEnPlaceReportOut(BaseModel):
    """The persisted/returned Mise en Place report (Story 4.5 AC1/AC3) —
    always exactly four :class:`MiseEnPlaceCheckOut`, one per check code,
    whatever the outcome."""

    model_config = ConfigDict(extra="forbid")

    #: Exactly one entry per check code — the cardinality the docstring above
    #: promises, enforced rather than described (review P8). Mirrors the
    #: identical guard in ``domain.mise_en_place.build_report``.
    checks: list[MiseEnPlaceCheckOut] = Field(
        min_length=len(CHECK_CODES), max_length=len(CHECK_CODES)
    )
    all_passed: bool
    bypassed: bool = False
    bypass_reason: str | None = None


class AcknowledgementOut(BaseModel):
    """L'accusé de réception d'un run (Story 5.1 AC2).

    ``eta_source`` n'est pas cosmétique : ``"history"`` signifie que CHAQUE
    node du DAG avait au moins une durée mesurée sur un run passé ; dès qu'un
    seul est retombé sur le défaut de configuration, c'est ``"heuristic"``.
    Un lecteur qui budgète doit pouvoir distinguer les deux — c'est ce que
    la revue du Dry Run a imposé à ses propres estimations
    (``no_execution_history``, ``node_estimate_from_fallback``).
    """

    model_config = ConfigDict(extra="forbid")

    message: str
    agents: list[str]
    eta_minutes: int = Field(ge=1)
    eta_source: Literal["history", "heuristic"]


class StartRunResponse(BaseModel):
    """Response of ``POST /api/v1/workflows/{workflow_id}/runs`` — 201 Created.

    Returned IMMEDIATELY, before the run progresses (AC1) — execution
    happens in a fire-and-forget background task.

    ``warnings`` carries the Contrôleur/Producteur LLM-diversity check re-run
    at run start, the second evaluation point ``epics.md`` assigns to this
    story. Non-blocking, exactly like the creation-time check of 4.1 AC4: an
    empty list is the normal case, a non-empty one still comes with a 201.
    It is re-evaluated here rather than trusted from creation because
    ``agent_templates`` rows are mutable — a workflow validated as diverse
    can be running two identical models by the time anyone starts it.

    ``mise_en_place`` (Story 4.5 AC1) — the same report persisted to
    ``workflow_runs.mise_en_place``, always present (a 201 is only reached
    once the hook ran, whether every check passed or a failure was bypassed
    via ``force``).

    ``acknowledgement`` (Story 5.1 AC2) — « Compris. Je mobilise [agents].
    ETA ~[X] min. ». Le même objet que la colonne, que la frame SSE ``state``
    et que l'event ``started`` : une seule valeur, calculée une fois, rendue
    sur les trois surfaces. Présent ici parce que le dogfooding de Sprint 2
    se fait en client HTTP (décision de John, 2026-09-14) : un appelant en
    ``curl`` obtient l'accusé sans avoir à ouvrir un flux SSE.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["running"] = "running"
    warnings: list[DiversityWarning] = Field(default_factory=list)
    mise_en_place: MiseEnPlaceReportOut
    acknowledgement: AcknowledgementOut


class RoutingStatsResponse(BaseModel):
    """Response of ``GET /api/v1/workflows/{workflow_id}/routing-stats``
    (Story 4.3 AC3 T10.2) — ``% routages déterministes vs LLM`` aggregated
    across runs of the workflow started within ``window_days`` (Story 4.10
    AC4 — previously EVERY run ever, a cost proportional to a number the
    caller controls, not the operator).

    ``deterministic_pct`` is ``None`` when ``deterministic + llm_escalated
    == 0`` — a legitimate state (no decision point was ever reached, e.g. a
    purely sequential workflow, or a workflow with zero runs), never a
    division-by-zero to paper over.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    #: How many trailing days `runs_counted`/`deterministic`/`llm_escalated`
    #: cover — Story 4.10 AC4 turned this from an all-time proportion into a
    #: trailing-window one, so the response says which rather than leaving a
    #: client to assume the old, unbounded meaning still holds.
    window_days: int = Field(ge=1)
    #: EVERY run of this workflow started within the window, whatever its
    #: status — including runs still `running` and runs that failed. It is
    #: therefore NOT the denominator of `deterministic_pct`: a workflow can
    #: legitimately report many runs counted and zero decisions. The ratio's
    #: denominator is `deterministic + llm_escalated`, which counts DECISIONS,
    #: not runs.
    runs_counted: int = Field(ge=0)
    deterministic: int = Field(ge=0)
    llm_escalated: int = Field(ge=0)
    deterministic_pct: float | None = None


class HandoffStatsResponse(BaseModel):
    """Response of ``GET /api/v1/workflows/{workflow_id}/handoff-stats``
    (Story 4.7 AC3) — token-reduction from handoff summaries, aggregated
    across every run of the workflow. Mirror ``RoutingStatsResponse``.

    ``reduction_ratio_pct`` is ``None`` when ``raw_tokens_replaced == 0`` — a
    legitimate state (no node in this workflow ever had a downstream
    successor to summarize for, or the workflow has zero runs), never a
    division-by-zero to paper over.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    #: EVERY run of this workflow, whatever its status. NOT the denominator
    #: of `reduction_ratio_pct` — a workflow can report many runs counted and
    #: zero handoffs (e.g. every workflow here is a single terminal node).
    runs_counted: int = Field(ge=0)
    raw_tokens_replaced: int = Field(ge=0)
    summary_tokens: int = Field(ge=0)
    reduction_ratio_pct: float | None = None


class RunControlResponse(BaseModel):
    """Response of ``POST /workflows/runs/{run_id}/{pause,resume,cancel}``
    (Story 4.6 AC1).

    Both fields are needed because a ``202`` says the request was RECORDED,
    not applied: after a ``pause`` the run still reads ``status="running"``
    with ``control_signal="pause"``, and only reaches ``paused`` once the
    driver hits its next superstep boundary. Returning the status alone would
    look, to a client, exactly like the call having done nothing.

    ``pause`` and ``cancel`` take no request body: a ``reason``/``actor``
    audit field would be a natural extension but no AC asks for one (unlike
    Story 4.5's ``force``+``reason``, which the epic demanded explicitly), so
    adding it there would be scope creep. ``resume`` is the exception — it
    accepts :class:`ResumeRunRequest`, because the review (``IG2``) made it
    re-run the Mise en Place pre-flight and a gate needs its bypass. This
    docstring claimed all three were bodiless until review lot 11.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: Literal["running", "paused", "completed", "error", "cancelled"]
    #: The PENDING request, if the effect is deferred. ``None`` once the
    #: transition has actually been applied (``resume``, and ``cancel`` on an
    #: already-paused run).
    control_signal: Literal["pause", "cancel"] | None = None


#: Where a node output rendered on an HTTP surface was actually read from.
#:
#: The distinction is NOT cosmetic and must never be inferred from the text:
#: ``checkpointer`` is the node's real output, ``preview`` is the 500-char
#: diagnostic excerpt of ``workflow_runs.checkpoint`` — all that survives once
#: Story 4.10's retention worker has purged the LangGraph thread. A reader who
#: cannot tell them apart reads a purged run as a short run.
NodeOutputSource = Literal["checkpointer", "preview"]

#: Same question, asked of the response as a whole — WHERE what you are
#: reading came from, and nothing else.
#:
#: ``none`` means no output could be shown at all. On its own that is not a
#: diagnosis: pair it with ``checkpointer_reachable`` to tell "this run has
#: recorded nothing" (reachable) from "nothing could be consulted" (not
#: reachable). The two facts are orthogonal, and the first version of this
#: story conflated them into a single ``unavailable`` value that the code
#: then assigned on the wrong criterion — the PRESENCE of an applicative
#: preview rather than the CAUSE — so a saturated pool read as a purged
#: thread. Splitting them is what makes the cause actually knowable.
NodeOutputsSource = Literal["checkpointer", "preview", "none"]


class NodeOutputOut(BaseModel):
    """One node's output as it leaves over HTTP (Story 5.7 AC2).

    **A string, not nested JSON, and that is the decision.** The value is
    produced by an LLM — an uncontrolled external input (NFR9, règle d'or #9)
    that Epic 6 will render in a UI — so it is emitted verbatim, re-parsed by
    nothing on the way out. It is also what makes pagination honest: an offset
    in characters is resumable, where re-cutting a nested object would emit
    invalid JSON, exactly the defect the Story 5.2 review fixed in
    ``read_file``.

    **Every cut is said.** ``truncated`` + ``total_chars`` + ``returned_chars``
    + ``next_offset``, never a text that merely stops. "Un résultat coupé en
    silence fait croire au lecteur qu'il a tout vu" is the defect that review
    paid for most (four silent paths in ``code_search.py``), and it does not
    get reintroduced at the HTTP surface.

    ``next_offset`` describes what the SERVER still holds, never where the
    text came from. The first version of this story tied it to the source —
    an excerpt was declared unresumable merely because it was an excerpt — so
    a preview cut by the caller's own ``limit`` answered "the rest is gone"
    while ``?offset=N`` served it perfectly. The two questions are separate:
    "is there more in this response's buffer?" (``next_offset``) and "was the
    original already cut before it got here?" (a ``preview`` whose last page
    still reads ``truncated``).
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str
    #: The JSON rendering of the node's output, redacted then truncated — in
    #: that order, so a ``next_offset`` from one page still lines up with the
    #: next (redaction changes length).
    output: str
    source: NodeOutputSource
    #: Length of the FULL rendering this excerpt was cut from, in characters.
    #: Compare against ``returned_chars`` to know how much is missing without
    #: having to fetch it.
    total_chars: int = Field(ge=0)
    returned_chars: int = Field(ge=0)
    #: Offset this excerpt starts at — ``0`` on the detail route, the caller's
    #: own ``offset`` on the per-node route. Echoed so a page is self-describing.
    offset: int = Field(ge=0)
    truncated: bool
    #: Where to resume, or ``None`` when this response holds nothing more.
    #: ALWAYS present as a key (``null`` rather than omitted) — the
    #: ``acknowledgement`` lesson of Story 5.1: a client written against one
    #: surface must not take a ``KeyError`` on another.
    #:
    #: Read WITH ``truncated``; the pair is the whole contract:
    #:
    #: ===============  ===============  ==================================
    #: ``truncated``    ``next_offset``  Meaning
    #: ===============  ===============  ==================================
    #: ``False``        ``None``         that is all of it, nothing missing
    #: ``True``         ``N``            more here — resume at ``N``
    #: ``True``         ``None``         you have read everything the server
    #:                                   holds, and the ORIGINAL was already
    #:                                   cut before it got here (``source``
    #:                                   is ``preview``: the LangGraph thread
    #:                                   is gone, so the rest is unreadable)
    #: ===============  ===============  ==================================
    next_offset: int | None = None


class RunNodeOutputResponse(BaseModel):
    """Response of ``GET /workflows/runs/{run_id}/nodes/{node_id}/output``
    (Story 5.7 AC2) — one page of ONE node's output.

    The route that makes "la sortie complète est atteignable" true: page after
    page, ``next_offset`` after ``next_offset``, with no per-node cap on the
    total read — only on what one response carries.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    node_id: str
    #: Bound applied to THIS response, in characters. Echoed because it comes
    #: from server configuration (``AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS``) when
    #: the caller passes no ``limit``: a reader must be able to see the number
    #: their page was cut at without reading the deployment's env.
    limit: int = Field(ge=1)
    #: Always present. The "no output anywhere for this node" case this field
    #: was once documented as nullable for is a ``404``, so the ``None`` was
    #: a branch a defensive client would have written and never entered.
    output: NodeOutputOut
    #: Whether the LangGraph checkpointer could be consulted at all. ``False``
    #: means ``output`` is whatever fallback was available, NOT the
    #: authoritative record — see :data:`NodeOutputsSource`.
    checkpointer_reachable: bool
    #: Story 4.10 AC1 — when this run's LangGraph thread was purged, or
    #: ``None`` if it was not. Rendered unconditionally, exactly as on
    #: ``RunDetailResponse``: it is a fact about the RUN, not about this
    #: response, and making it conditional gave the same column two meanings
    #: across two routes of the same story.
    checkpoint_purged_at: datetime | None = None


class RunDetailResponse(BaseModel):
    """Response of ``GET /api/v1/workflows/runs/{run_id}`` (Story 5.7 AC1).

    The surface an operator was missing: until this story, reading what a run
    did meant ``psql``. ``metrics.per_node`` carries the four tool counters
    Story 5.2 AC2 names word for word (``tool_calls``,
    ``tool_loop_iterations``, ``tool_failures``, ``tool_names``), and they had
    no HTTP surface at all.

    **What it returns and what it does NOT.** It returns the run's state, its
    metrics, its Mise en Place report, its acknowledgement, and an excerpt of
    each executed node's output. It does NOT return the LangGraph technical
    checkpoint, the routing/handoff detail of
    ``workflow_runs.checkpoint`` (``/routing-stats`` and ``/handoff-stats``
    aggregate those), nor a node's output in full — that is the per-node
    route above, page by page.

    ``metrics`` is typed ``dict`` deliberately: it is free JSONB written by
    several versions of the engine, so it is re-emitted defensively rather
    than promised a shape this schema would then have to keep true. The
    Story 5.2 review found ``_aggregate_metrics`` dropping a whole run's
    aggregation on one malformed value; a schema that claimed more than the
    column holds is the same mistake, one layer up.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    workflow_id: UUID
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    correlation_id: UUID
    #: Free JSONB (see class docstring). ``{}`` for a run that produced none —
    #: never a missing key.
    metrics: dict[str, Any] = Field(default_factory=dict)
    mise_en_place: MiseEnPlaceReportOut | None = None
    acknowledgement: AcknowledgementOut | None = None
    #: Applicative progress, read off ``workflow_runs.checkpoint`` — the same
    #: two fields the SSE ``state`` frame surfaces, for a caller who never
    #: opened the stream.
    last_node_id: str | None = None
    node_statuses: dict[str, str] = Field(default_factory=dict)
    #: A PENDING pause/cancel request, mirroring the SSE ``state`` frame. A run
    #: asked to pause still reads ``status="running"``.
    control_signal: str | None = None
    #: Already redacted at the source (``_mark_failed``), never re-derived here.
    last_error: str | None = None
    #: One entry per node that produced an output, in the order the engine
    #: recorded them. Empty when the run has not reached its first node.
    node_outputs: list[NodeOutputOut] = Field(default_factory=list)
    #: Where ``node_outputs`` came from, as a whole — see
    #: :data:`NodeOutputsSource`.
    node_outputs_source: NodeOutputsSource
    #: Whether the LangGraph checkpointer could be consulted at all.
    #:
    #: ``False`` is the value that keeps an outage from reading as a sterile
    #: run: nothing is known about any node's output, which is a different
    #: fact from "this run produced nothing". Orthogonal to
    #: ``node_outputs_source`` on purpose — a fallback can be served while the
    #: authoritative source is down, and the reader is entitled to know.
    checkpointer_reachable: bool
    #: Story 4.10 AC1 — when the LangGraph thread was purged. Rendered beside
    #: the outputs so a ``preview`` source explains itself instead of having
    #: to be guessed.
    checkpoint_purged_at: datetime | None = None


class DryRunRequest(BaseModel):
    """Body of ``POST /api/v1/workflows/{workflow_id}/dry-run`` (Story 4.4 AC1).

    Mirror of :class:`StartRunRequest` — the same ``task_input`` the workflow
    would receive on a real run. Never persisted: a Dry Run creates no
    ``workflow_runs`` row.

    **``input`` does not influence the estimate** (review fix P16). It is
    accepted so a caller can send the exact body it would POST to
    ``/runs``, but the estimation is structural + historical by design
    (Dev Notes § Algorithme ``probable_path``): with zero real LLM call
    there is no way to learn what THIS input would produce. A 10-character
    input and a 200 KB one therefore return identical token figures. Stated
    here because this is the public contract — burying it in the service
    docstring left every API consumer to discover it by experiment.
    """

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any] = Field(default_factory=dict)


class ProviderTokenEstimate(BaseModel):
    """Token/cost estimate aggregated for one resolved LLM provider
    (Story 4.4 AC1) — one entry per key of
    :attr:`DryRunResponse.token_estimate_per_provider`."""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    #: ``str``, never ``Decimal`` — mirror ``RoutingDecision.llm_cost_usd``
    #: (Story 4.3): this value crosses a JSON boundary, whose serializer
    #: raises on a raw ``Decimal``. ``None`` when no model in this provider
    #: group resolved a price (never a fabricated ``"0"``).
    cost_usd: str | None = None


class DryRunAgentInvolvement(BaseModel):
    """One node structurally reachable by the Dry Run (Story 4.4 AC1) —
    a superset of the nodes on ``probable_path``: a node that might run
    must be priced even when it is not on the single representative path."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    agent_template_id: UUID
    on_probable_path: bool


class DryRunRisk(BaseModel):
    """One identified risk (Story 4.4 AC1/AC3). ``code`` is a CLOSED set —
    exactly the risk types this story computes (no "recrutement dynamique"
    code: no dynamic-recruitment mechanism exists in this repo, see Story
    4.4 Dev Notes § Divergences assumées).

    Three of the six codes were added by the review (IG1/IG2/IG4). The
    original three left the response unable to say that an estimate was
    PARTIAL: a node whose model carried no price was dropped from the
    figures entirely, `no_execution_history` only ever fired for a workflow
    that had never run at all, and a historical fan-out decision was
    silently narrowed to one branch. Every one of those made the response
    look more complete than it was — the opposite of what a number meant
    for budgeting should do. Story 9.4 consumes these to know whether it
    can trust a total before blocking on it.
    """

    model_config = ConfigDict(extra="forbid")

    code: Literal[
        # A decision point with no usable historical majority — the path
        # guesses at this node.
        "routing_decision_uncertain",
        # The winning historical decision named several targets: the engine
        # would fan out here, `probable_path` can only show one of them.
        "routing_decision_multi_target",
        # The workflow has never run: every figure is heuristic.
        "no_execution_history",
        # THIS node had no usable history, even though the workflow has run
        # (a node added since, or one only reached on a rare branch).
        "node_estimate_from_fallback",
        # This node's model is absent from every pricing table, so its
        # tokens AND cost are missing from the totals.
        "model_price_unresolved",
        # `cost_estimate_usd` is over the configured cap (AC3).
        "budget_cap_exceeded",
    ]
    node_id: str | None = None
    detail: str
    #: The two figures AC3 asks `budget_cap_exceeded` to carry ("le montant
    #: estimé et le seuil dépassé"), as machine-readable fields rather than
    #: prose (review, BS1). T4.1 specified this model as
    #: ``{code, node_id, detail}``, which left nowhere to put them — so they
    #: went into the English `detail` string, and the Epic 6 Dialog this
    #: contract exists to prepare would have had to parse it back out. Same
    #: `str`-not-`Decimal` convention as everywhere else in this response:
    #: the JSON serializer raises on a raw `Decimal`.
    #:
    #: Populated only for `budget_cap_exceeded`; `None` on every other code.
    #: `detail` still states both, for a human reading the raw response.
    estimated_usd: str | None = None
    threshold_usd: str | None = None


class DryRunResponse(BaseModel):
    """Response of ``POST /api/v1/workflows/{workflow_id}/dry-run`` — 200 OK
    (Story 4.4 AC1). Never 201: unlike ``POST /workflows`` and
    ``POST /workflows/{id}/runs``, this endpoint creates no resource."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    probable_path: list[str]
    agents_involved: list[DryRunAgentInvolvement]
    token_estimate_per_provider: dict[str, ProviderTokenEstimate]
    #: UPPER BOUND: every node in ``agents_involved``, including branches
    #: that are mutually exclusive at runtime. A 5-way exclusive decision
    #: bills all five here while a real run pays one — deliberate, since
    #: excluding them would understate what the workflow CAN cost.
    #: ``None`` when NO node resolved a price — never a fabricated ``"0"``
    #: (Story 4.4 Dev Notes § Pièges connus #3).
    cost_estimate_usd: str | None
    #: EXPECTED cost: only the nodes on ``probable_path``. Added by the
    #: review (IG7) because the upper bound alone is neither the expected
    #: spend nor an announced bound, which makes it hard to act on for a
    #: `[Lancer]`/`[Annuler]` decision. Both are reported so the caller
    #: picks; `on_probable_path` already made the split available per node.
    #: ``None`` on the same terms as ``cost_estimate_usd``.
    probable_path_cost_usd: str | None = None
    identified_risks: list[DryRunRisk]


__all__ = [
    "AcknowledgementOut",
    "CreateWorkflowRequest",
    "CreateWorkflowResponse",
    "DiversityWarning",
    "DryRunAgentInvolvement",
    "DryRunRequest",
    "DryRunResponse",
    "DryRunRisk",
    "HandoffStatsResponse",
    "MiseEnPlaceCheckOut",
    "MiseEnPlaceReportOut",
    "NodeOutputOut",
    "NodeOutputSource",
    "NodeOutputsSource",
    "ProviderTokenEstimate",
    "ResumeRunRequest",
    "RoutingStatsResponse",
    "RunControlResponse",
    "RunDetailResponse",
    "RunNodeOutputResponse",
    "StartRunRequest",
    "StartRunResponse",
    "WorkflowEdgeRequest",
    "WorkflowNodeRequest",
]
