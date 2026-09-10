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

from typing import TYPE_CHECKING, Any
from uuid import UUID

from agentive_backend.features.workflow_engine.domain import (
    DomainValidationError,
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
    detect_cycle,
    find_dangling_edges,
    find_duplicate_node_ids,
)
from agentive_backend.features.workflow_engine.domain import parse as parse_condition
from agentive_backend.features.workflow_engine.schemas import (
    CreateWorkflowResponse,
    DiversityWarning,
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)
from agentive_backend.shared.contracts.diversity import LLMSelection, check_llm_diversity
from agentive_backend.shared.contracts.events import WorkflowCreatedEvent
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import ValidationError
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo

if TYPE_CHECKING:
    from agentive_backend.infra.db.models import AgentTemplate

_log = get_logger(__name__)

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
        warnings: list[DiversityWarning] = []
        checked_pairs: set[tuple[str, str]] = set()
        for edge in dag.edges:
            controller_template = templates[edge.to_node_id]
            if controller_template.archetype != _CONTROLLER_ARCHETYPE:
                continue
            pair = (edge.from_node_id, edge.to_node_id)
            if pair in checked_pairs:
                continue
            checked_pairs.add(pair)
            producer_template = templates[edge.from_node_id]
            controller_selection = _llm_selection_from_config(controller_template.config or {})
            producer_selection = _llm_selection_from_config(producer_template.config or {})
            result = check_llm_diversity(controller_selection, producer_selection)
            if result.is_diverse is False:
                warnings.append(
                    DiversityWarning(
                        controller_node_id=edge.to_node_id,
                        producer_node_id=edge.from_node_id,
                        controller_template_id=controller_template.id,
                        producer_template_id=producer_template.id,
                        reason=result.reason,
                    )
                )

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


__all__ = ["WorkflowService"]
