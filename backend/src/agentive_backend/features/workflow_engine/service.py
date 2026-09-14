"""Service layer — Workflow Engine (Story 4.1).

``WorkflowService.create_workflow`` validates a submitted DAG (AC1),
persists it versioned (AC2), validates branching-condition variables against
the emitting node's ``output_contract.core`` (AC3), and surfaces a
non-blocking Contrôleur/Producteur LLM-diversity warning (AC4, FR15, D84).

Every write composes the row DML and the outbox ``publish`` in a single
``with_tenant`` transaction (mirror ``AgentTemplateService.create_template``,
Story 2.1 P-02 atomicity), then a best-effort post-commit
``notify_best_effort`` wakes the outbox worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from agentive_backend.features.workflow_engine.domain import (
    DomainValidationError,
    RoutingRule,
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
    WorkflowState,
    build_report,
    detect_cycle,
    find_dangling_edges,
    find_duplicate_node_ids,
)
from agentive_backend.features.workflow_engine.domain import parse as parse_condition
from agentive_backend.features.workflow_engine.domain.run_control import (
    RunAction,
    Transition,
    resolve_transition,
)
from agentive_backend.features.workflow_engine.engine import build_state_graph
from agentive_backend.features.workflow_engine.engine.agent_node import (
    RetrySettings,
    is_raw_fallback_output,
)
from agentive_backend.features.workflow_engine.engine.graph_builder import (
    RoutingDecisionFailedError,
)
from agentive_backend.features.workflow_engine.engine.handoff import HandoffSettings
from agentive_backend.features.workflow_engine.engine.hybrid_router import RoutingSettings
from agentive_backend.features.workflow_engine.metrics import (
    ROUTING_DECISIONS_TOTAL,
    ROUTING_ESCALATION_SECONDS,
)
from agentive_backend.features.workflow_engine.recovery import derive_stale_threshold_s
from agentive_backend.features.workflow_engine.schemas import (
    CreateWorkflowResponse,
    DiversityWarning,
    MiseEnPlaceCheckOut,
    MiseEnPlaceReportOut,
    RunControlResponse,
    StartRunResponse,
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)
from agentive_backend.shared.config import settings
from agentive_backend.shared.contracts.diversity import LLMSelection, check_llm_diversity
from agentive_backend.shared.contracts.events import (
    WorkflowCreatedEvent,
    WorkflowRunCancelledEvent,
    WorkflowRunCancelRequestedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunControlRetractedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunMiseEnPlaceBypassedEvent,
    WorkflowRunMiseEnPlaceRefusedEvent,
    WorkflowRunPausedEvent,
    WorkflowRunPauseRequestedEvent,
    WorkflowRunResumedEvent,
    WorkflowRunResumeMiseEnPlaceEvaluatedEvent,
    WorkflowRunRoutingEscalatedEvent,
    WorkflowRunStartedEvent,
    WorkflowRunStepCompletedEvent,
)
from agentive_backend.shared.correlation import require_correlation_id
from agentive_backend.shared.event_bus import notify_best_effort, publish, publish_and_commit
from agentive_backend.shared.exceptions import (
    BusinessRuleError,
    ConflictError,
    DependencyError,
    InternalError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from agentive_backend.shared.llm.redaction import redact_secrets
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo, WorkflowRunRepo

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from pydantic import BaseModel

    from agentive_backend.features.workflow_engine.domain.mise_en_place import MiseEnPlaceReport
    from agentive_backend.features.workflow_engine.mise_en_place import MiseEnPlaceService
    from agentive_backend.infra.db.models import AgentTemplate, Workflow, WorkflowRun
    from agentive_backend.shared.llm.router import LLMRouter

_log = get_logger(__name__)

# Fire-and-forget run-execution tasks — kept alive until completion (a task
# with no external reference can be garbage-collected mid-await). Duplicated
# from `app.middleware._background_tasks` rather than imported — `features`
# may not import `app.*` (Contract 2 layering), cf Dev Notes § T5.2 étape 6.
_background_tasks: set[asyncio.Task[None]] = set()

# Upper bound on how long shutdown waits for cancelled run tasks to unwind.
# This is NOT a drain (T5.4 forbids waiting for runs to FINISH) — only the
# time it takes an already-cancelled task to propagate its `CancelledError`.
_CANCEL_TIMEOUT_S = 3.0


async def cancel_inflight_runs(*, timeout_s: float = _CANCEL_TIMEOUT_S) -> int:
    """Cancel every in-flight run task and wait for the cancellations to land.

    Called by the app lifespan BEFORE closing the LangGraph checkpointer.
    Without it, `AsyncExitStack.aclose()` shut the checkpointer's connection
    while these tasks were mid-`astream`; each one then raised on a closed
    connection, was caught by :meth:`WorkflowExecutionService._execute`'s
    ``except Exception`` and marked ``error`` — a TERMINAL status that
    `claim_stale_running` never revisits. So the very shutdown path whose
    comment claimed "an interrupted run IS the AC3 scenario" was in fact
    destroying every run it interrupted.

    Cancellation avoids that because ``asyncio.CancelledError`` derives from
    ``BaseException``, not ``Exception``: it flows straight through
    ``_execute``'s handler, no ``failed`` transition runs, and the row stays
    ``running`` for the next process's recovery sweep to claim.

    Returns the number of tasks cancelled.
    """
    pending = [task for task in _background_tasks if not task.done()]
    if not pending:
        return 0
    _log.info("workflow_engine.cancelling_inflight_runs", count=len(pending))
    for task in pending:
        task.cancel()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout_s)
    return len(pending)


# Key under which `_sync_checkpoint` records the config fingerprint of every
# template the run is currently executing with (review, intent gap 5).
TEMPLATE_FINGERPRINTS_KEY = "template_fingerprints"

# Must stay <= `WorkflowRunMiseEnPlaceBypassedEvent.reason`'s `max_length`
# and `StartRunRequest.reason`'s: `start_run` is reachable directly (not only
# through the HTTP schema), so the cap is enforced at all three layers rather
# than trusted from the outermost one (review P7).
_MAX_BYPASS_REASON_LEN = 2000


def _template_fingerprints(templates: Mapping[str, AgentTemplate]) -> dict[str, str]:
    """Fingerprint each node's resolved template config.

    `agent_templates` rows are MUTABLE: `AgentTemplateService.update_template`
    merges into `config` in place under the same `template_id`, and only a
    `system_prompt` change bumps `version` — a model, temperature or
    max_tokens edit does not. So neither the id nor the version identifies
    what a node will actually execute; only the content does.

    Recorded on every checkpoint sync so a resume can compare what it is
    about to run against what the run was running when it stopped.
    """
    return {
        node_id: hashlib.sha256(
            json.dumps(template.config or {}, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        for node_id, template in templates.items()
    }


def _request_fingerprint(name: str, dag_payload: dict[str, Any]) -> str:
    """Idempotence key of a ``POST /api/v1/workflows`` request (Story 4.8 AC1).

    SHA-256 hex of the submitted ``{name, dag}``. Two byte-identical bodies
    produce the same fingerprint, which is exactly the scenario AC1 describes
    (a client replaying its request after a network timeout).

    The converse does not hold, and the gap is in the safe direction: the
    hash is taken over ``dag_payload``, the projection that gets stored, so
    bodies that differ only where the projection erases the difference —
    a ``name`` with surrounding whitespace (``CreateWorkflowRequest``
    ``.strip()``s it), an omitted ``condition`` versus an explicit ``null``
    — converge to one fingerprint and one workflow. Nothing is LOST in that
    projection (``extra="forbid"`` on all three request models, and every
    field they accept is projected), so two genuinely different DAGs can
    never collide.

    FULL 64 characters, never truncated — unlike :func:`_template_fingerprints`
    six lines above, which cuts to ``[:16]``. The difference is not an
    inconsistency: that one is a diagnostic comparator (did this node's config
    drift while the run was stopped?), this one is a UNIQUENESS KEY backing a
    unique index. Truncating a hash used as a key is how you manufacture
    collisions, and a collision here would silently hand a caller someone
    else's workflow.

    ``sort_keys=True`` so key order inside ``dag_payload`` cannot change the
    result; ``separators`` pinned so a future Python default cannot either.
    ``ensure_ascii=False`` keeps a non-ASCII workflow name a single character
    rather than an escape sequence — the value is hashed, never displayed, so
    only stability matters, and ``.encode()`` makes the byte form explicit.

    What this deliberately does NOT do is normalise the DAG. ``dag_payload``
    preserves the submitted order of ``nodes`` and ``edges``, so two
    semantically equivalent bodies listing the same nodes in a different order
    are two different fingerprints, hence two workflows. Sorting them first
    would turn idempotence into "isomorphic DAGs are the same workflow" — a
    much stronger product rule that no AC asks for and that would surprise
    anyone who built two pipelines out of the same bricks.
    """
    canonical = json.dumps(
        {"name": name, "dag": dag_payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


# Applicative checkpoint preview — bounds `workflow_runs.checkpoint`'s
# `node_outputs_preview` JSONB growth (an LLM node output can be arbitrarily
# large). Mirror the `[:500]` convention used elsewhere for truncated
# diagnostic text (`LLMRouter`'s `error_detail`, `WorkflowRunFailedEvent
# .error_summary`).
_CHECKPOINT_PREVIEW_MAX_CHARS = 500

_CONTROLLER_ARCHETYPE = "controleur"


def _llm_selection_from_config(config: dict[str, Any]) -> LLMSelection | None:
    """Build an ``LLMSelection`` from a raw ``agent_templates.config`` dict.

    Small standalone adapter — NOT a cross-feature import of
    ``AgentTemplateService._to_llm_selection`` : ``.import-linter``
    Contract 1 forbids ``features.workflow_engine`` from importing
    ``features.agent_registry`` (D84 point 2). ``None`` if the template's
    LLM config is not yet set OR only partially set — mirrors Story 2.8
    P-02 : a stray ``null``/``{}``/partial ``llm_params`` mapping (e.g.
    ``temperature`` present but ``max_tokens`` missing) must never fabricate
    a default for the missing key, only a fully-populated ``llm_params``
    counts as "configured".
    """
    llm_model = config.get("llm_model")
    llm_params = config.get("llm_params")
    if (
        llm_model is None
        or not isinstance(llm_params, dict)
        or "temperature" not in llm_params
        or "max_tokens" not in llm_params
    ):
        return None
    return LLMSelection(
        model=llm_model,
        temperature=llm_params["temperature"],
        max_tokens=llm_params["max_tokens"],
    )


def _stored_edges(dag_payload: dict[str, Any]) -> list[SimpleNamespace]:
    """Adapt the stored DAG's edge dicts to the attribute shape
    :func:`_diversity_warnings` expects, so creation-time (domain
    ``WorkflowEdge``) and run-time (JSONB dicts) share one implementation."""
    return [
        SimpleNamespace(
            from_node_id=str(edge.get("from_node_id")),
            to_node_id=str(edge.get("to_node_id")),
        )
        for edge in dag_payload.get("edges", [])
    ]


def _diversity_warnings(
    edges: Sequence[Any], templates: Mapping[str, AgentTemplate]
) -> list[DiversityWarning]:
    """Contrôleur/Producteur LLM-diversity warnings for a DAG (AC4, FR15, D84).

    Non-blocking by construction — it only ever RETURNS warnings, never
    raises. Only edges INCOMING to a ``controleur`` node are considered
    (producer -> controller), never outgoing ones. A duplicated
    ``(from_node_id, to_node_id)`` pair is checked once: the DAG does not
    forbid duplicate edges, but the same producer/controller pair should not
    surface the same warning twice.

    Extracted from :meth:`WorkflowService.create_workflow` so that
    :meth:`WorkflowExecutionService.start_run` can run the SAME check at run
    start — the second evaluation point ``epics.md`` assigns to this story
    (create-story note 4.1, point 4: *"Un contrôle à la création se périme
    [...] Prévoir un second point d'évaluation au démarrage du run (Story
    4.2)"*). ``edges`` is typed loosely because the two callers hold
    different edge shapes (domain ``WorkflowEdge`` at creation, the stored
    DAG's dicts at run start) with the same two attributes.
    """
    warnings: list[DiversityWarning] = []
    checked_pairs: set[tuple[str, str]] = set()
    for edge in edges:
        from_node_id, to_node_id = edge.from_node_id, edge.to_node_id
        controller_template = templates.get(to_node_id)
        producer_template = templates.get(from_node_id)
        if controller_template is None or producer_template is None:
            continue
        if controller_template.archetype != _CONTROLLER_ARCHETYPE:
            continue
        pair = (from_node_id, to_node_id)
        if pair in checked_pairs:
            continue
        checked_pairs.add(pair)
        result = check_llm_diversity(
            _llm_selection_from_config(controller_template.config or {}),
            _llm_selection_from_config(producer_template.config or {}),
        )
        if result.is_diverse is False:
            warnings.append(
                DiversityWarning(
                    controller_node_id=to_node_id,
                    producer_node_id=from_node_id,
                    controller_template_id=controller_template.id,
                    producer_template_id=producer_template.id,
                    reason=result.reason,
                )
            )
    return warnings


class WorkflowService:
    """Workflow creation — DAG validation + versioned persistence (Story 4.1)."""

    def __init__(self, *, workflow_repo: WorkflowRepo, template_repo: AgentTemplateRepo) -> None:
        self._workflow_repo = workflow_repo
        self._template_repo = template_repo

    async def create_workflow(
        self,
        *,
        name: str,
        nodes: list[WorkflowNodeRequest],
        edges: list[WorkflowEdgeRequest],
        tenant_id: UUID | None = None,
    ) -> CreateWorkflowResponse:
        """Validate + persist a new ``workflows`` row (AC1, AC2, AC3, AC4).

        Pipeline (fail-fast, 422 RFC 7807 for ANY invalid DAG content — cf
        Dev Notes § "422 partout", ``get_by_id`` is used instead of
        ``require_by_id`` so a bad ``agent_template_id`` inside the body
        never surfaces as a 404).

        Story 4.8 split the pipeline in two, and the split is the point:

        OUTSIDE any transaction, because none of it reads the database —

        1. Duplicate ``node_id``.
        2. Dangling edges (``from_node_id``/``to_node_id`` not declared).
        3. No cycle (``graphlib.TopologicalSorter``).
        4. Build the stored DAG payload and its request fingerprint.

        INSIDE a SINGLE transaction, so that what is validated is what is
        committed (4.8 AC2) —

        5. Resolve every ``agent_template_id`` in ONE batch query (4.8 AC3),
           holding ``FOR SHARE`` on the rows so a concurrent
           ``PUT /agents/templates/{id}`` cannot rewrite a ``config`` between
           this validation and the commit.
        6. Every edge ``condition`` is syntactically valid AND its variable
           is exposed by the emitting node's ``output_contract.core``.
        7. Non-blocking Contrôleur/Producteur LLM-diversity warning for every
           edge incident (entering) a ``controleur`` node (AC4, D84) —
           NEVER raises, only appends to ``warnings``.
        8. Persist atomically (row INSERT + outbox event, single transaction).

        Two consequences of that split, both deliberate:

        * A DAG that is BOTH cyclic and references an unknown template now
          reports the cycle (it used to report the template). Both stay 422s
          with the same RFC 7807 shape; only which one wins changed.
        * No call to an EXTERNAL service runs under the ``FOR SHARE`` lock —
          the window is a batch SELECT, pure-CPU validation, an INSERT and an
          outbox row. **Never introduce an LLM or MCP call inside this
          block**: it would make template writers wait on a provider.
          The window is not instantaneous, though, and saying "nothing here
          blocks" would be false: on a concurrent replay the loser's INSERT
          waits on ``uq_workflow_request_fingerprint`` until the winner's
          transaction ends, holding its template locks for that whole wait.
          Bounded by a peer transaction that does no I/O of its own, so it is
          short — and since Story 4.14 AC2 a ``lock_timeout`` caps it
          (``settings.workflow_create_lock_timeout_s``, 5s by default),
          turning what used to be an indefinite wait into a typed 503.

        Story 4.8 AC1 — a replayed request (same body, first response lost)
        collides on ``uq_workflow_request_fingerprint``, which aborts the
        whole transaction (so no second row AND no second event, by
        construction rather than by a guard) and comes back as a
        :class:`ConflictError`. The replay branch then returns the ORIGINAL
        workflow with ``200 OK`` instead of ``201``.

        What this method guarantees about templates ends at the commit. A
        template rewritten one second later leaves the stored DAG stale, and
        no lock can prevent that. Story 4.8 claimed three mechanisms already
        covered that horizon; **Story 4.14 T4.3 checked them and they do
        not** — each covers strictly less than the claim:

        - ``start_run`` re-evaluates diversity, but from the same in-memory
          ``templates`` dict it just read, so it cannot see an edit made
          after that read;
        - :func:`_template_fingerprints` records what each node ran, but the
          comparison (:meth:`_report_config_drift`) is reached only from
          ``_resume_run`` — a crash-resume or a manual resume, never a run
          that completes normally;
        - :func:`_load_templates` raises only if a template **vanished**,
          never if it was modified.

        A run that starts, has a template rewritten under it, and finishes
        without pausing or crashing therefore sees the drift reported
        nowhere. Real, open, and out of Story 4.14's scope — the full
        accounting is in ``docs/runbooks/repositories-usage.md``
        § *Batch reads, and when to lock them*.

        Raises:
            ValidationError: any of steps 1-3, 5 or 6 fails (422 RFC 7807).
            ConflictError: a fingerprint collision whose original row could
                not be re-read (409) — see the replay branch.
        """
        dag = WorkflowDag(
            nodes=tuple(
                WorkflowNode(node_id=n.node_id, agent_template_id=n.agent_template_id)
                for n in nodes
            ),
            edges=tuple(
                WorkflowEdge(
                    from_node_id=e.from_node_id, to_node_id=e.to_node_id, condition=e.condition
                )
                for e in edges
            ),
        )

        # ─── Structural validation — no DB read, so no transaction ───

        # 1. Duplicate node ids.
        duplicates = find_duplicate_node_ids(dag)
        if duplicates:
            raise ValidationError(
                detail=f"Duplicate node_id(s) in workflow DAG: {', '.join(duplicates)}",
                context={"duplicate_node_ids": duplicates},
            )

        # 2. Dangling edges — reference a node not declared in `nodes`.
        dangling = find_dangling_edges(dag)
        if dangling:
            raise ValidationError(
                detail=(
                    "Edge references an undeclared node: "
                    + ", ".join(f"{e.from_node_id} -> {e.to_node_id}" for e in dangling)
                ),
                context={
                    "dangling_edges": [
                        {"from_node_id": e.from_node_id, "to_node_id": e.to_node_id}
                        for e in dangling
                    ]
                },
            )

        # 3. Cycle detection.
        cycle = detect_cycle(dag)
        if cycle is not None:
            raise ValidationError(
                detail=f"Workflow DAG contains a cycle: {' -> '.join(cycle)}",
                context={"cycle_node_ids": cycle},
            )

        # 4. Stored payload + idempotence key (Story 4.8 AC1).
        event_type = WorkflowCreatedEvent.event_type
        dag_payload: dict[str, Any] = {
            "nodes": [
                {"node_id": n.node_id, "agent_template_id": str(n.agent_template_id)}
                for n in dag.nodes
            ],
            "edges": [
                {
                    "from_node_id": e.from_node_id,
                    "to_node_id": e.to_node_id,
                    "condition": e.condition,
                }
                for e in dag.edges
            ],
        }
        fingerprint = _request_fingerprint(name, dag_payload)

        # Bound before the `try` even though the only `ConflictError` emitter
        # in the block is step 8, which runs after step 7 has assigned it. A
        # future step raising earlier would turn the replay branch into a
        # `NameError`; this is the one-line insurance against that.
        warnings: list[DiversityWarning] = []

        try:
            # Story 4.14 AC2 — bound how long a LOSING replay can wait on
            # `uq_workflow_request_fingerprint` (Story 4.8 AC1) while still
            # holding `FOR SHARE` on every template this transaction
            # resolved (AC2 below, step 5). Scoped to this transaction only
            # (`lock_timeout_ms`, not a role-wide setting): unbounded before
            # this, a stalled winner parked the loser indefinitely and
            # queued any concurrent `PUT /agents/templates/{id}` behind it
            # for just as long.
            # `round`, not `int`: `int` truncates toward zero, and Postgres
            # reads `lock_timeout = 0` as DISABLED. `max(1, ...)` is the
            # belt to `ge=0.001`'s braces — neither alone should be relied
            # on, since the setting and this conversion can drift apart.
            lock_timeout_ms = max(1, round(settings.workflow_create_lock_timeout_s * 1000))
            async with self._workflow_repo.with_tenant(
                tenant_id, lock_timeout_ms=lock_timeout_ms
            ) as session:
                # 5. Batch-resolve every agent_template_id, locked (AC2, AC3).
                # ONE query for the whole DAG, `FOR SHARE` held until commit.
                resolved = await self._template_repo.list_by_ids_in_session(
                    session,
                    [node.agent_template_id for node in dag.nodes],
                    lock=True,
                )
                # The 422 stays per-node and reports the FIRST offending node
                # in declaration order — the batch changed the query, not the
                # error contract. Iterating `resolved` instead would make the
                # reported node depend on Postgres' return order.
                templates: dict[str, AgentTemplate] = {}
                for node in dag.nodes:
                    template = resolved.get(node.agent_template_id)
                    if template is None:
                        raise ValidationError(
                            detail=(
                                f"Node '{node.node_id}' references unknown agent_template_id "
                                f"'{node.agent_template_id}'"
                            ),
                            context={
                                "node_id": node.node_id,
                                "agent_template_id": str(node.agent_template_id),
                            },
                        )
                    templates[node.node_id] = template

                # 6. Branching-condition DSL syntax + AC3 variable-exposure check.
                for edge in dag.edges:
                    if edge.condition is None:
                        continue
                    try:
                        parsed = parse_condition(edge.condition)
                    except DomainValidationError as exc:
                        raise ValidationError(
                            detail=(
                                f"Invalid branching condition on edge "
                                f"'{edge.from_node_id} -> {edge.to_node_id}': {exc}"
                            ),
                            context={
                                "from_node_id": edge.from_node_id,
                                "to_node_id": edge.to_node_id,
                                "condition": edge.condition,
                            },
                        ) from exc

                    emitter = templates[edge.from_node_id]
                    emitter_config = emitter.config if isinstance(emitter.config, dict) else {}
                    output_contract = emitter_config.get("output_contract")
                    output_contract = output_contract if isinstance(output_contract, dict) else {}
                    core_vars = output_contract.get("core")
                    core_vars = core_vars if isinstance(core_vars, dict) else {}
                    if parsed.field not in core_vars:
                        raise ValidationError(
                            detail=(
                                f"Condition on edge '{edge.from_node_id} -> {edge.to_node_id}' "
                                f"references variable 'output.{parsed.field}', not exposed by node "
                                f"'{edge.from_node_id}'s output_contract.core"
                            ),
                            context={
                                "from_node_id": edge.from_node_id,
                                "to_node_id": edge.to_node_id,
                                "variable": parsed.field,
                            },
                        )

                # 7. Controller/producer LLM diversity — non-blocking (AC4, D84).
                # Only edges INCOMING to a `controleur` node are considered
                # (producer_node_id -> controller_node_id) — never outgoing edges.
                # A duplicated (from_node_id, to_node_id) pair is checked once —
                # the DAG doesn't forbid duplicate edges, but the same producer ->
                # controller pair shouldn't surface the same warning twice.
                warnings = _diversity_warnings(dag.edges, templates)

                # 8. Persist atomically (row INSERT + outbox event).
                workflow = await self._workflow_repo.create_in_session(
                    session,
                    name=name,
                    dag=dag_payload,
                    version=1,
                    tenant_id=tenant_id,
                    request_fingerprint=fingerprint,
                )
                event = WorkflowCreatedEvent(
                    workflow_id=workflow.id,
                    name=name,
                    version=workflow.version,
                    node_count=len(dag.nodes),
                    actor="system",
                    tenant_id=tenant_id,
                )
                # ORDER MATTERS (Story 4.8 AC1): the INSERT flushes first, so a
                # fingerprint collision aborts the transaction BEFORE this line
                # ever runs. That is what makes "no second event" a property of
                # the code's shape rather than a guard someone could delete.
                event_id = await publish(event_type, event, session=session)
                # commit happens at __aexit__ if no exception is raised.
        except ConflictError:
            # Story 4.8 AC1 — this exact request already produced a workflow.
            # Re-read it in a FRESH transaction (the one above is aborted) and
            # hand back the original instead of creating a twin.
            existing = await self._workflow_repo.get_by_request_fingerprint(
                fingerprint, tenant_id=tenant_id
            )
            if existing is None:
                # The row collided with is gone between the failed INSERT and
                # this read. Surface the 409 rather than retrying: a retry
                # loop here would race the same deletion forever.
                raise
            _log.info(
                "workflow_create_idempotent_replay",
                workflow_id=str(existing.id),
                name=name,
                request_fingerprint=fingerprint,
            )
            # `warnings` is recomputed from the CURRENT templates, not restored
            # from the original creation — they are never persisted, and a
            # diversity warning is an observation about the present state
            # rather than a property of the row. Same reason `start_run`
            # re-evaluates them at run start.
            return CreateWorkflowResponse(
                workflow_id=existing.id,
                version=existing.version,
                warnings=warnings,
                idempotent_replay=True,
            )

        # ─── Post-commit: best-effort NOTIFY ───
        await notify_best_effort(event_id, event_type)

        _log.info(
            "workflow_created",
            workflow_id=str(workflow.id),
            name=name,
            version=workflow.version,
            node_count=len(dag.nodes),
            warning_count=len(warnings),
            request_fingerprint=fingerprint,
        )

        return CreateWorkflowResponse(
            workflow_id=workflow.id,
            version=workflow.version,
            warnings=warnings,
            idempotent_replay=False,
        )


def _preview(value: Any) -> str:
    """Truncated JSON preview of a node's output — see
    ``_CHECKPOINT_PREVIEW_MAX_CHARS``. ``default=str`` covers any stray
    non-JSON-native value that slips into a node's best-effort-parsed output
    (this is a diagnostic preview, not a strict round-trip)."""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= _CHECKPOINT_PREVIEW_MAX_CHARS:
        return text
    return text[:_CHECKPOINT_PREVIEW_MAX_CHARS] + "…"


# Story 4.6 T10.2 — how much of a failed chain's per-attempt breakdown is
# persisted. Bounded on BOTH axes: a chain is at most 4 providers today
# (Story 2.2's `max_length=4`), and each field is truncated to the same 500
# chars the router already applies to `error_detail`. The `checkpoint` JSONB
# is read by the SSE `state` frame and the Trace Explorer — it must not grow
# without limit, and NFR9 forbids letting a provider's echoed prompt through
# unredacted.
_MAX_PERSISTED_ATTEMPTS: Final = 5
_ATTEMPT_FIELD_MAX_CHARS: Final = 500
_ATTEMPT_FIELDS: Final = (
    "provider",
    "model_attempted",
    "error_type",
    "error_detail",
    "error_class",
)


def _failure_attempts(exc: Exception) -> list[dict[str, str]]:
    """Extract and sanitise ``exc.context["attempts"]`` (Story 4.6 AC3).

    ``LLMAllProvidersFailedError`` carries one entry per provider the router
    tried. The router already redacts ``error_detail`` on its way in, but
    redaction is re-applied here rather than trusted: this value is about to
    be persisted AND streamed to SSE clients, and the cost of re-running a
    regex is nothing against the cost of leaking a DSN or an API key (NFR9).

    Returns ``[]`` for any exception that carries no such breakdown — which
    is most of them.
    """
    context = getattr(exc, "context", None)
    if not isinstance(context, dict):
        return []
    raw = context.get("attempts")
    if not isinstance(raw, list):
        return []
    sanitised: list[dict[str, str]] = []
    for entry in raw[:_MAX_PERSISTED_ATTEMPTS]:
        if not isinstance(entry, dict):
            continue
        sanitised.append(
            {
                field: redact_secrets(str(entry[field]))[:_ATTEMPT_FIELD_MAX_CHARS]
                for field in _ATTEMPT_FIELDS
                if field in entry
            }
        )
    return sanitised


def _failure_chain_traversals(exc: Exception) -> int | None:
    """``exc.context["chain_traversals"]`` when the node's retry loop set it.

    ``None`` for every exception that carries no such count — most of them,
    including any failure that never reached the LLM.
    """
    context = getattr(exc, "context", None)
    if not isinstance(context, dict):
        return None
    raw = context.get("chain_traversals")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None


def _latest_node_id(state_values: Mapping[str, Any]) -> str | None:
    """The most recently completed node, read off ``node_outputs``.

    Relies on the same insertion-order invariant ``_serialize_upstream``
    already documents ("`node_outputs` preserves completion order"), so the
    LAST key is the newest. Used where the applicative checkpoint's
    ``last_node_id`` is not in hand but the graph state is.
    """
    node_outputs = state_values.get("node_outputs")
    if not isinstance(node_outputs, dict) or not node_outputs:
        return None
    latest = next(reversed(node_outputs))
    return latest if isinstance(latest, str) else None


def _last_node_id(checkpoint: dict[str, Any] | None) -> str | None:
    """Best-effort ``checkpoint["last_node_id"]`` — the last node that
    COMPLETED, as stamped by :meth:`WorkflowExecutionService._sync_checkpoint`.

    ``None`` for a run whose first checkpoint sync never landed, or whose
    ``checkpoint`` JSONB is malformed. Never raises: every caller (Story 4.6's
    ``paused``/``cancelled`` events) treats this as a trace field, not as a
    resume cursor — resuming is LangGraph's own committed checkpoint's job.

    Mirror of ``recovery._resumed_from_node_id``; duplicated rather than
    imported, because ``recovery`` imports THIS module and the reverse
    direction would close a cycle.
    """
    if not isinstance(checkpoint, dict):
        return None
    value = checkpoint.get("last_node_id")
    return value if isinstance(value, str) else None


#: ``node_statuses`` values meaning "will not run again", so a resume's
#: remaining-work estimate must not re-price them. Written by
#: ``_sync_checkpoint`` (``"success"``) and ``_mark_completed``'s
#: truncated-branch detection (``"skipped"``). Any other value — a future
#: failed/retrying/partial marker — is still ahead of the resume.
_ACCOUNTED_NODE_STATUSES: Final = frozenset({"success", "skipped"})


def _already_executed_node_ids(checkpoint: dict[str, Any] | None) -> frozenset[str]:
    """Node ids a paused run has already completed (Story 4.12 AC5), read
    from ``checkpoint["node_statuses"]`` (stamped by ``_sync_checkpoint``
    after every completed superstep — ``dict.fromkeys(node_outputs,
    "success")``, plus a possible ``"skipped"`` entry from
    ``_mark_completed``'s truncated-branch detection).

    A run can only be `paused` between two supersteps, so every key here
    reflects real, already-billed work — never a hypothesis.

    The STATUS is checked, not just the key: a node recorded as failed,
    retrying or partial WILL execute again, and dropping it here would make
    `budget_available` under-price the resume and let through a run the cap
    should have refused.

    Best-effort like its sibling `_last_node_id`: a malformed or absent
    checkpoint yields an empty set, which reproduces the pre-4.12 behaviour
    (price the whole DAG) rather than raising on a resume.
    """
    if not isinstance(checkpoint, dict):
        return frozenset()
    node_statuses = checkpoint.get("node_statuses")
    if not isinstance(node_statuses, dict):
        return frozenset()
    return frozenset(
        node_id
        for node_id, status in node_statuses.items()
        if isinstance(node_id, str) and status in _ACCOUNTED_NODE_STATUSES
    )


def _dag_from_stored(payload: dict[str, Any]) -> WorkflowDag:
    """Reconstruct the domain :class:`WorkflowDag` from ``workflows.dag``
    JSONB — the exact inverse of ``WorkflowService.create_workflow``'s
    ``dag_payload`` shape (Story 4.1)."""
    return WorkflowDag(
        nodes=tuple(
            WorkflowNode(node_id=n["node_id"], agent_template_id=UUID(n["agent_template_id"]))
            for n in payload["nodes"]
        ),
        edges=tuple(
            WorkflowEdge(
                from_node_id=e["from_node_id"],
                to_node_id=e["to_node_id"],
                condition=e.get("condition"),
            )
            for e in payload["edges"]
        ),
    )


async def _load_templates(
    template_repo: AgentTemplateRepo, dag_payload: dict[str, Any], *, tenant_id: UUID | None
) -> dict[str, AgentTemplate]:
    """Preload every node's :class:`AgentTemplate` — ONE batch query for the
    whole DAG (Story 4.8 AC3).

    This used to issue one query per node, and said so under a "dette
    assumée" pointing at Story 4.8. It is the hotter of the two N+1s that
    story closed: ``create_workflow`` paid its N round trips once per
    workflow, this one pays them on every run AND every dry-run.

    Module-level (Story 4.4 T3.1 — promoted from a ``WorkflowExecutionService``
    private method, same behavior, no logic change) so :class:`.dry_run.DryRunService`
    can reuse it without duplicating the loop: one implementation, two callers,
    mirror the ``resolve_deterministic_targets`` extraction of Story 4.3 T4.1.

    No lock here, unlike ``create_workflow``'s resolution: this is a read on
    a hot path, not the pre-write validation whose coherence 4.8 AC2 had to
    guarantee. Verified, not assumed (Story 4.14 AC4): this transaction
    closes before ``_gate_on_mise_en_place`` (real external I/O) even runs,
    so a lock taken here would be released long before the ``workflow_runs``
    INSERT it could only matter for — making a lock the wrong tool, not
    merely an expensive one. Full accounting of what IS and is NOT caught by
    the run-time checks in ``docs/runbooks/repositories-usage.md`` § *Batch
    reads, and when to lock them*: the honest answer is narrower than "three
    mechanisms cover it" — a same-call, no-crash template edit racing this
    read is not caught by anything today.

    A missing template here is an INTERNAL INCONSISTENCY, not a client
    error: Story 4.1 validated every ``agent_template_id`` at creation and
    no delete endpoint exists, so reaching this branch means the stored
    DAG references a row that vanished. ``require_by_id`` reported that as
    a 404 on ``POST /workflows/{workflow_id}/runs``, whose 404 already
    means "unknown workflow_id" — telling the caller their perfectly valid
    workflow id was wrong, and inviting them to retry forever. 500 instead:
    the id in the URL is fine, the server's data is not.
    """
    nodes = dag_payload.get("nodes", [])
    # Parse every id up front, but keep the parse failure inside this
    # function's documented contract (review P7). Batching made the parsing
    # eager, and a bare `KeyError`/`ValueError` from a malformed stored node
    # would escape as a body-less 500 — where the per-node loop it replaced
    # used to reach the `InternalError` below first. A corrupt stored DAG is
    # the same class of server-side inconsistency either way; it must keep
    # the same RFC 7807 shape.
    try:
        node_ids = [UUID(node["agent_template_id"]) for node in nodes]
    except (KeyError, ValueError, TypeError) as exc:
        raise InternalError(
            detail="workflow DAG stores a malformed agent_template_id — stored DAG is inconsistent",
            context={"error": str(exc)},
        ) from exc
    resolved = await template_repo.list_by_ids(node_ids, tenant_id=tenant_id)
    # Walk the DAG in declaration order, not the resolved map: the node
    # reported as inconsistent must not depend on Postgres' return order.
    templates: dict[str, AgentTemplate] = {}
    for node, template_id in zip(nodes, node_ids, strict=True):
        template = resolved.get(template_id)
        if template is None:
            raise InternalError(
                detail=(
                    "workflow references an agent template that no longer exists "
                    "— stored DAG is inconsistent"
                ),
                context={
                    "node_id": str(node["node_id"]),
                    "agent_template_id": str(template_id),
                },
            )
        templates[node["node_id"]] = template
    return templates


#: Per-field cap on the free-text a check contributes to an outbox row,
#: mirroring the ``summary[:4000]`` the refusal event already applies. A run
#: can be resumed arbitrarily many times and each resume writes one of these,
#: so an unbounded copy of a 100-node workflow's check details is a growth
#: path in the same table this feature elsewhere purges.
_MAX_EVENT_CHECK_TEXT_CHARS: Final = 1_000


def _bounded_mise_en_place_payload(report: MiseEnPlaceReport) -> dict[str, Any]:
    """:func:`_mise_en_place_out` with every free-text field truncated.

    The API response keeps the full report; only the durable event copy is
    bounded. Codes, verdicts and ``retryable`` are untouched — they are what
    a consumer filters on, and they are already fixed-size.
    """
    payload = _mise_en_place_out(report).model_dump(mode="json")
    checks = payload.get("checks")
    if isinstance(checks, list):
        for check in checks:
            if not isinstance(check, dict):
                continue
            for field in ("detail", "suggested_action"):
                value = check.get(field)
                if isinstance(value, str):
                    check[field] = value[:_MAX_EVENT_CHECK_TEXT_CHARS]
    reason = payload.get("bypass_reason")
    if isinstance(reason, str):
        payload["bypass_reason"] = reason[:_MAX_EVENT_CHECK_TEXT_CHARS]
    return payload


def _mise_en_place_out(report: MiseEnPlaceReport) -> MiseEnPlaceReportOut:
    """Domain ``MiseEnPlaceReport`` -> API ``MiseEnPlaceReportOut`` (Story 4.5
    T5.1) — mirror the ``ProbablePathResult``/``DryRunResponse`` split of
    Story 4.4: the domain dataclass stays framework-free, this is the
    API/JSONB-facing shape."""
    return MiseEnPlaceReportOut(
        checks=[
            MiseEnPlaceCheckOut(
                code=check.code,
                passed=check.passed,
                detail=check.detail,
                suggested_action=check.suggested_action,
                retryable=check.retryable,
            )
            for check in report.checks
        ],
        all_passed=report.all_passed,
        bypassed=report.bypassed,
        bypass_reason=report.bypass_reason,
    )


def _aggregate_routing(routing_decisions: Mapping[str, Any]) -> dict[str, Any]:
    """``{deterministic, llm_escalated, tokens, cost_usd}`` from the run's
    ``routing_decisions`` state channel (Story 4.3 AC3 T9.4).

    A node absent from ``routing_decisions`` (every non-decision-point node,
    AC4) simply isn't counted — the denominator is "decisions actually
    taken", never "edges crossed" (Dev Notes § Définition du point de
    décision de routage).

    The token/cost block is the review's IG1 fix: escalation spend was
    dropped on the floor by ``_escalate`` and therefore missing from the
    run's ``total_cost_usd``, so a workflow whose whole selling point is
    "route deterministically, spend less" under-reported the cost of the very
    mechanism it was measuring. Kept as its OWN block rather than folded into
    ``per_node`` so the claim stays falsifiable: routing spend must remain
    separable from node spend.
    """
    deterministic = 0
    llm_escalated = 0
    input_tokens = 0
    output_tokens = 0
    cost = Decimal("0")
    any_cost = False
    for decision in routing_decisions.values():
        if not isinstance(decision, dict):
            continue
        mode = decision.get("mode")
        if mode == "deterministic":
            deterministic += 1
        elif mode == "llm_escalated":
            llm_escalated += 1
        input_tokens += int(decision.get("llm_input_tokens") or 0)
        output_tokens += int(decision.get("llm_output_tokens") or 0)
        cost_raw = decision.get("llm_cost_usd")
        if cost_raw is not None:
            any_cost = True
            cost += Decimal(str(cost_raw))
    return {
        "deterministic": deterministic,
        "llm_escalated": llm_escalated,
        "tokens": {"input": input_tokens, "output": output_tokens},
        "cost_usd": str(cost) if any_cost else None,
    }


def _coerce_token_count(value: Any) -> int:
    """One JSONB number as a non-negative int, or ``0`` for anything else.

    Review of 2026-09-12 (P-5). ``_aggregate_handoffs`` promised in its own
    docstring to be "defensive per-entry (a corrupt entry is skipped, never
    poisons the whole aggregate)" but only checked ``isinstance(entry, dict)``:
    ``int("n/a")`` raises ``ValueError`` and ``int({...})`` raises
    ``TypeError``. That mattered because ``_mark_completed`` calls
    ``_aggregate_metrics`` with NO ``try``/``except`` (unlike the partial path,
    which has one), so one corrupt value meant ``update_status("completed")``
    was never reached: the run stayed ``running``, got claimed by the recovery
    sweep, was RE-EXECUTED up to ``MAX_RECOVERY_ATTEMPTS`` and finally marked
    ``error`` — a run that had in fact succeeded.

    Negative values are floored at 0 for the same reason the SQL aggregate
    casts defensively: a token count below zero is corruption, and letting it
    through only moves the failure to the response schema's ``ge=0``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _aggregate_handoffs(
    handoffs: Mapping[str, Any], substitutions: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """``{raw_tokens_replaced, summary_tokens, reduction_ratio, tokens,
    cost_usd}`` from the run's ``handoffs`` state channel (Story 4.7 AC3).

    Mirror ``_aggregate_routing`` exactly: defensive per-entry (a corrupt
    entry is skipped, never poisons the whole aggregate), a SEPARATE block
    rather than folded into ``per_node`` totals — see below for why.

    The ratio compares OUTPUT sizes on both sides of the substitution: what
    the producing node ITSELF output (``raw_output_tokens_replaced`` — the
    volume that would have been forwarded raw to the next agent before this
    story) against what its summary output instead (``summary_output_tokens``
    — the volume actually forwarded). This is what FR53 means by "reduces
    the next agent's token consumption": the OUTPUT of one hop becomes the
    INPUT of the next, so shrinking that output is exactly what shrinks the
    next agent's bill. The summary call's OWN input cost
    (``summary_input_tokens`` — reading the raw output it condenses) is a
    real cost too, but it is the price of running the optimization, not the
    saving it produces — kept visible separately per-node (``_aggregate_metrics``)
    rather than netted into this ratio, which would make the ≥30% target
    unfalsifiable (a expensive-to-run summary could still report a "good"
    ratio if its own cost were absorbed into the same fraction).

    ``tokens``/``cost_usd`` (review of 2026-09-12, P-2) are what this block
    owes the run's TOTALS, and they are the summary call's FULL spend — input
    included. The ratio above and these totals answer two different questions:
    "did substituting shrink the payload?" and "what did this run actually
    cost?". Conflating them is exactly what left the second unanswerable.

    **The two sides come from two different channels** (review of 2026-09-12,
    B-01). ``handoffs`` is keyed by PRODUCER and says what summarizing cost:
    that is where ``tokens``/``cost_usd`` belong. ``handoff_substitutions`` is
    keyed by CONSUMER and says what substituting actually replaced, in a real
    prompt, after truncation: that is where the RATIO belongs. Reading the
    ratio off the producer side credited a saving three ways it never made —
    to a consumer that opted out and read the raw output anyway, to an entry
    the size cap dropped before any prompt saw it, and once for an output a
    chain forwards to every downstream node in turn. It was also, precisely,
    the number the story offered as proof of its ">= 30%" target.
    """
    substitutions = substitutions if isinstance(substitutions, dict) else {}
    raw_tokens_replaced = 0
    summary_tokens = 0
    for entry in substitutions.values():
        if not isinstance(entry, dict):
            continue
        raw_tokens_replaced += _coerce_token_count(entry.get("raw_tokens_replaced"))
        summary_tokens += _coerce_token_count(entry.get("summary_tokens"))

    input_tokens = 0
    output_tokens = 0
    cost = Decimal("0")
    any_cost = False
    for entry in handoffs.values():
        if not isinstance(entry, dict):
            continue
        input_tokens += _coerce_token_count(entry.get("summary_input_tokens"))
        output_tokens += _coerce_token_count(entry.get("summary_output_tokens"))
        cost_raw = entry.get("summary_cost_usd")
        if cost_raw is not None:
            try:
                cost += Decimal(str(cost_raw))
            except InvalidOperation:
                continue
            any_cost = True
    reduction_ratio = (
        round(1 - summary_tokens / raw_tokens_replaced, 4) if raw_tokens_replaced > 0 else None
    )
    # P-10 — a ratio below zero means the summaries came out BIGGER than the
    # outputs they replaced: the optimization is costing more than it saves.
    # The value is reported as measured (clamping would hide the only signal
    # that says so), but it is no longer reported in silence.
    if reduction_ratio is not None and reduction_ratio < 0:
        _log.warning(
            "workflow_engine.handoff_summary_inflated",
            raw_tokens_replaced=raw_tokens_replaced,
            summary_tokens=summary_tokens,
            reduction_ratio=reduction_ratio,
        )
    return {
        "raw_tokens_replaced": raw_tokens_replaced,
        "summary_tokens": summary_tokens,
        "reduction_ratio": reduction_ratio,
        "tokens": {"input": input_tokens, "output": output_tokens},
        "cost_usd": str(cost) if any_cost else None,
    }


