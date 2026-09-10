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
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from uuid import UUID

from agentive_backend.features.workflow_engine.domain import (
    DomainValidationError,
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
    WorkflowState,
    detect_cycle,
    find_dangling_edges,
    find_duplicate_node_ids,
)
from agentive_backend.features.workflow_engine.domain import parse as parse_condition
from agentive_backend.features.workflow_engine.engine import build_state_graph
from agentive_backend.features.workflow_engine.schemas import (
    CreateWorkflowResponse,
    DiversityWarning,
    StartRunResponse,
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)
from agentive_backend.shared.contracts.diversity import LLMSelection, check_llm_diversity
from agentive_backend.shared.contracts.events import (
    WorkflowCreatedEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
    WorkflowRunStartedEvent,
    WorkflowRunStepCompletedEvent,
)
from agentive_backend.shared.correlation import require_correlation_id
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import InternalError, ValidationError
from agentive_backend.shared.llm.redaction import redact_secrets
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo, WorkflowRunRepo

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agentive_backend.infra.db.models import AgentTemplate, Workflow
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
        never surfaces as a 404) :

        1. Duplicate ``node_id``.
        2. Dangling edges (``from_node_id``/``to_node_id`` not declared).
        3. Every ``agent_template_id`` must resolve to an existing template.
        4. No cycle (``graphlib.TopologicalSorter``).
        5. Every edge ``condition`` is syntactically valid AND its variable
           is exposed by the emitting node's ``output_contract.core``.
        6. Non-blocking Contrôleur/Producteur LLM-diversity warning for every
           edge incident (entering) a ``controleur`` node (AC4, D84) —
           NEVER raises, only appends to ``warnings``.
        7. Persist atomically (row INSERT + outbox event, single transaction).

        Raises:
            ValidationError: any of steps 1-5 fails (422 RFC 7807).
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

        # 3. Every agent_template_id must exist — 422, not 404 (Dev Notes).
        templates: dict[str, AgentTemplate] = {}
        for node in dag.nodes:
            template = await self._template_repo.get_by_id(
                node.agent_template_id, tenant_id=tenant_id
            )
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

        # 4. Cycle detection.
        cycle = detect_cycle(dag)
        if cycle is not None:
            raise ValidationError(
                detail=f"Workflow DAG contains a cycle: {' -> '.join(cycle)}",
                context={"cycle_node_ids": cycle},
            )

        # 5. Branching-condition DSL syntax + AC3 variable-exposure check.
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

        # 6. Controller/producer LLM diversity — non-blocking (AC4, D84).
        # Only edges INCOMING to a `controleur` node are considered
        # (producer_node_id -> controller_node_id) — never outgoing edges.
        # A duplicated (from_node_id, to_node_id) pair is checked once —
        # the DAG doesn't forbid duplicate edges, but the same producer ->
        # controller pair shouldn't surface the same warning twice.
        warnings = _diversity_warnings(dag.edges, templates)

        # 7-9. Persist atomically (row INSERT + outbox event) + post-commit notify.
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

        async with self._workflow_repo.with_tenant(tenant_id) as session:
            workflow = await self._workflow_repo.create_in_session(
                session,
                name=name,
                dag=dag_payload,
                version=1,
                tenant_id=tenant_id,
            )
            event = WorkflowCreatedEvent(
                workflow_id=workflow.id,
                name=name,
                version=workflow.version,
                node_count=len(dag.nodes),
                actor="system",
                tenant_id=tenant_id,
            )
            event_id = await publish(event_type, event, session=session)
            # commit happens at __aexit__ if no exception is raised.

        # ─── Post-commit: best-effort NOTIFY ───
        await notify_best_effort(event_id, event_type)

        _log.info(
            "workflow_created",
            workflow_id=str(workflow.id),
            name=name,
            version=workflow.version,
            node_count=len(dag.nodes),
            warning_count=len(warnings),
        )

        return CreateWorkflowResponse(
            workflow_id=workflow.id,
            version=workflow.version,
            warnings=warnings,
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


def _aggregate_metrics(
    node_metrics: Mapping[str, Any], *, total_duration_ms: int
) -> dict[str, Any]:
    """Aggregate per-node metrics into the ``workflow_runs.metrics`` JSONB
    shape (AC4). Called on BOTH the completed path (full ``node_metrics``)
    and the failed path (only the already-executed nodes' metrics) — AC4
    requires partial metrics to remain aggregated on failure, not dropped."""
    total_input = 0
    total_output = 0
    total_cost = Decimal("0")
    any_cost = False
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
        per_node[node_id] = {
            "duration_ms": metric.get("duration_ms"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_raw,
            "model_used": metric.get("model_used"),
        }
    return {
        "total_duration_ms": total_duration_ms,
        "total_tokens": {"input": total_input, "output": total_output},
        "total_cost_usd": str(total_cost) if any_cost else None,
        "per_node": per_node,
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
    ) -> None:
        self._workflow_repo = workflow_repo
        self._workflow_run_repo = workflow_run_repo
        self._template_repo = template_repo
        self._llm_router = llm_router
        self._checkpointer = checkpointer

    async def start_run(
        self,
        *,
        workflow_id: UUID,
        run_input: dict[str, Any],
        tenant_id: UUID | None = None,
    ) -> StartRunResponse:
        """Create a ``running`` ``workflow_runs`` row and spawn its execution
        as a background task (AC1). Returns immediately — the HTTP request
        never blocks on the run's progress.

        Raises:
            NotFoundError: ``workflow_id`` does not reference an existing
                workflow (404 — the URL's primary resource, cf Dev Notes
                § "404 vs 422").
            ValidationError: the workflow exists but ``status != "active"``
                (422).
        """
        workflow = await self._workflow_repo.require_by_id(workflow_id, tenant_id=tenant_id)
        if workflow.status != "active":
            raise ValidationError(
                detail=f"Workflow '{workflow_id}' is not active (status={workflow.status!r})",
                context={"workflow_id": str(workflow_id), "status": workflow.status},
            )

        templates = await self._load_templates(workflow.dag, tenant_id=tenant_id)
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

        event_type = WorkflowRunStartedEvent.event_type
        async with self._workflow_run_repo.with_tenant(tenant_id) as session:
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
            )
            event = WorkflowRunStartedEvent(
                run_id=run.id, workflow_id=workflow_id, tenant_id=tenant_id
            )
            event_id = await publish(
                event_type, event, session=session, correlation_id=correlation_id
            )
            # commit happens at __aexit__ if no exception is raised.

        await notify_best_effort(event_id, event_type)

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
        return StartRunResponse(run_id=run.id, status="running", warnings=warnings)

    async def _load_templates(
        self, dag_payload: dict[str, Any], *, tenant_id: UUID | None
    ) -> dict[str, AgentTemplate]:
        """Preload every node's :class:`AgentTemplate` — one query per node
        (dette assumée, mirror 4.1/4.8's N sequential queries; cf Dev Notes §
        "Résolution batch").

        A missing template here is an INTERNAL INCONSISTENCY, not a client
        error: Story 4.1 validated every ``agent_template_id`` at creation and
        no delete endpoint exists, so reaching this branch means the stored
        DAG references a row that vanished. ``require_by_id`` reported that as
        a 404 on ``POST /workflows/{workflow_id}/runs``, whose 404 already
        means "unknown workflow_id" — telling the caller their perfectly valid
        workflow id was wrong, and inviting them to retry forever. 500 instead:
        the id in the URL is fine, the server's data is not.
        """
        templates: dict[str, AgentTemplate] = {}
        for node in dag_payload.get("nodes", []):
            template_id = UUID(node["agent_template_id"])
            template = await self._template_repo.get_by_id(template_id, tenant_id=tenant_id)
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
        await self._execute(
            run_id,
            workflow,
            templates,
            initial_input=None,
            correlation_id=run.correlation_id,
            started_at=datetime.now(UTC),
            fallback_input=fallback_input if isinstance(fallback_input, dict) else None,
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

        try:
            dag = _dag_from_stored(workflow.dag)
            graph = build_state_graph(dag, templates, self._llm_router).compile(
                checkpointer=self._checkpointer
            )
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

        try:
            if state_input is None:
                state_input = await self._restart_input_if_never_checkpointed(
                    graph, config, fallback_input, run_id=run_id, correlation_id=correlation_id
                )

            async for update in graph.astream(  # type: ignore[call-overload]
                state_input,
                config,
                stream_mode="updates",
            ):
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
        total_duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        node_metrics = state_values.get("node_metrics") or {}
        metrics = _aggregate_metrics(node_metrics, total_duration_ms=total_duration_ms)
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

        node_outputs = state_values.get("node_outputs") or {}
        node_metrics = state_values.get("node_metrics") or {}
        # AC4 — partial metrics from already-completed nodes stay aggregated,
        # and the run's REAL wall-clock duration is reported. The previous
        # hardcoded 0 made every failed run look instantaneous, so the AC4
        # metric could not be used to tell a fast failure from a run that
        # burned nine minutes before dying.
        total_duration_ms = int((ended_at - started_at).total_seconds() * 1000)
        metrics = _aggregate_metrics(node_metrics, total_duration_ms=total_duration_ms)
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

        checkpoint = {
            "last_node_id": failed_node_id,
            "node_statuses": node_statuses,
            "node_outputs_preview": {nid: _preview(output) for nid, output in node_outputs.items()},
            "last_error": error_summary,
        }

        # Compare-and-set — see `_mark_completed`. A run another writer has
        # already finished must not be rewritten as failed, and must not
        # emit a `failed` event contradicting the row.
        rowcount = await self._workflow_run_repo.update_status(
            run_id,
            status="error",
            ended_at=ended_at,
            metrics=metrics,
            only_if_status="running",
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