def _aggregate_metrics(
    node_metrics: Mapping[str, Any],
    *,
    total_duration_ms: int,
    routing_decisions: Mapping[str, Any] | None = None,
    handoffs: Mapping[str, Any] | None = None,
    handoff_substitutions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate per-node metrics into the ``workflow_runs.metrics`` JSONB
    shape (AC4). Called on BOTH the completed path (full ``node_metrics``)
    and the failed path (only the already-executed nodes' metrics) — AC4
    requires partial metrics to remain aggregated on failure, not dropped.

    ``routing_decisions``/``handoffs`` default to an empty mapping (Story 4.3
    AC3/AC4, Story 4.7 AC3) — explicit parameters rather than derived
    internally so no test call site that predates either story needs to
    change; a workflow with no decision points / no handoffs reports the
    zeroed shape, never a missing key."""
    total_input = 0
    total_output = 0
    total_cost = Decimal("0")
    any_cost = False
    # P-5 — `… or {}` only guards `None`; a checkpoint holding a list here
    # used to raise on `.values()`/`.get()` inside an unprotected
    # `_mark_completed`. Same discipline `recovery.py` already applies to
    # every value it reads back out of a checkpoint.
    handoffs = handoffs if isinstance(handoffs, dict) else {}
    per_node: dict[str, Any] = {}
    for node_id, metric in node_metrics.items():
        metric = metric if isinstance(metric, dict) else {}
        input_tokens = int(metric.get("input_tokens") or 0)
        output_tokens = int(metric.get("output_tokens") or 0)
        total_input += input_tokens
        total_output += output_tokens
        cost_raw = metric.get("cost_usd")
        if cost_raw is not None:
            any_cost = True
            total_cost += Decimal(str(cost_raw))
        node_per_node: dict[str, Any] = {
            "duration_ms": metric.get("duration_ms"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_raw,
            "model_used": metric.get("model_used"),
            # Story 4.6 AC3 — how many times this node walked the FULL
            # provider chain. Projected here, not merely left in LangGraph
            # state, because `workflow_runs.metrics` is the only surface an
            # operator can actually query: a node that cost four chain
            # traversals otherwise looked identical to one that cost one,
            # with only its `duration_ms` hinting at the difference.
            # Defaults to 1 for rows written before this story.
            "llm_attempts": metric.get("llm_attempts", 1),
        }
        # Story 4.7 — the cost of producing THIS node's own handoff summary
        # (a separate LLM call, T3.2), same rationale as `llm_attempts`: the
        # only surface an operator can query. Absent (not `0`) for a node
        # that was never summarized (terminal node, or summarization failed)
        # — mirror `llm_attempts`' "defaults to 1 for rows written before
        # this story" posture, but as an absent key rather than a fabricated
        # default, since "never attempted" and "attempted for free" are
        # different facts.
        handoff_entry = handoffs.get(node_id)
        if isinstance(handoff_entry, dict):
            node_per_node["handoff_summary_tokens"] = {
                "input": _coerce_token_count(handoff_entry.get("summary_input_tokens")),
                "output": _coerce_token_count(handoff_entry.get("summary_output_tokens")),
            }
            # P-2 — the per-node surface an operator queries owes the cost as
            # well as the tokens, exactly like `cost_usd` beside it.
            node_per_node["handoff_summary_cost_usd"] = handoff_entry.get("summary_cost_usd")
            # I-03 — the Dry Run prices historical spend against the model it
            # was actually spent on, mirror `routing_decisions.llm_model`.
            node_per_node["handoff_summary_model"] = handoff_entry.get("summary_model")
        per_node[node_id] = node_per_node
    # IG1 — routing escalations are LLM calls this run paid for, so they
    # belong in the run's totals. `per_node` keeps describing NODE execution
    # only; the `routing` block below carries the same figures separately, so
    # the two are addable and comparable rather than conflated.
    routing = _aggregate_routing(routing_decisions or {})
    total_input += int(routing["tokens"]["input"])
    total_output += int(routing["tokens"]["output"])
    if routing["cost_usd"] is not None:
        any_cost = True
        total_cost += Decimal(str(routing["cost_usd"]))
    # Review of 2026-09-12 (P-2) — IG1 above, applied to Story 4.7's own extra
    # call. A handoff summary is an LLM call this run paid for, on every
    # non-terminal node, so on a 10-node linear DAG the run billed 19 calls and
    # reported 10: `total_cost_usd` stayed byte-identical to a pre-4.7 run while
    # the provider invoice did not. Any budget, overrun alert or per-run
    # rebilling built on this field was wrong by roughly half the call count.
    # The `handoffs` block below still carries the same figures separately, so
    # node spend, routing spend and summary spend stay addable and comparable
    # rather than conflated — the same shape IG1 settled on for routing.
    handoff_block = _aggregate_handoffs(handoffs, handoff_substitutions)
    total_input += int(handoff_block["tokens"]["input"])
    total_output += int(handoff_block["tokens"]["output"])
    if handoff_block["cost_usd"] is not None:
        any_cost = True
        total_cost += Decimal(str(handoff_block["cost_usd"]))
    return {
        "total_duration_ms": total_duration_ms,
        "total_tokens": {"input": total_input, "output": total_output},
        "total_cost_usd": str(total_cost) if any_cost else None,
        "per_node": per_node,
        "routing": routing,
        "handoffs": handoff_block,
    }


class WorkflowExecutionService:
    """Execute a persisted workflow end-to-end via the LangGraph engine
    (Story 4.2 AC1-AC4) — checkpointed natively at every node, resumable
    after a crash (T5.5, delegated to by :class:`.recovery.WorkflowRecoveryWorker`).

    Simplification note (Completion Notes) — the row-INSERT + outbox-event
    atomicity pattern used everywhere else in this codebase (``with_tenant``
    single transaction) is kept ONLY for :meth:`start_run` (the ``started``
    row + event, where the recommendation was explicit in Dev Notes). The
    ``step_completed``/``completed``/``failed`` transitions update the row
    then publish the event as two separate steps: none of them are
    client-facing writes a caller waits on, the run's authoritative status
    always lives in ``workflow_runs.status`` (read directly by the SSE
    endpoint, T8.2, not reconstructed from the event stream), and a dropped
    event only delays — never loses — API-observable staleness detection
    (AC3's recovery worker keys off ``last_checkpoint_at``, not off events).
    """

    def __init__(
        self,
        *,
        workflow_repo: WorkflowRepo,
        workflow_run_repo: WorkflowRunRepo,
        template_repo: AgentTemplateRepo,
        llm_router: LLMRouter,
        checkpointer: AsyncPostgresSaver,
        routing_rules: Sequence[RoutingRule],
        mise_en_place_service: MiseEnPlaceService,
    ) -> None:
        self._workflow_repo = workflow_repo
        self._workflow_run_repo = workflow_run_repo
        self._template_repo = template_repo
        self._llm_router = llm_router
        self._checkpointer = checkpointer
        # Story 4.5 T3.8 — the pre-workflow hook `start_run` calls between
        # the diversity check and the row INSERT (AC1/AC2/AC3).
        self._mise_en_place_service = mise_en_place_service
        # Story 4.3 T9.1/T9.3 — loaded once at boot (`app.lifespan`), never
        # per-run. REQUIRED, not defaulted to `()`: forgetting `routing_settings`
        # is already a hard `ValueError` in `build_state_graph`, while an empty
        # catalog used to be accepted in silence — and the silent one is the
        # EXPENSIVE failure, since no rule can ever match and every decision
        # point escalates to the LLM. An empty tuple remains a legitimate
        # explicit choice; only forgetting the argument is now impossible.
        # `RoutingSettings` (the deployment KNOBS, as opposed to the
        # rules themselves) is built fresh at the start of every run instead
        # (see `_execute`) — not stashed here — because Dev Notes T5.1 is
        # explicit that it must never be read from inside the engine, and
        # building it once per run (not once per process) keeps a `settings`
        # override picked up by a hot-reloading test fixture honest.
        self._routing_rules = routing_rules

    async def start_run(
        self,
        *,
        workflow_id: UUID,
        run_input: dict[str, Any],
        tenant_id: UUID | None = None,
        force: bool = False,
        reason: str | None = None,
    ) -> StartRunResponse:
        """Create a ``running`` ``workflow_runs`` row and spawn its execution
        as a background task (AC1). Returns immediately — the HTTP request
        never blocks on the run's progress.

        ``force``/``reason`` (Story 4.5 AC3) — bypass a failing Mise en
        Place pre-workflow check. Defensive re-validation of "``reason``
        required when ``force``" lives HERE too (not only in
        ``StartRunRequest``'s Pydantic validator), since this method is a
        real API surface a caller can reach directly.

        Raises:
            NotFoundError: ``workflow_id`` does not reference an existing
                workflow (404 — the URL's primary resource, cf Dev Notes
                § "404 vs 422").
            ValidationError: the workflow exists but ``status != "active"``
                (422), OR ``force=True`` with a blank/absent ``reason``
                (422, AC3 — checked unconditionally, whether or not any
                Mise en Place check actually fails).
            DependencyError: a Mise en Place check failed and ``force`` was
                not set (503, AC2) — no ``workflow_runs`` row is created.
        """
        workflow = await self._workflow_repo.require_by_id(workflow_id, tenant_id=tenant_id)
        if workflow.status != "active":
            raise ValidationError(
                detail=f"Workflow '{workflow_id}' is not active (status={workflow.status!r})",
                context={"workflow_id": str(workflow_id), "status": workflow.status},
            )

        # Story 4.9 AC2 — cheap early refusal before the expensive Mise en
        # Place gate below runs at all.
        await self._ensure_run_capacity(workflow_id=workflow_id, tenant_id=tenant_id)

        templates = await _load_templates(self._template_repo, workflow.dag, tenant_id=tenant_id)
        correlation_id = UUID(require_correlation_id())

        # Second evaluation point for Contrôleur/Producteur LLM diversity —
        # assigned to this story by `epics.md` (create-story note 4.1, point
        # 4) and implemented nowhere: `check_llm_diversity` was only ever
        # called at creation. The gap matters precisely because
        # `agent_templates` rows are mutable — a workflow validated as
        # diverse at creation can be running two identical models by the time
        # anyone starts it. Non-blocking, exactly like AC4's creation-time
        # check: reported, never a refusal.
        warnings = _diversity_warnings(_stored_edges(workflow.dag), templates)
        if warnings:
            _log.warning(
                "workflow_run_llm_diversity_degraded",
                workflow_id=str(workflow_id),
                pairs=[(w.producer_node_id, w.controller_node_id) for w in warnings],
            )

        # Story 4.5 AC1-AC3 — Mise en Place pre-workflow hook. Runs BEFORE
        # the `workflow_runs` row exists (préambule point 2): `workflow`,
        # `templates` and `correlation_id` are already resolved above, and
        # nothing is persisted yet — the necessary condition for AC2's "the
        # workflow does not start" to hold structurally rather than by
        # convention (a row created first would already be `running` by the
        # time a check failed).
        report, bypassed = await self._gate_on_mise_en_place(
            workflow_id=workflow_id,
            templates=templates,
            task_input=run_input,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            force=force,
            reason=reason,
        )

        event_type = WorkflowRunStartedEvent.event_type
        async with self._workflow_run_repo.with_tenant(tenant_id) as session:
            # Authoritative recount (Story 4.9 AC2/T2.2), scoped to the SAME
            # transaction as the INSERT below — the early check above only
            # short-circuits the common case, this is the one that actually
            # gates the write.
            running_count = await self._workflow_run_repo.count_running_in_session(
                session, workflow_id=workflow_id, alive_since=self._alive_since()
            )
            self._raise_if_over_capacity(running_count, workflow_id=workflow_id)
            run = await self._workflow_run_repo.create_in_session(
                session,
                workflow_id=workflow_id,
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                # Preserve the caller's input so a run that crashes BEFORE
                # LangGraph writes its first checkpoint can still be restarted
                # from START (AC3's "crashé avant tout checkpoint"). Overwritten
                # by `_sync_checkpoint` as soon as the first node lands, at
                # which point LangGraph owns the state and this is dead weight.
                checkpoint={
                    "task_input": run_input,
                    TEMPLATE_FINGERPRINTS_KEY: _template_fingerprints(templates),
                },
                # The SAME shape the API returns, not `asdict(report)`
                # (review P13). `all_passed` is a computed `@property`, so
                # `asdict` dropped it — leaving the JSONB unvalidatable by
                # `MiseEnPlaceReportOut`, the model that documents itself as
                # the persisted shape, and un-queryable via
                # `mise_en_place->>'all_passed'`. One shape, both surfaces.
                mise_en_place=_mise_en_place_out(report).model_dump(mode="json"),
            )
            event = WorkflowRunStartedEvent(
                run_id=run.id, workflow_id=workflow_id, tenant_id=tenant_id
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=correlation_id
            )
            bypass_event_id: UUID | None = None
            # The reason travels on the report itself — `_gate_on_mise_en_place`
            # owns the validated, redacted value and returns it there.
            if bypassed and report.bypass_reason is not None:
                bypass_event = WorkflowRunMiseEnPlaceBypassedEvent(
                    run_id=run.id,
                    workflow_id=workflow_id,
                    reason=report.bypass_reason,
                    failed_checks=[check.code for check in report.checks if not check.passed],
                    tenant_id=tenant_id,
                )
                bypass_event_id = await publish(
                    WorkflowRunMiseEnPlaceBypassedEvent.event_type,
                    bypass_event,
                    session=session,
                    correlation_id=correlation_id,
                )
            # commit happens at __aexit__ if no exception is raised.

        await notify_best_effort(event_id, event_type)
        if bypass_event_id is not None:
            # Notified like `started`, instead of waiting for the outbox
            # poll (review P11). An explicit human bypass is the event an
            # audit consumer most needs promptly — it had the longest
            # latency of the two published here.
            await notify_best_effort(
                bypass_event_id, WorkflowRunMiseEnPlaceBypassedEvent.event_type
            )

        task = asyncio.create_task(
            self._drive_run(run.id, workflow, templates, run_input, correlation_id=correlation_id),
            name=f"workflow-run-{run.id}",
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        _log.info(
            "workflow_run_started",
            run_id=str(run.id),
            workflow_id=str(workflow_id),
            node_count=len(templates),
        )
        return StartRunResponse(
            run_id=run.id,
            status="running",
            warnings=warnings,
            mise_en_place=_mise_en_place_out(report),
        )

    async def request_run_control(
        self,
        *,
        run_id: UUID,
        action: RunAction,
        tenant_id: UUID | None = None,
        force: bool = False,
        reason: str | None = None,
    ) -> RunControlResponse:
        """Apply a ``pause``/``resume``/``cancel`` request to a run (AC1).

        Two shapes of outcome, decided entirely by
        :func:`~..domain.run_control.resolve_transition`:

        * DEFERRED (``pause``/``cancel`` on a live run) — writes
          ``control_signal`` and leaves ``status`` alone. The driver settles
          it at its next superstep boundary, which is bounded by a node's
          duration, never instantaneous. HTTP ``202``.
        * IMMEDIATE (``resume``, and ``cancel`` on an already-``paused``
          run) — moves the row here and now, because no driver is alive to
          observe anything.

        Every write is a compare-and-set and a 0 rowcount is treated exactly
        like an illegal starting status: ``ConflictError`` (409) and **no
        event published**. That is the discipline ``_mark_completed``/
        ``_mark_failed`` already enforce — an event announcing a transition
        that did not happen tells every SSE client and every downstream
        consumer the opposite of what the row says.
        """
        run = await self._workflow_run_repo.get_by_id(run_id, tenant_id=tenant_id)
        if run is None:
            # 404, symmetric with `POST /workflows/{workflow_id}/runs`
            # (decision 4.2): the id is the URL's primary resource.
            raise NotFoundError(
                detail=f"Workflow run '{run_id}' not found", context={"run_id": str(run_id)}
            )

        try:
            transition = resolve_transition(action, run.status)
        except ConflictError as exc:
            # The pure function cannot know the run id nor the pending
            # signal; the 409 body must carry both (AC1's `context`
            # contract, as revised by `BS1`).
            exc.context["run_id"] = str(run_id)
            # Review lot 9 (F3). `pending_control_signal` used to exist ONLY
            # on the rowcount-0 branch below — and the scenario `BS1` added
            # it for lands HERE instead. A process dying between writing a
            # signal and observing it leaves the run `running` with a
            # pending `pause`; the operator's next move is `resume`, which
            # is illegal from `running`, so it took this path and got a 409
            # that could not say why the run was stuck. The field answered
            # the CAS race and missed the deadlock it was named for.
            exc.context["pending_control_signal"] = run.control_signal
            raise

        ended_at = datetime.now(UTC) if transition.immediate_status == "cancelled" else None
        event_type, event = self._event_for_transition(
            action, run=run, transition=transition, tenant_id=tenant_id
        )

        # A resume's dependencies are loaded BEFORE anything is written. Both
        # calls can raise — the workflow may have been deleted, or a template
        # the DAG references removed, while the run sat paused — and they used
        # to run AFTER the row had moved to `running` and the `resumed` event
        # had been published and notified. The caller then got a 404 on a run
        # that was already `running` with no driver behind it: unpausable
        # (nobody observes the signal), unresumable (`resume` from `running`
        # is a 409), and left to the recovery sweep. Failing here costs
        # nothing — the row has not moved.
        resume_context = (
            await self._load_resume_context(run, tenant_id=tenant_id)
            if action == "resume"
            else None
        )
        # Review lot 9 (F1). `_gate_on_mise_en_place`'s return value was
        # DISCARDED here, so a resume that overrode a red pre-flight left no
        # trace at all — while the REFUSAL path inside the same function is
        # audited (`_audit_mise_en_place_refusal`). A refused resume reached
        # the bus and a forced one did not, which is the wrong way round for
        # the only one of the two a human had to decide.
        resume_report: MiseEnPlaceReport | None = None
        resume_bypassed = False
        if resume_context is not None:
            # Story 4.6 review, `IG2` — a resume re-runs the pre-flight, with
            # the same bypass vector as a launch. An environment decays while
            # a run sits paused (a rotated key, a decommissioned MCP server,
            # an exhausted budget), and an ungated resume walked straight
            # into it: the row moved to `running`, the first node raised, and
            # `_mark_failed` closed the run `error` — destroying a checkpoint
            # that was still resumable a second earlier. Refusing here leaves
            # the row `paused`, so the run survives its environment.
            #
            # Deliberately AFTER `_load_resume_context` (which supplies the
            # templates the checks run against) and BEFORE any write, so a
            # refusal costs nothing. The recovery worker stays UNGATED: a
            # sweep that refuses to resume would leave orphans stranded,
            # which is the opposite of what it exists for.
            workflow_for_resume, templates_for_resume = resume_context
            # Story 4.9 AC2/T2.3 — same profile of cost as `start_run`'s
            # Mise en Place gate (up to `_MAX_CONCURRENT_PROBES` MCP pings
            # plus a Dry Run), so the same cheap early refusal applies here
            # before paying for it.
            await self._ensure_run_capacity(workflow_id=workflow_for_resume.id, tenant_id=tenant_id)
            resume_report, resume_bypassed = await self._gate_on_mise_en_place(
                workflow_id=workflow_for_resume.id,
                templates=templates_for_resume,
                task_input={},
                tenant_id=tenant_id,
                correlation_id=run.correlation_id,
                force=force,
                reason=reason,
                run_id=run_id,
                already_executed_node_ids=_already_executed_node_ids(run.checkpoint),
            )

        async with self._workflow_run_repo.with_tenant(tenant_id) as session:
            if action == "resume":
                # Authoritative recount (Story 4.9 AC2/T2.3), scoped to the
                # SAME transaction as the status UPDATE below.
                running_count = await self._workflow_run_repo.count_running_in_session(
                    session,
                    workflow_id=run.workflow_id,
                    alive_since=self._alive_since(),
                )
                self._raise_if_over_capacity(running_count, workflow_id=run.workflow_id)
            if transition.retracts_signal:
                # Story 4.9 AC6/T6.5 — third shape, neither DEFERRED nor
                # IMMEDIATE: clears whatever `pause`/`cancel` is currently
                # pending, guarded by the repo on `control_signal IS NOT
                # NULL` so a 0 rowcount means "nothing left to retract"
                # (already observed, or never was pending), never "erased
                # something a concurrent request just wrote".
                rowcount = await self._workflow_run_repo.retract_control_in_session(
                    session, run_id, only_if_status=run.status
                )
            elif transition.immediate_status is None:
                if transition.signal is None:  # pragma: no cover — table invariant
                    raise InternalError(detail="control transition has neither signal nor status")
                rowcount = await self._workflow_run_repo.request_control_in_session(
                    session,
                    run_id,
                    signal=transition.signal,
                    only_if_status=run.status,
                    # `cancel` may replace a pending `pause` (and only that).
                    # Without it, "pause… no, kill it" answered 409 for as
                    # long as the node took, and a signal left behind by a
                    # dead process locked every control action on the run
                    # until the recovery sweep.
                    overrides=transition.overrides,
                )
            else:
                rowcount = await self._workflow_run_repo.update_status_in_session(
                    session,
                    run_id,
                    status=transition.immediate_status,
                    ended_at=ended_at,
                    only_if_status=run.status,
                    clear_control=True,
                    # A resume goes back to `running`, so the sweep must see
                    # it as live activity — without this stamp,
                    # `claim_stale_running` reclaims a long-paused run within
                    # one tick and drives it a second time in parallel.
                    touch_last_checkpoint=transition.immediate_status == "running",
                )
            if rowcount == 0:
                # Lost a race: a concurrent control call got there first, the
                # run reached a terminal status between the read and the
                # write, or a signal was already pending (`request_control`
                # refuses to overwrite one). Same answer as an illegal
                # status — and nothing published, since the transaction the
                # `publish` below would have joined is about to be rolled
                # back by this raise anyway.
                #
                # Re-read before describing the conflict. The snapshot above
                # is what the caller raced AGAINST, so reporting it here
                # answered with the very state that is no longer true: two
                # concurrent `pause`s produced a 409 saying
                # `pending_control_signal: null` — "nothing is pending" —
                # while a pause was, which is the one question this body
                # exists to answer. A run that finished mid-flight likewise
                # reported `current_status: "running"`.
                current = await self._workflow_run_repo.get_by_id_in_session(session, run_id)
                observed_status = current.status if current is not None else run.status
                observed_signal = current.control_signal if current is not None else None
                # ISO-8601 TEXT, never a raw `datetime`: `context` is copied
                # verbatim into the RFC 7807 body and handed to
                # `JSONResponse`, whose `render` is a bare `json.dumps` with
                # no `default=` — a `datetime` raises `TypeError` INSIDE the
                # exception handler and the caller gets a 500, not this 409.
                observed_requested_at = (
                    current.control_requested_at.isoformat()
                    if current is not None and current.control_requested_at is not None
                    else None
                )
                # `retract` reaches this branch on the ordinary "nothing was
                # pending" case: the status IS allowed, only the write's
                # `control_signal IS NOT NULL` predicate failed. The generic
                # message would tell the caller its state changed
                # concurrently while also reporting a status that
                # `allowed_from` lists — contradictory, and never the real
                # reason.
                nothing_was_pending = (
                    transition.retracts_signal
                    and observed_signal is None
                    and observed_status in transition.allowed_from
                )
                if nothing_was_pending:
                    detail = f"cannot {action} run '{run_id}': no control request is pending on it"
                    conflict_reason = "no_pending_control_request"
                else:
                    detail = (
                        f"cannot {action} run '{run_id}': its state changed concurrently "
                        "or a control request is already pending"
                    )
                    conflict_reason = "state_changed_or_already_pending"
                raise ConflictError(
                    detail=detail,
                    context={
                        "run_id": str(run_id),
                        "current_status": observed_status,
                        "allowed_from": list(transition.allowed_from),
                        "action": action,
                        # Machine-readable counterpart of `detail`.
                        "conflict_reason": conflict_reason,
                        # WHICH request is pending. Without it the caller was
                        # told only that "a control request is already
                        # pending", with no way to tell a race from a signal
                        # nobody will ever observe — and no way to know
                        # whether escalating to `cancel` would get through.
                        "pending_control_signal": observed_signal,
                        # Story 4.9 AC6/T6.1 — WHEN it started being pending,
                        # so a caller can tell "just asked, still within a
                        # node's normal duration" from "asked long enough ago
                        # that the driver may be dead" without cross-
                        # referencing the runbook's ~1548s worst case by hand.
                        "pending_control_requested_at": observed_requested_at,
                    },
                )
            # Same transaction as the write (mirror `start_run`) — the event
            # and the row commit together or not at all. `retract` publishes
            # nothing (see above), so both are `None` on that path.
            event_id: UUID | None = None
            if event_type is not None and event is not None:
                event_id = await publish(
                    event_type, event, session=session, correlation_id=run.correlation_id
                )
            bypass_event_id, resume_evaluated_event_id = await self._publish_resume_gate_events(
                session,
                run_id=run_id,
                run=run,
                resume_report=resume_report,
                resume_bypassed=resume_bypassed,
                tenant_id=tenant_id,
            )

        if event_id is not None and event_type is not None:
            await notify_best_effort(event_id, event_type)
        if bypass_event_id is not None:
            await notify_best_effort(
                bypass_event_id, WorkflowRunMiseEnPlaceBypassedEvent.event_type
            )
        if resume_evaluated_event_id is not None:
            await notify_best_effort(
                resume_evaluated_event_id, WorkflowRunResumeMiseEnPlaceEvaluatedEvent.event_type
            )

        if resume_context is not None:
            self._launch_resume(run, *resume_context)

        _log.info(
            "workflow_run_control_requested",
            run_id=str(run_id),
            action=action,
            from_status=run.status,
            deferred=transition.immediate_status is None,
        )
        return RunControlResponse(
            run_id=run_id,
            status=transition.immediate_status or run.status,
            control_signal=transition.signal,
        )

    @staticmethod
    async def _publish_resume_gate_events(
        # `Any`, not `AsyncSession` — `import-linter` Contract 3 forbids
        # `features/*` from importing sqlalchemy even under `TYPE_CHECKING`
        # (a static AST edge, not a runtime one; mirrors the same
        # constraint noted in `shared/repositories/workflow_repo.py`).
        session: Any,
        *,
        run_id: UUID,
        run: WorkflowRun,
        resume_report: MiseEnPlaceReport | None,
        resume_bypassed: bool,
        tenant_id: UUID | None,
    ) -> tuple[UUID | None, UUID | None]:
        """Publish the resume's Mise en Place audit trail, in the SAME
        session/transaction as the row write (mirror ``start_run``).

        Returns ``(bypass_event_id, resume_evaluated_event_id)``, both
        ``None`` when ``resume_report`` is ``None`` (not a resume, or
        ``retract``/``pause``/``cancel`` on this call).
        """
        bypass_event_id: UUID | None = None
        if resume_bypassed and resume_report is not None and resume_report.bypass_reason:
            # Byte-for-byte the event `start_run` publishes for the same
            # decision — same type, same fields, same transaction. An
            # audit consumer must not have to know whether the human
            # overrode a launch or a resume to find the override.
            bypass_event_id = await publish(
                WorkflowRunMiseEnPlaceBypassedEvent.event_type,
                WorkflowRunMiseEnPlaceBypassedEvent(
                    run_id=run_id,
                    workflow_id=run.workflow_id,
                    reason=resume_report.bypass_reason,
                    failed_checks=[
                        check.code for check in resume_report.checks if not check.passed
                    ],
                    tenant_id=tenant_id,
                ),
                session=session,
                correlation_id=run.correlation_id,
            )
        resume_evaluated_event_id: UUID | None = None
        if resume_report is not None:
            # Story 4.12 AC3 — EVERY resume that proceeds, not just a
            # bypassed one: `workflow_runs.mise_en_place` stays the
            # immutable launch record (never overwritten here), so this
            # is where "which report actually authorized THIS resume"
            # is durably recorded instead.
            resume_evaluated_event_id = await publish(
                WorkflowRunResumeMiseEnPlaceEvaluatedEvent.event_type,
                WorkflowRunResumeMiseEnPlaceEvaluatedEvent(
                    run_id=run_id,
                    workflow_id=run.workflow_id,
                    all_passed=resume_report.all_passed,
                    bypassed=resume_bypassed,
                    failed_checks=[
                        check.code for check in resume_report.checks if not check.passed
                    ],
                    mise_en_place=_bounded_mise_en_place_payload(resume_report),
                    tenant_id=tenant_id,
                ),
                session=session,
                correlation_id=run.correlation_id,
            )
        return bypass_event_id, resume_evaluated_event_id

    @classmethod
    def _event_for_transition(
        cls,
        action: RunAction,
        *,
        run: WorkflowRun,
        transition: Transition,
        tenant_id: UUID | None,
    ) -> tuple[str, BaseModel]:
        """The event one control transition publishes.

        ``retract`` is the one shape :meth:`_control_event` cannot express —
        it announces no state change, only the withdrawal of a request. It
        still publishes: the ``pause_requested``/``cancel_requested`` it
        undoes is a durable outbox row, so leaving the withdrawal unrecorded
        makes the bus say a cancellation was asked for on a run that then
        completed normally, with nothing saying who took it back. A client
        watching the SSE ``state`` frame sees the signal disappear, but only
        if it was connected at that moment — which is not an audit trail.

        ``run.control_signal`` is the snapshot read at entry. The write this
        event accompanies is guarded on ``control_signal IS NOT NULL``, so a
        signal that moved in between makes that write match nothing and
        nothing is published at all.
        """
        if transition.retracts_signal:
            return WorkflowRunControlRetractedEvent.event_type, WorkflowRunControlRetractedEvent(
                run_id=run.id,
                workflow_id=run.workflow_id,
                retracted_signal=run.control_signal or "unknown",
                tenant_id=tenant_id,
            )
        return cls._control_event(
            action, run=run, transition_status=transition.immediate_status, tenant_id=tenant_id
        )

    @staticmethod
    def _control_event(
        action: RunAction,
        *,
        run: WorkflowRun,
        transition_status: str | None,
        tenant_id: UUID | None,
    ) -> tuple[str, BaseModel]:
        """The event for one control transition — built BEFORE the write so
        the transactional block stays a write + a publish and nothing else."""
        if action == "resume":
            # Reuses Story 4.2's existing event rather than inventing a
            # `user_resumed` twin: what happened is identical — the run
            # restarts from its last checkpoint, only the trigger differs.
            #
            # An earlier version of this comment added "and `correlation_id`
            # already distinguishes the two". It does NOT (review of
            # 2026-09-12): this publishes with `run.correlation_id`, and so
            # does `recovery._resume_one`, both being the run's original
            # LAUNCH id. The two events are byte-identical on the bus, so an
            # operator cannot tell a human resume from a sweep reclaim —
            # the first question asked when a run's cost doubles overnight.
            # Left as-is deliberately; closing it means a new event or field,
            # or using the HTTP request's id, which would break the "every
            # event of a run carries the run's correlation id" trace model
            # relied on since 4.2. Recorded as a `defer`.
            return WorkflowRunResumedEvent.event_type, WorkflowRunResumedEvent(
                run_id=run.id,
                workflow_id=run.workflow_id,
                resumed_from_node_id=_last_node_id(run.checkpoint),
                tenant_id=tenant_id,
            )
        if action == "pause":
            return WorkflowRunPauseRequestedEvent.event_type, WorkflowRunPauseRequestedEvent(
                run_id=run.id, workflow_id=run.workflow_id, tenant_id=tenant_id
            )
        if transition_status == "cancelled":
            # Terminal on the spot — no gap between request and effect to
            # record, so this publishes the EFFECT, not a request.
            #
            # The duration comes from the metrics the DRIVER aggregated when
            # it paused, never from `now() - run.started_at`. That subtraction
            # folds the idle window into the run's execution time — a run
            # paused on Monday and cancelled on Thursday reported three days
            # — and `_execute` documents the invariant it breaks: `started_at`
            # is the start of an execution SEGMENT, never
            # `workflow_runs.started_at`. Falls back to 0 rather than to a
            # wall-clock number that is knowably wrong.
            metrics = run.metrics if isinstance(run.metrics, dict) else {}
            raw_duration = metrics.get("total_duration_ms")
            duration_ms = raw_duration if isinstance(raw_duration, int) else 0
            return WorkflowRunCancelledEvent.event_type, WorkflowRunCancelledEvent(
                run_id=run.id,
                workflow_id=run.workflow_id,
                cancelled_at_node_id=_last_node_id(run.checkpoint),
                total_duration_ms=max(duration_ms, 0),
                tenant_id=tenant_id,
            )
        return WorkflowRunCancelRequestedEvent.event_type, WorkflowRunCancelRequestedEvent(
            run_id=run.id, workflow_id=run.workflow_id, tenant_id=tenant_id
        )

    async def _load_resume_context(
        self, run: WorkflowRun, *, tenant_id: UUID | None
    ) -> tuple[Workflow, dict[str, AgentTemplate]]:
        """Load what re-driving a resumed run needs — BEFORE any write.

        Same calls as ``WorkflowRecoveryWorker._resume_one``, but the worker
        is deliberately NOT imported: sweeping for orphans and serving a
        user's resume are distinct responsibilities that merely share a
        destination.

        Split out from :meth:`_launch_resume` so it can fail while the row is
        still ``paused``. Both calls raise on a workflow deleted or a
        template removed during the pause, and running them after the status
        write left a `running` row that no task was driving.

        Deactivating a workflow governs LAUNCH, not continuation: an
        already-started run resumes normally, and only ``start_run`` refuses.
        Three reasons this is not merely the permissive choice:

        * ``WorkflowRecoveryWorker`` does not come through here — it calls
          ``require_by_id`` and ``_resume_run`` directly, deliberately
          ungated. Refusing here therefore did not stop a deactivated
          workflow's runs from being resumed; it stopped only the path an
          OPERATOR drives, i.e. the one whose caller has the most context.
        * A refusal has no exit. A ``paused`` run is excluded from
          ``claim_stale_running`` and from ``list_purgeable`` (terminal rows
          only), so it would sit unresumable and unpurgeable forever while
          ``count_stale_paused`` reported it every day — a permanent stuck
          state produced by an administrative action.
        * Killing the run is already available, explicitly and auditably, as
          ``POST /cancel``. Deactivation does not need to do it implicitly.

        The status is logged rather than enforced, so an operator resuming
        into a deactivated workflow can see that they did.
        """
        workflow = await self._workflow_repo.require_by_id(run.workflow_id, tenant_id=tenant_id)
        if workflow.status != "active":
            _log.warning(
                "workflow_engine.resume_of_inactive_workflow",
                run_id=str(run.id),
                workflow_id=str(workflow.id),
                workflow_status=workflow.status,
            )
        templates = await _load_templates(self._template_repo, workflow.dag, tenant_id=tenant_id)
        return workflow, templates

    def _launch_resume(
        self, run: WorkflowRun, workflow: Workflow, templates: dict[str, AgentTemplate]
    ) -> None:
        """Schedule the background task that re-drives a resumed run (T3.1 §6).

        Synchronous on purpose: everything that can fail was already done by
        :meth:`_load_resume_context`, so nothing between the commit and the
        task creation can raise and strand the run.

        ``_resume_run``'s own ``status != "running"`` guard stays correct and
        untouched, because the caller above has ALREADY moved the row to
        ``running`` before this runs. That ordering is load-bearing — flip it
        and the resume silently no-ops.
        """
        task = asyncio.create_task(
            self._resume_run(run.id, workflow, templates),
            name=f"workflow-run-resume-{run.id}",
        )
        # Retained until completion — a task with no external reference can
        # be garbage-collected mid-`await` (mirror `start_run`).
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    @staticmethod
    def _alive_since() -> datetime:
        """Cutoff below which a ``running`` row is no longer evidence of a
        live driver, and so must not consume concurrency capacity.

        Derived from the recovery worker's own staleness threshold rather
        than from a knob of its own: past it, the sweep already treats the
        run as orphaned and will reclaim or abandon it, so the cap and the
        sweep agree on "alive" by construction instead of by coincidence.
        The window between the threshold and the next sweep tick is the only
        place they differ, and the cap is explicitly a mechanical ceiling
        rather than a linearizable quota (see
        ``count_running_in_session``).
        """
        return datetime.now(UTC) - timedelta(
            seconds=derive_stale_threshold_s(
                base_delay_s=settings.workflow_retry_base_delay_s,
                max_delay_s=settings.workflow_retry_max_delay_s,
                escalation_timeout_s=settings.routing_escalation_timeout_s,
                handoff_summary_timeout_s=settings.workflow_handoff_summary_timeout_s,
                # Story 5.0 — depuis la boucle d'outils, ce plafond plat REMPLACE
                # `NODE_TIMEOUT_S` dans la dérivation : un nœud n'est plus un appel
                # LLM mais N appels LLM et M appels d'outils, tous enfermés dedans.
                tool_loop_max_wall_clock_s=settings.tool_loop_max_wall_clock_s,
            )
        )

    async def _ensure_run_capacity(self, *, workflow_id: UUID, tenant_id: UUID | None) -> None:
        """Early, best-effort admission check (Story 4.9 AC2, T2.2/T2.3).

        Called BEFORE ``_gate_on_mise_en_place`` on both ``start_run`` and a
        ``resume`` — that gate is the expensive part of the request (up to
        ``_MAX_CONCURRENT_PROBES`` concurrent MCP pings, plus a Dry Run), so
        a request already over the concurrency cap should not pay for it
        just to be refused a moment later.

        Racy against a concurrent writer by construction (``READ
        COMMITTED``): the authoritative check is the one taken in the same
        transaction as the write, via
        :meth:`WorkflowRunRepo.count_running_in_session`. This one only
        turns the common case — capacity already visibly exhausted — into
        a cheap, early refusal.
        """
        running_count = await self._workflow_run_repo.count_running(
            workflow_id, tenant_id=tenant_id, alive_since=self._alive_since()
        )
        self._raise_if_over_capacity(running_count, workflow_id=workflow_id)

    @staticmethod
    def _raise_if_over_capacity(running_count: int, *, workflow_id: UUID) -> None:
        limit = settings.workflow_max_concurrent_running_runs
        if running_count >= limit:
            raise RateLimitError(
                detail=(
                    f"workflow '{workflow_id}' already has {running_count} run(s) "
                    f"running (limit {limit}) — refused until capacity frees up"
                ),
                context={
                    "workflow_id": str(workflow_id),
                    "running_count": running_count,
                    "max_concurrent_running_runs": limit,
                },
            )

    async def _gate_on_mise_en_place(
        self,
        *,
        workflow_id: UUID,
        templates: dict[str, AgentTemplate],
        task_input: dict[str, Any],
        tenant_id: UUID | None,
        correlation_id: UUID,
        force: bool,
        reason: str | None,
        # Story 4.12 AC4 — `None` from `start_run` (no run exists yet, the
        # scenario `WorkflowRunMiseEnPlaceRefusedEvent.run_id` was designed
        # `None` for), the resuming run's id from `resume` (Story 4.6 IG2),
        # so a refusal on THAT path names which paused run stayed stuck.
        run_id: UUID | None = None,
        # Story 4.12 AC5 — empty from `start_run` (nothing has executed
        # yet); the resuming run's already-completed node ids from
        # `resume`, so its `budget_available` check prices only the
        # remaining DAG instead of re-charging work already billed.
        already_executed_node_ids: frozenset[str] = frozenset(),
    ) -> tuple[MiseEnPlaceReport, bool]:
        """Run the pre-flight and refuse the caller unless it passes (AC2/AC3).

        Returns ``(report, bypassed)``. Raises before ANYTHING is persisted —
        the necessary condition for "the workflow does not start" to hold
        structurally rather than by convention.

        Shared by ``start_run`` and by ``resume`` (Story 4.6 review, `IG2`).
        Extracted rather than copied: the 422-vs-503 rule, the RFC 7807
        context shape and the audit-on-refusal discipline are subtle enough
        that two copies would answer differently within a release.
        """
        bypass_reason: str | None = None
        if force:
            candidate = (reason or "").strip()
            if not candidate:
                raise ValidationError(
                    detail="reason is required when force=true",
                    context={"workflow_id": str(workflow_id)},
                )
            # Length is validated HERE too, not only in `StartRunRequest`
            # (review P7). `WorkflowRunMiseEnPlaceBypassedEvent.reason` caps
            # at 2000; a direct caller of this method — the very case this
            # defensive re-validation exists for — could otherwise insert
            # the row, publish `started`, and only then have Pydantic raise
            # INSIDE the transaction: rollback plus an untyped 500 rather
            # than a clean 422.
            if len(candidate) > _MAX_BYPASS_REASON_LEN:
                raise ValidationError(
                    detail=(
                        f"reason must be at most {_MAX_BYPASS_REASON_LEN} characters "
                        f"(got {len(candidate)})"
                    ),
                    context={"workflow_id": str(workflow_id)},
                )
            bypass_reason = redact_secrets(candidate)

        report = await self._mise_en_place_service.run_checks(
            workflow_id=workflow_id,
            templates=templates,
            task_input=task_input,
            tenant_id=tenant_id,
            already_executed_node_ids=already_executed_node_ids,
            # A resume prices an ALREADY-LAUNCHED run, so "is this workflow
            # launchable?" is not its question — see `_load_resume_context`
            # on why deactivation governs launch, not continuation. `run_id`
            # is the discriminator the parameter above already documents:
            # `None` on `start_run`, the paused run's id on `resume`.
            allow_inactive_workflow=run_id is not None,
        )

        bypassed = False
        if not report.all_passed:
            if not force:
                failed_checks = [check.code for check in report.checks if not check.passed]
                # AC2 asks the error body for `{"detail": <résumé>, ...}` —
                # `app.main`'s RFC 7807 handler merges `context` at the
                # TOP level and silently DROPS any context key that
                # collides with a reserved field (`type`/`title`/`status`/
                # `correlation_id`/`detail`), logging a warning instead.
                # `context["detail"]` would therefore never reach the
                # response body. The readable summary goes on
                # `AgentiveError.detail` itself instead — same JSON key,
                # same content, but the one the handler actually renders.
                # `suggested_actions`, plural: it is a LIST, and naming a
                # list with the same singular key the per-check field uses
                # forced clients to handle two types under one name
                # (review P15). Omitted entirely when empty rather than
                # rendered as `[]`, which promised a remediation and
                # delivered none.
                suggested_actions = [
                    check.suggested_action
                    for check in report.checks
                    if not check.passed and check.suggested_action
                ]
                context: dict[str, Any] = {
                    "failed_checks": failed_checks,
                    "mise_en_place": _mise_en_place_out(report).model_dump(mode="json"),
                }
                if suggested_actions:
                    context["suggested_actions"] = suggested_actions
                # 503 ONLY when an identical retry could plausibly succeed
                # on its own — an MCP server that is down, a check that timed
                # out (review BS5). A missing API key, an absent namespace or
                # a budget overrun will still be there on the next attempt,
                # and 503 tells clients, proxies and gateways to retry: it
                # turned a permanently misconfigured workflow into an
                # infinite automatic retry loop. Those get 422, the same
                # code `status != "active"` already returns a few lines
                # above for the same reason — the request cannot succeed
                # until a human changes something.
                retryable = all(check.retryable for check in report.checks if not check.passed)
                error_cls = DependencyError if retryable else BusinessRuleError
                summary = "; ".join(
                    f"{check.code}: {check.detail}" for check in report.checks if not check.passed
                )
                await self._audit_mise_en_place_refusal(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    correlation_id=correlation_id,
                    failed_checks=failed_checks,
                    summary=summary,
                    retryable=retryable,
                    report=report,
                )
                raise error_cls(detail=summary, context=context)
            # `force=True`, so `bypass_reason` was validated non-blank above
            # (it is only ever left `None` when `force` is falsy). A real
            # raise, not an `assert`: `python -O` strips asserts, and this
            # one guards the audit trail's only non-empty-reason guarantee
            # (review P16).
            if bypass_reason is None:  # pragma: no cover — unreachable
                raise InternalError(detail="bypass reason missing while force=true")
            report = build_report(report.checks, bypassed=True, bypass_reason=bypass_reason)
            bypassed = True
        # else: every check passed. `force=True` here is a no-op (AC3's
        # "no-op silencieux") — `report` stays unbypassed and no audit event
        # is published, since nothing was actually bypassed.

        return report, bypassed

    async def _audit_mise_en_place_refusal(
        self,
        *,
        workflow_id: UUID,
        tenant_id: UUID | None,
        correlation_id: UUID,
        # `Sequence[str]`, not `list[str]`: `list` is invariant, and the
        # caller holds a `list[CheckCode]` (a list of `Literal`s).
        failed_checks: Sequence[str],
        summary: str,
        retryable: bool,
        report: MiseEnPlaceReport,
        run_id: UUID | None = None,
    ) -> None:
        """Trace a REFUSED launch in the outbox (AC2, review BS2).

        A refused launch creates no ``workflow_runs`` row — AC2 requires
        exactly that — so its report has nowhere to be persisted on that
        table and used to vanish the moment the 503/422 was returned. It is
        traced here instead, on the same bus that already carries
        ``mise_en_place_bypassed``: a refusal is as auditable a decision as
        a bypass (NFR8/NFR15).

        BEST-EFFORT on purpose. If this write fails, the refusal is still
        raised: the caller already receives the full report in the error
        body, and replacing a precise "your MCP server is down" with an
        opaque database error would trade a useful answer for a useless
        one. The failure is logged at ERROR so an operator can see the
        audit gap.
        """
        try:
            async with self._workflow_run_repo.with_tenant(tenant_id) as session:
                await publish_and_commit(
                    session,
                    WorkflowRunMiseEnPlaceRefusedEvent.event_type,
                    WorkflowRunMiseEnPlaceRefusedEvent(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        failed_checks=list(failed_checks),
                        detail=summary[:4000],
                        retryable=retryable,
                        mise_en_place=_mise_en_place_out(report).model_dump(mode="json"),
                        tenant_id=tenant_id,
                    ),
                    correlation_id=correlation_id,
                )
        except Exception:
            _log.exception(
                "workflow_engine.mise_en_place_refusal_audit_failed",
                workflow_id=str(workflow_id),
                failed_checks=list(failed_checks),
            )

    async def _drive_run(
        self,
        run_id: UUID,
        workflow: Workflow,
        templates: dict[str, AgentTemplate],
        run_input: dict[str, Any],
        *,
        correlation_id: UUID,
    ) -> None:
        """Fresh-run driver (T5.3) — starts the graph from ``START`` with
        ``run_input`` as ``task_input``."""
        await self._execute(
            run_id,
            workflow,
            templates,
            initial_input=run_input,
            correlation_id=correlation_id,
            started_at=datetime.now(UTC),
        )

    # `started_at` is deliberately the START OF THIS EXECUTION SEGMENT, in
    # both drivers, and never `workflow_runs.started_at`. AC4's
    # `total_duration_ms` is meant to be comparable with the sum of
    # `per_node.duration_ms`; measuring a resumed run from the original
    # `started_at` folded in the entire crash gap — potentially hours of
    # wall-clock during which nothing executed. The run's calendar lifetime
    # remains derivable from the row's own `started_at`/`ended_at`.

    async def _resume_run(
        self,
        run_id: UUID,
        workflow: Workflow,
        templates: dict[str, AgentTemplate],
    ) -> None:
        """Resume-driver (T5.5), called by :class:`.recovery.WorkflowRecoveryWorker`
        for an orphaned run — passes ``None`` as the graph input so LangGraph
        resumes from its last internal checkpoint instead of restarting at
        ``START`` (mirror spike ``resume_existing=True``)."""
        run = await self._workflow_run_repo.get_by_id(run_id)
        if run is None:
            # The run vanished between the recovery worker's scan and this
            # call (e.g. a concurrent process already resumed and completed
            # it) — nothing to do, not an error.
            _log.warning("workflow_run_resume_target_missing", run_id=str(run_id))
            return
        if run.status != "running":
            # Checking only for `None` was not enough: a run that another
            # writer finished between the claim and this call still EXISTS,
            # it is simply terminal. Re-driving it would restart a finished
            # graph and then try to stamp a second terminal status on it.
            _log.warning(
                "workflow_run_resume_target_not_running",
                run_id=str(run_id),
                status=run.status,
            )
            return
        stored = run.checkpoint if isinstance(run.checkpoint, dict) else {}
        fallback_input = stored.get("task_input")
        self._report_config_drift(run_id, stored, templates)
        # Story 4.3 T9.7 — the decisions this run already took before the
        # crash. `_sync_checkpoint` mirrors the `routing_decisions` state
        # channel into the applicative checkpoint on every node landing, so
        # this is the run-scoped record the idempotence guard needs.
        stored_decisions = stored.get("routing_decisions")
        await self._execute(
            run_id,
            workflow,
            templates,
            initial_input=None,
            correlation_id=run.correlation_id,
            started_at=datetime.now(UTC),
            fallback_input=fallback_input if isinstance(fallback_input, dict) else None,
            already_decided=(
                tuple(stored_decisions) if isinstance(stored_decisions, dict) else None
            ),
        )

    @staticmethod
    def _report_config_drift(
        run_id: UUID,
        stored_checkpoint: Mapping[str, Any],
        templates: Mapping[str, AgentTemplate],
    ) -> None:
        """Surface template config changes that happened across the crash gap.

        ``start_run`` snapshots the templates it resolved; a resume reloads
        them from the DB at resume time. Since `agent_templates` rows are
        mutated in place, a template edited in between makes the second half
        of the run execute under a configuration the first half never saw —
        a different model, temperature or system prompt — with nothing in the
        record to say so.

        Reported, not refused: failing the resume would strand a recoverable
        run over an edit that is very often intentional. But it must be
        visible, and it now is — in the logs and, via the caller's next
        checkpoint sync, in the run's own trace.
        """
        previous = stored_checkpoint.get(TEMPLATE_FINGERPRINTS_KEY)
        if not isinstance(previous, dict):
            return
        current = _template_fingerprints(templates)
        drifted = sorted(
            node_id
            for node_id, fingerprint in current.items()
            if node_id in previous and previous[node_id] != fingerprint
        )
        if drifted:
            _log.warning(
                "workflow_run_template_config_drift",
                run_id=str(run_id),
                drifted_nodes=drifted,
            )

    async def _execute(
        self,
        run_id: UUID,
        workflow: Workflow,
        templates: dict[str, AgentTemplate],
        *,
        initial_input: dict[str, Any] | None,
        correlation_id: UUID,
        started_at: datetime,
        fallback_input: dict[str, Any] | None = None,
        already_decided: Collection[str] | None = None,
    ) -> None:
        """Shared driver for :meth:`_drive_run` and :meth:`_resume_run`
        (T5.5) — the only difference between a fresh run and a resumed one is
        whether ``initial_input`` is a dict (start from ``START``) or
        ``None`` (LangGraph resumes from the last committed checkpoint).

        Never re-raises (T5.3) — an uncaught exception in a bare
        ``asyncio.Task`` only logs "Task exception was never retrieved"
        without crashing the process, so catching here both avoids that
        noise AND guarantees the ``failed`` transition always runs. That
        promise is why EVERY step below sits inside a handler: the previous
        shape left graph compilation before the ``try`` and the completion
        transition after it, so a failure in either escaped into the bare
        task and froze the row on ``running`` — with no ``error`` status and
        no ``failed`` event, but still visible to the recovery sweep, which
        would resume it only to fail again at the very same line.

        ``asyncio.CancelledError`` is the deliberate exception to that: it
        derives from ``BaseException``, so it passes straight through every
        ``except Exception`` here. That is what lets shutdown leave an
        interrupted run on ``running`` for the next process to claim, rather
        than burying it in a terminal ``error`` (cf ``cancel_inflight_runs``).
        """
        # Untyped as `dict[str, Any]` rather than langchain_core's
        # `RunnableConfig` — `.import-linter` Contract 5 forbids
        # `features.*` from importing `langchain_core` directly, even just
        # for a type hint. `RunnableConfig` is a `TypedDict`; a plain dict
        # shaped like it satisfies LangGraph structurally at runtime.
        #
        # Built before anything that can fail: `_mark_failed` needs it to
        # look the run's state up even when the failure IS the compilation.
        config: dict[str, Any] = {"configurable": {"thread_id": str(run_id)}}
        # Story 4.3 T5.1/T9.3 — built once per run, from `settings`, by this
        # assembly layer. Never read from inside the engine.
        routing_settings = RoutingSettings(
            threshold=settings.routing_confidence_threshold,
            escalation_model=settings.routing_escalation_model,
            escalation_timeout_s=settings.routing_escalation_timeout_s,
            escalation_max_tokens=settings.routing_escalation_max_tokens,
        )
        # Story 4.6 T11.3 — same posture as `routing_settings` above: built
        # here, in the assembly layer, from `settings`; `engine/` and
        # `domain/` never read configuration themselves (golden rule #6).
        retry_settings = RetrySettings(
            base_delay_s=settings.workflow_retry_base_delay_s,
            max_delay_s=settings.workflow_retry_max_delay_s,
        )
        # Story 4.7 T5.1 — same posture as `routing_settings`/`retry_settings`
        # above: built here, in the assembly layer, from `settings`; `engine/`
        # never reads configuration itself (golden rule #6).
        # Review of 2026-09-12 (I-04) — `None` is the deployment kill switch,
        # and it is the path `build_state_graph`/`execute_agent_node` already
        # default to: no new branch, the pre-4.7 behaviour byte for byte.
        handoff_settings = (
            HandoffSettings(
                model=settings.workflow_handoff_summary_model,
                max_tokens=settings.workflow_handoff_summary_max_tokens,
                timeout_s=settings.workflow_handoff_summary_timeout_s,
            )
            if settings.workflow_handoff_summary_enabled
            else None
        )
        # Story 4.3 T9.7 — node_ids whose routing decision has already been
        # accounted for: counted in Prometheus AND, when escalated, published
        # as an event. Seeded from the decisions already present in the
        # RESTORED checkpoint (`already_decided`), not empty, because the
        # guard has to hold "for this RUN" and not merely for this execution.
        # Relying on "LangGraph never re-yields a committed superstep" is an
        # argument about someone else's scheduler, not a guarantee we own —
        # and the cost of being wrong is a permanently skewed AC3 ratio plus
        # duplicate escalation events that over-represent resumed runs in the
        # raw material of innovation #1.
        accounted_decisions: set[str] = set(already_decided or ())

        try:
            dag = _dag_from_stored(workflow.dag)
            graph = build_state_graph(
                dag,
                templates,
                self._llm_router,
                rules=self._routing_rules,
                routing_settings=routing_settings,
                retry_settings=retry_settings,
                handoff_settings=handoff_settings,
            ).compile(checkpointer=self._checkpointer)
        except Exception as exc:
            # A stored DAG that no longer parses, or a `node_id` LangGraph
            # reserves (`__start__`/`__end__`, which Story 4.1 accepts at
            # creation), raises here — before `astream` is ever reached, so
            # there is no graph to interrogate for a failed node.
            await self._mark_failed_safely(
                run_id,
                workflow.id,
                None,
                config,
                exc,
                correlation_id=correlation_id,
                started_at=started_at,
            )
            return

        state_input: WorkflowState | None = (
            {
                "task_input": initial_input,
                "correlation_id": str(correlation_id),
                "node_outputs": {},
                "node_metrics": {},
            }
            if initial_input is not None
            else None
        )

        # Story 4.6 AC2 — set once the driver has settled a pause/cancel
        # request, so the completion block below is skipped. A local flag,
        # not an exception: stopping on request is a normal outcome, and
        # raising would route it through `_mark_failed`.
        settled: str | None = None

        try:
            if state_input is None:
                state_input = await self._restart_input_if_never_checkpointed(
                    graph, config, fallback_input, run_id=run_id, correlation_id=correlation_id
                )

            # Evaluated BEFORE the first superstep (T6.2): a run the recovery
            # worker picked up while a pause/cancel request had never been
            # observed (its previous process died in between) must settle
            # without executing a single node — otherwise the request costs
            # one more billed LLM call every time the sweep resumes it.
            # Read the state BEFORE settling. This path exists for runs the
            # recovery worker picked up — runs that have already executed and
            # BILLED nodes — so passing an empty mapping recorded the
            # cancellation of a run that cost real money as free: `per_node`
            # empty, `total_cost_usd` null, `cancelled_at_node_id` null. That
            # is exactly what the cancel branch aggregates metrics to avoid.
            pre_state = await self._state_values_safely(graph, config, run_id=run_id)
            settled = await self._observe_control(
                run_id,
                workflow.id,
                correlation_id=correlation_id,
                started_at=started_at,
                state_values=pre_state,
                last_node_id=_latest_node_id(pre_state),
            )
            if settled is not None:
                return

            # Held in a name so the `break` below can close it. Story 4.6
            # added the only exit that leaves this generator suspended
            # mid-`await`: the two pre-existing ones (the stream running out,
            # an exception) finalise it themselves.
            stream = graph.astream(  # type: ignore[call-overload]
                state_input,
                config,
                stream_mode="updates",
            )
            async for update in stream:
                # `stream_mode="updates"` also surfaces LangGraph's own
                # bookkeeping keys (`__interrupt__`, …). Filtering on
                # `templates` keeps a phantom `step_completed` event for a
                # node that does not exist from ever being published.
                node_ids = [node_id for node_id in update if node_id in templates]
                if not node_ids:
                    continue

                # ONE state read per superstep, not one per node. In a
                # fan-out, `update` carries every node that completed in
                # parallel: the previous per-node `aget_state` + UPDATE
                # issued N identical round-trips writing N times the same
                # snapshot, and left `last_node_id` holding whichever node
                # dict iteration happened to visit last.
                snapshot = await graph.aget_state(config)  # type: ignore[arg-type]
                await self._sync_checkpoint_safely(
                    run_id, snapshot.values, last_node_id=max(node_ids), templates=templates
                )
                for node_id in node_ids:
                    node_metrics_update = (update[node_id] or {}).get("node_metrics") or {}
                    duration_ms = int(
                        (node_metrics_update.get(node_id) or {}).get("duration_ms", 0)
                    )
                    await self._publish_step_completed_safely(
                        run_id, workflow.id, node_id, duration_ms, correlation_id=correlation_id
                    )

                    # Story 4.3 T9.6 — best-effort publication, OUT of the
                    # decision path itself: an event failure must never fail
                    # an otherwise healthy run.
                    #
                    # Prometheus counting deliberately does NOT happen here
                    # (review IG4). Counting per superstep counted only the
                    # decisions THIS execution observed, while
                    # `metrics["routing"]` is computed from the final state and
                    # includes those restored from before a crash — so every
                    # resumed run left the counters permanently below
                    # `/routing-stats`, with nothing saying the two were not
                    # comparable. Both now derive from the same final state,
                    # at run end, and agree by construction.
                    routing_update = (update[node_id] or {}).get("routing_decisions") or {}
                    decision = routing_update.get(node_id)
                    if isinstance(decision, dict) and node_id not in accounted_decisions:
                        accounted_decisions.add(node_id)
                        if decision.get("mode") == "llm_escalated":
                            await self._publish_routing_escalated_safely(
                                run_id,
                                workflow.id,
                                node_id,
                                decision,
                                dag=dag,
                                node_outputs=snapshot.values.get("node_outputs") or {},
                                correlation_id=correlation_id,
                            )

                # END OF SUPERSTEP — the one instant in a run where stopping
                # costs nothing (Story 4.6 AC2). LangGraph has committed its
                # own checkpoint for the superstep that just finished, and
                # `_sync_checkpoint_safely` above has written the applicative
                # summary; a `break` here leaves a run that resumes from
                # exactly this point without re-executing a single node.
                #
                # Deliberately AFTER the `step_completed` publications: the
                # work of this superstep happened and must be reported, even
                # though the run stops right after it.
                settled = await self._observe_control(
                    run_id,
                    workflow.id,
                    correlation_id=correlation_id,
                    started_at=started_at,
                    state_values=snapshot.values,
                    last_node_id=max(node_ids),
                )
                if settled is not None:
                    # Finalise the generator HERE rather than leaving it to
                    # the GC: it is suspended inside LangGraph's Pregel loop,
                    # holding a checkpointer context and its own background
                    # tasks. Deferred cleanup surfaces as a late checkpoint
                    # write against an already-terminal run, and as "Task was
                    # destroyed but it is pending" at shutdown. Best-effort —
                    # a failure to close must not undo a pause that the row
                    # has already recorded.
                    await self._aclose_stream_safely(stream, run_id=run_id)
                    break
        except Exception as exc:
            await self._mark_failed_safely(
                run_id,
                workflow.id,
                graph,
                config,
                exc,
                correlation_id=correlation_id,
                started_at=started_at,
            )
            return

        if settled is not None:
            # The run stopped ON REQUEST, at a superstep boundary — it did
            # not reach `END`. `_observe_control` already wrote the terminal
            # (or suspended) status and published its event; marking it
            # `completed` on top would overwrite a legitimate `paused`/
            # `cancelled` with a lie about a run that never finished.
            return

        try:
            snapshot = await graph.aget_state(config)  # type: ignore[arg-type]
            await self._mark_completed(
                run_id,
                workflow.id,
                snapshot.values,
                started_at=started_at,
                correlation_id=correlation_id,
                declared_node_ids=[node.node_id for node in dag.nodes],
            )
        except Exception as exc:
            # The final state read and the metric aggregation can fail on
            # their own (a corrupt `node_metrics` entry, a DB hiccup on the
            # status write). `_mark_completed`'s own status write is guarded
            # by `only_if_status="running"`, so if it DID land before the
            # failure, the `_mark_failed` below is a no-op on the row and
            # publishes nothing — a completed run is never rewritten as
            # failed.
            await self._mark_failed_safely(
                run_id,
                workflow.id,
                graph,
                config,
                exc,
                correlation_id=correlation_id,
                started_at=started_at,
            )

    async def _prior_duration_ms(self, run_id: UUID) -> int:
        """Executed time already persisted for this run, or ``0``.

        Called by EVERY writer of ``metrics.total_duration_ms`` — the settle
        path and both terminal paths — because ``started_at`` is the start of
        THIS execution segment in both drivers (deliberately, so a crash gap
        is not billed as execution time). Without it the field means "time
        since the run was last resumed", which is not what any reader wants
        and not what its own comment claims.

        Returns ``0`` when nothing was persisted before, so a run that never
        paused is unaffected — and so a crash-resume, where no segment total
        was ever written, keeps its Story 4.2 semantics exactly.

        Best-effort, like every other read on these paths: losing the earlier
        total costs accuracy on one metric, raising would cost the run its
        terminal transition.
        """
        try:
            run = await self._workflow_run_repo.get_by_id(run_id)
        except Exception:
            _log.warning("workflow_engine.prior_duration_read_failed", run_id=str(run_id))
            return 0
        metrics = getattr(run, "metrics", None)
        if not isinstance(metrics, dict):
            return 0
        prior = metrics.get("total_duration_ms")
        return prior if isinstance(prior, int) and not isinstance(prior, bool) and prior > 0 else 0

    @staticmethod
    async def _aclose_stream_safely(stream: Any, *, run_id: UUID) -> None:
        """``stream.aclose()``, swallowing everything — never raises.

        A test double or a future LangGraph version may not expose
        ``aclose``; and a generator that refuses to unwind must not convert a
        recorded pause into an ``error`` through ``_execute``'s handler.
        """
        aclose = getattr(stream, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:
            _log.warning("workflow_engine.stream_close_failed", run_id=str(run_id))

    @staticmethod
    def _partial_metrics(
        state_values: Mapping[str, Any], *, total_duration_ms: int, run_id: UUID
    ) -> dict[str, Any]:
        """``_aggregate_metrics`` over a partially-executed run — never raises.

        Mirrors `_mark_failed`, not `_mark_completed`: a run stopped on
        request has real partial spend, and every node that finished before
        the stop was billed. Dropping it would make an interruption look free.

        The guard matters because ``node_metrics`` is free-form graph state:
        ``Decimal(str(cost_usd))`` and ``int(input_tokens)`` both raise on a
        corrupt entry. Unguarded, that exception escaped into `_execute`'s
        handler and `_mark_failed_safely` rewrote the run as ``error`` — so a
        user's cancellation destroyed the run instead of stopping it. The
        duration is preserved even when the per-node breakdown cannot be.
        """
        try:
            return _aggregate_metrics(
                state_values.get("node_metrics") or {},
                total_duration_ms=total_duration_ms,
                routing_decisions=state_values.get("routing_decisions") or {},
                handoffs=state_values.get("handoffs") or {},
                handoff_substitutions=state_values.get("handoff_substitutions") or {},
            )
        except Exception:
            _log.warning("workflow_engine.partial_metrics_aggregation_failed", run_id=str(run_id))
            return _aggregate_metrics({}, total_duration_ms=total_duration_ms)

    async def _state_values_safely(
        self, graph: Any, config: dict[str, Any], *, run_id: UUID
    ) -> dict[str, Any]:
        """``graph.aget_state(config).values``, or ``{}`` — never raises.

        Best-effort for the same reason ``_sync_checkpoint_safely`` is: this
        read feeds observability (which nodes ran, what they cost), and a
        transient failure must not turn an otherwise healthy run into an
        ``error``. Degrading to ``{}`` costs the accounting of one
        cancellation; raising would cost the run.
        """
        try:
            snapshot = await graph.aget_state(config)
            values = snapshot.values
        except Exception:
            _log.warning("workflow_engine.state_read_failed", run_id=str(run_id))
            return {}
        # `dict`, not `Mapping`: this module imports `Mapping` under
        # `TYPE_CHECKING` only, so an isinstance against it passes mypy and
        # raises `NameError` at runtime — inside `_execute`'s handler, which
        # would rewrite a healthy run as `error`.
        return dict(values) if isinstance(values, dict) else {}

    async def _observe_control(
        self,
        run_id: UUID,
        workflow_id: UUID,
        *,
        correlation_id: UUID,
        started_at: datetime,
        state_values: Mapping[str, Any],
        last_node_id: str | None,
    ) -> str | None:
        """Settle a pending pause/cancel request, or return ``None`` (AC2).

        Returns the status the run was moved to (``"paused"`` /
        ``"cancelled"``), which the caller uses both to leave the ``astream``
        loop and to skip the completion block. ``None`` means "nothing to do,
        keep running".

        **Best-effort on the READ, strict on the WRITE.** A DB failure while
        reading ``control_signal`` must not kill an otherwise healthy run —
        the request will simply be observed at the next superstep, or by the
        recovery sweep (mirror ``_sync_checkpoint_safely``, correctif lot 1 of
        the 4.2 review). The compare-and-set, by contrast, is not softened: a
        0 rowcount means the transition did NOT happen — someone else already
        closed this run — so nothing is published and the run continues,
        rather than announcing a state the row contradicts.

        One ``SELECT control_signal`` per superstep, against one LLM call per
        superstep: the cost is noise. It must NOT be cached in-process —
        that cache is precisely what would break the multi-worker property
        the DB-borne signal buys for free.
        """
        try:
            signal = await self._workflow_run_repo.get_control_signal(run_id)
        except Exception:
            _log.warning("workflow_engine.control_signal_read_failed", run_id=str(run_id))
            return None
        if signal not in ("pause", "cancel"):
            # Includes `None` (the overwhelmingly common case) and any value
            # a future story or a manual edit may have put there: an
            # unrecognised signal is ignored rather than guessed at.
            if signal is not None:
                _log.warning(
                    "workflow_engine.control_signal_unknown",
                    run_id=str(run_id),
                    # Coerced and bounded, never the raw value: this comes
                    # from a free `VARCHAR(20)` column, and a structured-log
                    # renderer is not the place to discover what an
                    # unexpected object serializes to.
                    signal=str(signal)[:20],
                )
            return None

        # Both branches aggregate. The pause branch used to skip it, on the
        # reasoning that `_mark_completed` would do it when the run finished
        # — but a paused run may never finish: `cancel` on a `paused` run is
        # terminal on the spot, and it is served by `request_run_control`,
        # which has no graph and therefore no access to `node_metrics` at
        # all. The party holding the state is this one, so it is the one that
        # must write it; otherwise a run paused after eight billed nodes and
        # then cancelled reports `metrics = {}`.
        # `max(..., 0)` because the contract is `Field(ge=0)` and this value
        # is a wall-clock subtraction: an NTP step backwards makes it
        # negative, and the resulting `ValidationError` would be raised AFTER
        # the `cancelled` write had already committed — leaving a terminal row
        # with no event at all. `_control_event` already clamps; this path
        # did not, and the asymmetry was the tell.
        elapsed_ms = max(int((datetime.now(UTC) - started_at).total_seconds() * 1000), 0)
        # `started_at` is the start of THIS execution segment, so a run that
        # has already been paused once would report only its latest segment.
        # Carrying the previously persisted total forward is what makes the
        # number mean "time this run spent executing" rather than "time since
        # it was last resumed" — an API-driven pause/resume cycle otherwise
        # resets it at will (before this story it took a crash to see it).
        elapsed_ms += await self._prior_duration_ms(run_id)
        metrics = self._partial_metrics(state_values, total_duration_ms=elapsed_ms, run_id=run_id)

        # Review lot 9 (F2) — the settle write is guarded on the SIGNAL as
        # well as the status, on both branches. Two DB round-trips separate
        # the read of `signal` above from the write below, and `status` does
        # not move during that window, so `only_if_status` alone cannot see
        # a signal that changed underneath. A `cancel` escalating over this
        # very `pause` would be answered 202 and then erased by
        # `clear_control=True` here.
        if signal == "pause":
            # `ended_at` stays NULL: a paused run is suspended, not finished.
            rowcount = await self._workflow_run_repo.update_status(
                run_id,
                status="paused",
                metrics=metrics,
                only_if_status="running",
                only_if_control_signal="pause",
                clear_control=True,
            )
            if rowcount == 0:
                # NOT an error: losing here means the signal changed, and the
                # only legal change is an escalation to `cancel`. Returning
                # `None` sends the driver back into its loop, which observes
                # the cancel at the next boundary and settles it — one extra
                # superstep, and the operator gets the cancellation they were
                # promised.
                _log.warning("workflow_run_pause_transition_skipped", run_id=str(run_id))
                return None
            event_type: str = WorkflowRunPausedEvent.event_type
            event: BaseModel = WorkflowRunPausedEvent(
                run_id=run_id, workflow_id=workflow_id, paused_at_node_id=last_node_id
            )
            _log.info("workflow_run_paused", run_id=str(run_id), paused_at_node_id=last_node_id)
        else:
            ended_at = datetime.now(UTC)
            total_duration_ms = elapsed_ms
            rowcount = await self._workflow_run_repo.update_status(
                run_id,
                status="cancelled",
                ended_at=ended_at,
                metrics=metrics,
                only_if_status="running",
                only_if_control_signal="cancel",
                clear_control=True,
            )
            if rowcount == 0:
                _log.warning("workflow_run_cancel_transition_skipped", run_id=str(run_id))
                return None
            # `cancelled` is a TERMINAL transition, so it owes the same
            # Prometheus write as `completed` and `error`. Omitting it
            # reopened the drift the IG4 fix closed: `aggregate_routing_modes`
            # counts every run regardless of status, so `/routing-stats`
            # included cancelled runs while the counter did not, and the two
            # "agree by construction" only as long as every terminal path
            # records. Deliberately NOT done on the pause branch — a paused
            # run can still resume and reach `_mark_completed`, which would
            # then count its decisions twice.
            self._record_routing_metrics_safely(state_values.get("routing_decisions") or {})
            event_type = WorkflowRunCancelledEvent.event_type
            event = WorkflowRunCancelledEvent(
                run_id=run_id,
                workflow_id=workflow_id,
                cancelled_at_node_id=last_node_id,
                total_duration_ms=total_duration_ms,
            )
            _log.info(
                "workflow_run_cancelled",
                run_id=str(run_id),
                cancelled_at_node_id=last_node_id,
                total_duration_ms=total_duration_ms,
            )

        async with self._workflow_run_repo.with_tenant(None) as session:
            event_id = await publish(
                event_type, event, session=session, correlation_id=correlation_id
            )
        await notify_best_effort(event_id, event_type)
        return "paused" if signal == "pause" else "cancelled"

    async def _restart_input_if_never_checkpointed(
        self,
        graph: Any,
        config: dict[str, Any],
        fallback_input: dict[str, Any] | None,
        *,
        run_id: UUID,
        correlation_id: UUID,
    ) -> WorkflowState | None:
        """Return a ``START`` state when the thread has NO checkpoint to
        resume from, else ``None`` (a genuine LangGraph resume).

        AC3 names this case explicitly — "process précédent crashé avant tout
        checkpoint" — and it was the one case resume could NOT handle:
        ``astream(None, ...)`` on a thread LangGraph has never seen raises
        ``EmptyInputError("Received no input for __start__")`` (verified
        against langgraph 1.1.8). That exception is indistinguishable from a
        real node failure, so the recovery worker resumed the run, watched it
        die instantly, and buried it in the terminal ``error`` status — the
        exact opposite of recovery. The window is real: ``start_run`` commits
        the row before the fire-and-forget task is necessarily scheduled.

        The original ``task_input`` is stamped on the run row at creation
        precisely so this restart is possible. ``_sync_checkpoint`` overwrites
        it once the first node lands, by which point LangGraph owns the state
        and this path is unreachable.
        """
        snapshot = await graph.aget_state(config)
        if snapshot.values or snapshot.next:
            return None
        if fallback_input is None:
            raise InternalError(
                detail=(
                    "cannot resume run: no LangGraph checkpoint exists for this thread "
                    "and no task_input was preserved on the run row"
                ),
                context={"run_id": str(run_id)},
            )
        _log.info("workflow_run_resume_restarts_from_start", run_id=str(run_id))
        return {
            "task_input": fallback_input,
            "correlation_id": str(correlation_id),
            "node_outputs": {},
            "node_metrics": {},
        }

    async def _sync_checkpoint_safely(
        self,
        run_id: UUID,
        state_values: Mapping[str, Any],
        *,
        last_node_id: str,
        templates: Mapping[str, AgentTemplate],
    ) -> None:
        """BEST-EFFORT applicative checkpoint — never aborts a healthy run.

        This write is observability, not execution: LangGraph's own
        checkpoint (written natively by ``AsyncPostgresSaver``, already
        committed by the time we get here) is the technical source of truth.
        Yet a single transient DB error used to propagate out of the
        streaming loop and mark a perfectly healthy run ``error``, discarding
        the work of every node that had already succeeded.

        The trade-off is explicit: if this keeps failing,
        ``last_checkpoint_at`` stops advancing and the recovery worker will
        eventually claim the run as stale. That is the correct fallback for a
        DB that is genuinely unavailable, and it is bounded by
        ``MAX_RECOVERY_ATTEMPTS``.
        """
        try:
            await self._sync_checkpoint(
                run_id, state_values, last_node_id=last_node_id, templates=templates
            )
        except Exception:
            _log.warning(
                "workflow_run_checkpoint_sync_failed", run_id=str(run_id), node_id=last_node_id
            )

    async def _publish_step_completed_safely(
        self,
        run_id: UUID,
        workflow_id: UUID,
        node_id: str,
        duration_ms: int,
        *,
        correlation_id: UUID,
    ) -> None:
        """BEST-EFFORT ``step_completed`` — same rationale as
        :meth:`_sync_checkpoint_safely`. This class's own docstring already
        argues that a dropped lifecycle event only DELAYS API-observable
        staleness (the SSE endpoint re-polls the row), so it must never be
        able to fail an otherwise healthy run."""
        try:
            await self._publish_step_completed(
                run_id, workflow_id, node_id, duration_ms, correlation_id=correlation_id
            )
        except Exception:
            _log.warning(
                "workflow_run_step_event_publish_failed", run_id=str(run_id), node_id=node_id
            )

    @staticmethod
    def _record_routing_metrics_safely(routing_decisions: Mapping[str, Any]) -> None:
        """Best-effort Prometheus instrumentation (Story 4.3 T8.4), recorded
        ONCE per run over its FINAL state.

        Called from the terminal transitions — completed AND error, mirroring
        ``_aggregate_metrics`` — and deliberately not per superstep (review
        IG4). Per-superstep counting only ever saw the decisions of the
        CURRENT execution, while ``metrics["routing"]`` is computed from the
        final state and includes the decisions restored from before a crash;
        every resumed run therefore left Prometheus permanently below
        ``/routing-stats`` with nothing documenting the gap. Sharing one
        source makes them agree by construction, and a run resumed three
        times still counts its decisions once.

        Trade-off accepted: counters move at run END, not live. For a ratio
        whose authoritative reading is the SQL endpoint anyway, that is the
        right side of the trade; a run that crashes and is never resumed
        contributes to neither, which is consistent rather than skewed.

        A metrics failure must never affect an otherwise healthy run, hence
        the blanket guard — the only place in this path that carries one.
        """
        try:
            for decision in routing_decisions.values():
                if not isinstance(decision, dict):
                    continue
                mode = decision.get("mode")
                source = decision.get("source")
                if isinstance(mode, str) and isinstance(source, str):
                    ROUTING_DECISIONS_TOTAL.labels(mode=mode, source=source).inc()
                if mode == "llm_escalated":
                    latency_ms = decision.get("llm_latency_ms")
                    if isinstance(latency_ms, int):
                        ROUTING_ESCALATION_SECONDS.observe(latency_ms / 1000.0)
        except Exception:
            # A context-free warning is unactionable: whoever reads it needs
            # to know WHAT failed to record and WHY, not merely that
            # something did.
            _log.warning(
                "workflow_engine.routing_metrics_record_failed",
                decision_count=len(routing_decisions),
                exc_info=True,
            )

    async def _publish_routing_escalated_safely(
        self,
        run_id: UUID,
        workflow_id: UUID,
        node_id: str,
        decision: Mapping[str, Any],
        *,
        dag: WorkflowDag,
        node_outputs: Mapping[str, Any],
        correlation_id: UUID,
    ) -> None:
        """BEST-EFFORT ``routing_escalated`` — same rationale as
        :meth:`_publish_step_completed_safely` (Story 4.3 T9.6)."""
        try:
            await self._publish_routing_escalated(
                run_id,
                workflow_id,
                node_id,
                decision,
                dag=dag,
                node_outputs=node_outputs,
                correlation_id=correlation_id,
            )
        except Exception:
            _log.warning(
                "workflow_run_routing_escalated_publish_failed",
                run_id=str(run_id),
                node_id=node_id,
            )

    async def _publish_routing_escalated(
        self,
        run_id: UUID,
        workflow_id: UUID,
        node_id: str,
        decision: Mapping[str, Any],
        *,
        dag: WorkflowDag,
        node_outputs: Mapping[str, Any],
        correlation_id: UUID,
    ) -> None:
        """Publish ``workflow_engine.workflow_run.routing_escalated`` (Story
        4.3 AC2/AC3 T7, T9.6) from the SERVICE, not the engine — the engine
        (``hybrid_router.decide_route``) runs inside a LangGraph node and
        must not open a DB session or know about the event bus (Contract 4,
        D91 point 7). The decision is already sitting in state; this only
        reads it back and reconstructs the DAG-shape facts the event needs
        (``candidates``/``context``) — deliberately WITHOUT the node's own
        output (T7.3, payload-size discipline for the bounded SSE queue)."""
        declared_edges = [edge for edge in dag.edges if edge.from_node_id == node_id]
        # Deduplicated like `decide_route`'s own `declared_candidates`: two
        # edges `a → b` under different conditions are one candidate. The
        # event's context is the rule-learning material (AC3), so it has to
        # describe what the decision actually saw.
        candidates = list(dict.fromkeys(edge.to_node_id for edge in declared_edges))
        conditional_count = sum(1 for edge in declared_edges if edge.condition is not None)
        own_output = node_outputs.get(node_id)
        context_payload = {
            "candidate_count": len(candidates),
            "conditional_count": conditional_count,
            "unconditional_count": len(declared_edges) - conditional_count,
            # Mirrors `hybrid_router`'s own definition, including the
            # `{"_raw": ...}` envelope check — the event must not report
            # "output present" for a node whose output could not be parsed
            # (code review BS1). Reconstructed rather than read from the
            # decision because T7.3 keeps `own_output` out of the payload.
            "has_parsable_output": own_output is not None
            and not is_raw_fallback_output(own_output),
            "output_field_count": len(own_output) if isinstance(own_output, dict) else 0,
        }

        event_type = WorkflowRunRoutingEscalatedEvent.event_type
        async with self._workflow_run_repo.with_tenant(None) as session:
            event = WorkflowRunRoutingEscalatedEvent(
                run_id=run_id,
                workflow_id=workflow_id,
                node_id=node_id,
                candidates=candidates,
                decision_target=list(decision.get("targets") or []),
                confidence_best=decision.get("confidence"),
                rule_id_best=decision.get("rule_id"),
                reason=str(decision.get("reason") or ""),
                llm_model=str(decision.get("llm_model") or "unknown"),
                llm_latency_ms=int(decision.get("llm_latency_ms") or 0),
                context=context_payload,
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=correlation_id
            )
        await notify_best_effort(event_id, event_type)

    async def _mark_failed_safely(
        self,
        run_id: UUID,
        workflow_id: UUID,
        graph: Any | None,
        config: dict[str, Any],
        exc: Exception,
        *,
        correlation_id: UUID,
        started_at: datetime,
    ) -> None:
        """:meth:`_execute` promises never to re-raise, and the failure path
        itself touches the DB and the outbox — exactly the resources whose
        unavailability tends to have caused the failure being reported. Log
        and give up rather than resurrect an exception inside a bare
        ``asyncio.Task``."""
        try:
            await self._mark_failed(
                run_id,
                workflow_id,
                graph,
                config,
                exc,
                correlation_id=correlation_id,
                started_at=started_at,
            )
        except Exception:
            _log.exception("workflow_run_mark_failed_failed", run_id=str(run_id))

    async def _sync_checkpoint(
        self,
        run_id: UUID,
        state_values: Mapping[str, Any],
        *,
        last_node_id: str,
        templates: Mapping[str, AgentTemplate],
    ) -> None:
        """Sync the applicative summary (AC2) after each node completes —
        NOT the LangGraph technical checkpoint, written natively by
        ``AsyncPostgresSaver`` and never touched through this repo."""
        node_outputs = state_values.get("node_outputs") or {}
        checkpoint = {
            "last_node_id": last_node_id,
            "node_statuses": dict.fromkeys(node_outputs, "success"),
            "node_outputs_preview": {nid: _preview(output) for nid, output in node_outputs.items()},
            # Story 4.3 T9.5 — per-node routing decision detail, read by the
            # Trace Explorer (Epic 8, NFR15). Already JSON-serializable
            # (`RoutingDecision.to_mapping()` at the point it entered state).
            "routing_decisions": dict(state_values.get("routing_decisions") or {}),
            # Story 4.7 T5.2 — mirror `routing_decisions` exactly: the
            # applicative reflection of the `handoffs` state channel, read by
            # an operator inspecting `workflow_runs.checkpoint` directly (and
            # a future Trace Explorer, Epic 8) without needing to reconstruct
            # LangGraph's own checkpoint.
            "handoffs": dict(state_values.get("handoffs") or {}),
            # B-01 — mirrored beside `handoffs` for the same reason: an
            # operator reading this JSONB must be able to tell what a summary
            # COST from what substituting it actually SAVED.
            "handoff_substitutions": dict(state_values.get("handoff_substitutions") or {}),
            # Rewritten every sync rather than preserved from creation: the
            # window that matters for drift is between the LAST executed node
            # and the resume, not between creation and the resume.
            TEMPLATE_FINGERPRINTS_KEY: _template_fingerprints(templates),
        }
        await self._workflow_run_repo.update_checkpoint(
            run_id, checkpoint=checkpoint, last_checkpoint_at=datetime.now(UTC)
        )

    async def _publish_step_completed(
        self,
        run_id: UUID,
        workflow_id: UUID,
        node_id: str,
        duration_ms: int,
        *,
        correlation_id: UUID,
    ) -> None:
        event_type = WorkflowRunStepCompletedEvent.event_type
        async with self._workflow_run_repo.with_tenant(None) as session:
            event = WorkflowRunStepCompletedEvent(
                run_id=run_id, workflow_id=workflow_id, node_id=node_id, duration_ms=duration_ms
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=correlation_id
            )
        await notify_best_effort(event_id, event_type)

    async def _mark_completed(
        self,
        run_id: UUID,
        workflow_id: UUID,
        state_values: Mapping[str, Any],
        *,
        started_at: datetime,
        correlation_id: UUID,
        declared_node_ids: Sequence[str] = (),
    ) -> None:
        ended_at = datetime.now(UTC)
        # Segment + everything already billed. Review of 2026-09-12: only the
        # pause/cancel path carried the prior total forward, so a run paused
        # after ten minutes and completed two seconds later persisted
        # `total_duration_ms = 2000` — a number that goes DOWN, and that
        # contradicts the `per_node` map written beside it in the same JSONB
        # (LangGraph's `node_metrics` channel accumulates across the pause, so
        # the parts summed to ~600 s while the whole said 2 s). Comparability
        # between the two is the stated point of the field.
        total_duration_ms = max(int((ended_at - started_at).total_seconds() * 1000), 0)
        total_duration_ms += await self._prior_duration_ms(run_id)
        node_metrics = state_values.get("node_metrics") or {}
        routing_decisions = state_values.get("routing_decisions") or {}
        handoffs = state_values.get("handoffs") or {}
        handoff_substitutions = state_values.get("handoff_substitutions") or {}
        metrics = _aggregate_metrics(
            node_metrics,
            total_duration_ms=total_duration_ms,
            routing_decisions=routing_decisions,
            handoffs=handoffs,
            handoff_substitutions=handoff_substitutions,
        )
        # IG4 — same source as `metrics["routing"]`, so the counters and the
        # persisted ratio cannot drift apart on a resumed run.
        self._record_routing_metrics_safely(routing_decisions)
        total_cost_raw = metrics["total_cost_usd"]

        # A run that reaches `END` without executing every declared node is a
        # TRUNCATED run, and it used to be indistinguishable from a nominal
        # one: same `completed` status, same shape of response, nothing said
        # a branch had been skipped. The usual cause is `condition_dsl`'s
        # deliberate degradation — a node whose output does not carry the
        # routing variable makes EVERY outgoing condition false, so
        # `_make_router` falls back to `[END]` and the entire downstream is
        # silently dropped.
        #
        # Detected here rather than in the router because completion is where
        # the full picture exists, and because this catches every cause of a
        # skipped node, not only unmatched conditions. Still `completed` —
        # the reachable graph genuinely finished — but no longer silent.
        executed = set(state_values.get("node_outputs") or {})
        skipped = sorted(node_id for node_id in declared_node_ids if node_id not in executed)
        if skipped:
            _log.warning(
                "workflow_run_completed_with_skipped_nodes",
                run_id=str(run_id),
                skipped_nodes=skipped,
                executed_count=len(executed),
            )

        # Compare-and-set on `running`: a 0 rowcount means someone else
        # already gave this run a terminal status (a duplicate resume, or a
        # `_mark_failed` that raced us). Publishing `completed` for a
        # transition that did not happen would tell every SSE client and
        # every downstream consumer the opposite of what the row says.
        rowcount = await self._workflow_run_repo.update_status(
            run_id,
            status="completed",
            ended_at=ended_at,
            metrics=metrics,
            only_if_status="running",
            # A control request written while the last superstep was still
            # running is never observed — the run reaches `END` first. Both
            # writes are compare-and-set on `running`, so they interleave
            # without conflicting, and nothing else clears the column: the
            # row ended up `completed` while still carrying `"pause"`, which
            # the SSE `state` frame then reported forever.
            #
            # Story 4.9 AC6/T6.2 — decided a documented NO-OP rather than a
            # new `control_request_dropped` event: the operator who asked is
            # never told their request was silently dropped by this path
            # alone, but a `control_signal`/`control_requested_at` pair the
            # client was already watching on the SSE stream (T6.1) simply
            # disappears the moment the frame flips to `completed` — an
            # attentive client can infer the drop from that, without this
            # feature growing a second, rarely-exercised event type whose
            # own delivery could be lost the same way the original was.
            clear_control=True,
        )
        if rowcount == 0:
            _log.warning("workflow_run_completed_transition_skipped", run_id=str(run_id))
            return

        if skipped:
            # Surfaced through the SSE `state` frame and the Trace Explorer,
            # which both read `node_statuses` — a skipped node now says so
            # instead of simply being absent.
            node_outputs = state_values.get("node_outputs") or {}
            await self._workflow_run_repo.update_checkpoint(
                run_id,
                checkpoint={
                    "last_node_id": None,
                    "node_statuses": {
                        **dict.fromkeys(node_outputs, "success"),
                        **dict.fromkeys(skipped, "skipped"),
                    },
                    "node_outputs_preview": {
                        nid: _preview(output) for nid, output in node_outputs.items()
                    },
                    "skipped_nodes": skipped,
                    "routing_decisions": dict(state_values.get("routing_decisions") or {}),
                    "handoffs": dict(handoffs),
                    "handoff_substitutions": dict(handoff_substitutions),
                },
                last_checkpoint_at=ended_at,
            )

        event_type = WorkflowRunCompletedEvent.event_type
        async with self._workflow_run_repo.with_tenant(None) as session:
            event = WorkflowRunCompletedEvent(
                run_id=run_id,
                workflow_id=workflow_id,
                total_duration_ms=total_duration_ms,
                total_cost_usd=Decimal(total_cost_raw) if total_cost_raw is not None else None,
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=correlation_id
            )
        await notify_best_effort(event_id, event_type)

        _log.info(
            "workflow_run_completed",
            run_id=str(run_id),
            total_duration_ms=total_duration_ms,
            node_count=len(node_metrics),
        )

    async def _mark_failed(
        self,
        run_id: UUID,
        workflow_id: UUID,
        graph: Any | None,
        config: dict[str, Any],
        exc: Exception,
        *,
        correlation_id: UUID,
        started_at: datetime,
    ) -> None:
        ended_at = datetime.now(UTC)
        state_values: Mapping[str, Any] = {}
        failed_node_id: str | None = None
        # `graph is None` when the failure happened before compilation ever
        # produced one — there is simply no state to interrogate.
        if graph is not None:
            try:
                # `graph: Any | None` already disables checking on this call.
                snapshot = await graph.aget_state(config)
                state_values = snapshot.values
                # Best-effort (T6.3/T6.5): a node that raises never commits a
                # checkpoint for itself, so it never appears in `node_outputs` —
                # `snapshot.next` (nodes LangGraph was ABOUT to run) is the only
                # available signal for which node actually failed.
                failed_node_id = snapshot.next[0] if snapshot.next else None
            except Exception:
                _log.warning("workflow_run_failed_state_lookup_failed", run_id=str(run_id))

        node_outputs = dict(state_values.get("node_outputs") or {})
        node_metrics = dict(state_values.get("node_metrics") or {})

        # IG3 — a node whose ROUTING decision failed did execute: its LLM
        # call is paid and its output exists, but LangGraph discards the
        # update of a node that raises, so none of it is in `state_values`.
        # `RoutingDecisionFailedError` carries that update precisely so the
        # spend does not vanish — without this, a run ended `error` with no
        # trace of a node that ran, and its cost missing from the totals.
        # The run still fails: this only stops it failing *silently about
        # what it spent*.
        if isinstance(exc, RoutingDecisionFailedError):
            failed_node_id = exc.node_id
            node_outputs.update(exc.node_update.get("node_outputs") or {})
            node_metrics.update(exc.node_update.get("node_metrics") or {})
            # The wrapper says nothing an operator can act on; the cause does.
            exc = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc

        # AC4 — partial metrics from already-completed nodes stay aggregated,
        # and the run's REAL wall-clock duration is reported. The previous
        # hardcoded 0 made every failed run look instantaneous, so the AC4
        # metric could not be used to tell a fast failure from a run that
        # burned nine minutes before dying.
        total_duration_ms = max(int((ended_at - started_at).total_seconds() * 1000), 0)
        # Same accumulation as `_mark_completed` — a run that failed after a
        # pause owes the same honest total as one that succeeded.
        total_duration_ms += await self._prior_duration_ms(run_id)
        routing_decisions = state_values.get("routing_decisions") or {}
        handoffs = state_values.get("handoffs") or {}
        metrics = _aggregate_metrics(
            node_metrics,
            total_duration_ms=total_duration_ms,
            routing_decisions=routing_decisions,
            handoffs=handoffs,
            handoff_substitutions=state_values.get("handoff_substitutions") or {},
        )
        # IG4 — recorded on the error path too, exactly like `_aggregate_metrics`
        # (AC4 of Story 4.2: partial metrics are kept, never dropped).
        self._record_routing_metrics_safely(routing_decisions)
        # NFR9 — `str(exc)` on a psycopg/httpx error routinely carries the
        # full DSN (password included) or a URL with an API key, and this
        # string is BOTH persisted in `checkpoint.last_error` and streamed
        # verbatim to SSE clients via `WorkflowRunFailedEvent.error_summary`.
        # Redact before either, not at the read sites.
        error_summary = redact_secrets(str(exc))[:500]

        # The node that raised never commits a checkpoint, so it is absent
        # from `node_outputs` — `dict.fromkeys(node_outputs, "success")` alone
        # therefore described a FAILED run as one where every known node
        # succeeded, and the culprit appeared nowhere at all.
        node_statuses = dict.fromkeys(node_outputs, "success")
        if failed_node_id is not None:
            node_statuses[failed_node_id] = "error"

        checkpoint: dict[str, Any] = {
            "last_node_id": failed_node_id,
            "node_statuses": node_statuses,
            "node_outputs_preview": {nid: _preview(output) for nid, output in node_outputs.items()},
            "last_error": error_summary,
        }
        # Story 4.6 AC3 — when the whole provider chain failed, the router
        # packs a per-attempt breakdown into `exc.context["attempts"]`
        # (provider, model attempted, error type, redacted detail). It was
        # ENTIRELY lost: `error_summary` is a single line reading "all N
        # providers failed", which tells an operator nothing about WHICH
        # provider failed HOW. Persisted here, bounded and re-redacted.
        attempts = _failure_attempts(exc)
        if attempts:
            checkpoint["last_error_attempts"] = attempts

        # How many times the node re-walked the whole chain before giving up
        # (Story 4.6 AC3). `attempts` above describes ONE traversal — the
        # router builds a fresh error each call — so without this a node that
        # burned 8 billed provider calls is indistinguishable from one that
        # burned 2, and the failing node commits no `node_metrics` to say so.
        traversals = _failure_chain_traversals(exc)
        if traversals is not None:
            checkpoint["last_error_chain_traversals"] = traversals

        # Compare-and-set — see `_mark_completed`. A run another writer has
        # already finished must not be rewritten as failed, and must not
        # emit a `failed` event contradicting the row.
        rowcount = await self._workflow_run_repo.update_status(
            run_id,
            status="error",
            ended_at=ended_at,
            metrics=metrics,
            only_if_status="running",
            # Same race as `_mark_completed`: a node can raise while a
            # control request sits unobserved, leaving an `error` row that
            # advertises a pending cancellation for good. Same Story 4.9
            # AC6/T6.2 decision applies here: documented no-op, not a new
            # event — see `_mark_completed`'s comment for the rationale.
            clear_control=True,
        )
        if rowcount == 0:
            _log.warning(
                "workflow_run_failed_transition_skipped",
                run_id=str(run_id),
                error_type=type(exc).__name__,
            )
            return
        await self._workflow_run_repo.update_checkpoint(
            run_id, checkpoint=checkpoint, last_checkpoint_at=ended_at
        )

        event_type = WorkflowRunFailedEvent.event_type
        async with self._workflow_run_repo.with_tenant(None) as session:
            event = WorkflowRunFailedEvent(
                run_id=run_id,
                workflow_id=workflow_id,
                failed_node_id=failed_node_id,
                error_summary=error_summary,
                # Story 4.6 AC3 — the failure's CLASS, so an alerting
                # consumer can filter on `LLMAllProvidersFailedError`
                # without parsing `error_summary`'s redacted prose.
                error_type=type(exc).__name__[:200],
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=correlation_id
            )
        await notify_best_effort(event_id, event_type)

        _log.error(
            "workflow_run_failed",
            run_id=str(run_id),
            error_type=type(exc).__name__,
            failed_node_id=failed_node_id,
        )


__all__ = ["WorkflowExecutionService", "WorkflowService", "cancel_inflight_runs"]
