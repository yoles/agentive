"""Unit tests — :class:`WorkflowService.create_workflow` (Story 4.1 T8.3).

Mock-driven (``AgentTemplateRepo``/``WorkflowRepo`` mocked) — mirrors
``tests/unit/agent_registry/test_instantiate_template_service.py``. The
Postgres-real integration path (atomicity, outbox event persisted) lives in
``tests/integration/workflow_engine/test_create_workflow_e2e.py`` (T8.6).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from agentive_backend.features.workflow_engine.schemas import (
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)
from agentive_backend.features.workflow_engine.service import (
    WorkflowExecutionService,
    WorkflowService,
)
from agentive_backend.shared.correlation import set_correlation_id
from agentive_backend.shared.exceptions import InternalError, NotFoundError, ValidationError


@pytest.fixture
def event_publish_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """Patch ``service.publish`` + ``service.notify_best_effort`` so we don't touch the bus."""
    pub_mock = AsyncMock(return_value=uuid4())
    notify_mock = AsyncMock(return_value=None)
    import agentive_backend.features.workflow_engine.service as svc_module

    monkeypatch.setattr(svc_module, "publish", pub_mock)
    monkeypatch.setattr(svc_module, "notify_best_effort", notify_mock)
    yield pub_mock


def _template(
    *,
    archetype: str = "producteur",
    output_contract: dict[str, Any] | None = None,
    llm_model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> SimpleNamespace:
    config: dict[str, Any] = {}
    if output_contract is not None:
        config["output_contract"] = output_contract
    if llm_model is not None:
        config["llm_model"] = llm_model
        config["llm_params"] = {"temperature": temperature, "max_tokens": max_tokens}
    return SimpleNamespace(id=uuid4(), archetype=archetype, config=config)


def _make_service(
    templates_by_id: dict[UUID, SimpleNamespace],
) -> tuple[WorkflowService, AsyncMock, AsyncMock]:
    """Returns (service, workflow_repo, template_repo)."""
    session_mock = AsyncMock()
    session_mock.flush = AsyncMock()
    session_mock.refresh = AsyncMock()
    session_mock.add = MagicMock()

    @asynccontextmanager
    async def _with_tenant(_tenant_id: Any) -> AsyncIterator[AsyncMock]:
        yield session_mock

    workflow_repo = AsyncMock()
    workflow_repo.with_tenant = _with_tenant

    async def _mock_create(
        _session: object,
        *,
        name: str,
        dag: dict[str, Any],
        version: int = 1,
        status: str = "active",
        tenant_id: Any | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid4(),
            name=name,
            dag=dag,
            version=version,
            status=status,
            tenant_id=tenant_id,
            created_at=datetime.now(UTC),
        )

    workflow_repo.create_in_session = AsyncMock(side_effect=_mock_create)

    template_repo = AsyncMock()

    async def _get_by_id(template_id: UUID, *, tenant_id: Any | None = None) -> Any:
        return templates_by_id.get(template_id)

    template_repo.get_by_id = AsyncMock(side_effect=_get_by_id)

    service = WorkflowService(workflow_repo=workflow_repo, template_repo=template_repo)
    return service, workflow_repo, template_repo


# ─── AC1 — structural validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_create_workflow_happy_path_single_node_no_edges(
    event_publish_mock: AsyncMock,
) -> None:
    """A single-node DAG with no edges is a valid mono-agent workflow."""
    tpl = _template()
    service, wrepo, _trepo = _make_service({tpl.id: tpl})

    response = await service.create_workflow(
        name="solo",
        nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
        edges=[],
    )

    assert response.version == 1
    assert response.warnings == []
    wrepo.create_in_session.assert_awaited_once()
    event_publish_mock.assert_awaited_once()
    event_type = event_publish_mock.await_args.args[0]
    assert event_type == "workflow_engine.workflow.created"
    event_payload = event_publish_mock.await_args.args[1]
    assert event_payload.node_count == 1


@pytest.mark.asyncio
async def test_create_workflow_duplicate_node_id_raises_422(
    event_publish_mock: AsyncMock,
) -> None:
    tpl = _template()
    service, wrepo, _trepo = _make_service({tpl.id: tpl})

    with pytest.raises(ValidationError, match="Duplicate"):
        await service.create_workflow(
            name="dup",
            nodes=[
                WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id),
                WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id),
            ],
            edges=[],
        )
    wrepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_dangling_edge_raises_422(
    event_publish_mock: AsyncMock,
) -> None:
    tpl = _template()
    service, wrepo, _trepo = _make_service({tpl.id: tpl})

    with pytest.raises(ValidationError, match="undeclared node"):
        await service.create_workflow(
            name="dangling",
            nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
            edges=[WorkflowEdgeRequest(from_node_id="a", to_node_id="ghost")],
        )
    wrepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_unknown_agent_template_id_raises_422(
    event_publish_mock: AsyncMock,
) -> None:
    service, wrepo, _trepo = _make_service({})
    unknown_id = uuid4()

    with pytest.raises(ValidationError, match="unknown agent_template_id"):
        await service.create_workflow(
            name="missing-template",
            nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=unknown_id)],
            edges=[],
        )
    wrepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_cycle_raises_422(event_publish_mock: AsyncMock) -> None:
    tpl_a, tpl_b = _template(), _template()
    service, wrepo, _trepo = _make_service({tpl_a.id: tpl_a, tpl_b.id: tpl_b})

    with pytest.raises(ValidationError, match="cycle"):
        await service.create_workflow(
            name="cyclic",
            nodes=[
                WorkflowNodeRequest(node_id="a", agent_template_id=tpl_a.id),
                WorkflowNodeRequest(node_id="b", agent_template_id=tpl_b.id),
            ],
            edges=[
                WorkflowEdgeRequest(from_node_id="a", to_node_id="b"),
                WorkflowEdgeRequest(from_node_id="b", to_node_id="a"),
            ],
        )
    wrepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_invalid_dsl_syntax_raises_422(
    event_publish_mock: AsyncMock,
) -> None:
    tpl_a = _template(output_contract={"core": {"status": "string"}})
    tpl_b = _template()
    service, wrepo, _trepo = _make_service({tpl_a.id: tpl_a, tpl_b.id: tpl_b})

    with pytest.raises(ValidationError, match="Invalid branching condition"):
        await service.create_workflow(
            name="bad-dsl",
            nodes=[
                WorkflowNodeRequest(node_id="a", agent_template_id=tpl_a.id),
                WorkflowNodeRequest(node_id="b", agent_template_id=tpl_b.id),
            ],
            edges=[
                WorkflowEdgeRequest(
                    from_node_id="a",
                    to_node_id="b",
                    condition="output.status == 'ok' and output.other == 1",
                )
            ],
        )
    wrepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


# ─── AC3 — branching-condition variable exposure ───────────────────────


@pytest.mark.asyncio
async def test_create_workflow_condition_variable_in_core_passes(
    event_publish_mock: AsyncMock,
) -> None:
    tpl_a = _template(output_contract={"core": {"status": "string"}})
    tpl_b = _template()
    service, wrepo, _trepo = _make_service({tpl_a.id: tpl_a, tpl_b.id: tpl_b})

    response = await service.create_workflow(
        name="valid-condition",
        nodes=[
            WorkflowNodeRequest(node_id="a", agent_template_id=tpl_a.id),
            WorkflowNodeRequest(node_id="b", agent_template_id=tpl_b.id),
        ],
        edges=[
            WorkflowEdgeRequest(from_node_id="a", to_node_id="b", condition="output.status == 'ok'")
        ],
    )
    assert response.version == 1
    wrepo.create_in_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_workflow_condition_variable_absent_from_output_contract_raises_422(
    event_publish_mock: AsyncMock,
) -> None:
    tpl_a = _template(output_contract={"core": {"other_field": "string"}})
    tpl_b = _template()
    service, wrepo, _trepo = _make_service({tpl_a.id: tpl_a, tpl_b.id: tpl_b})

    with pytest.raises(ValidationError, match=r"output\.status") as exc_info:
        await service.create_workflow(
            name="missing-variable",
            nodes=[
                WorkflowNodeRequest(node_id="a", agent_template_id=tpl_a.id),
                WorkflowNodeRequest(node_id="b", agent_template_id=tpl_b.id),
            ],
            edges=[
                WorkflowEdgeRequest(
                    from_node_id="a", to_node_id="b", condition="output.status == 'ok'"
                )
            ],
        )
    assert exc_info.value.context["from_node_id"] == "a"
    wrepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_condition_variable_only_in_extras_still_rejected(
    event_publish_mock: AsyncMock,
) -> None:
    """AC3 — ``extras`` is the permissive, non-contractual zone; never counts as exposed."""
    tpl_a = _template(output_contract={"core": {}, "extras": {"status": "string"}})
    tpl_b = _template()
    service, wrepo, _trepo = _make_service({tpl_a.id: tpl_a, tpl_b.id: tpl_b})

    with pytest.raises(ValidationError, match=r"output_contract\.core"):
        await service.create_workflow(
            name="extras-only",
            nodes=[
                WorkflowNodeRequest(node_id="a", agent_template_id=tpl_a.id),
                WorkflowNodeRequest(node_id="b", agent_template_id=tpl_b.id),
            ],
            edges=[
                WorkflowEdgeRequest(
                    from_node_id="a", to_node_id="b", condition="output.status == 'ok'"
                )
            ],
        )
    wrepo.create_in_session.assert_not_awaited()


# ─── AC4 — Controller/Producer LLM diversity (non-blocking) ────────────


@pytest.mark.asyncio
async def test_create_workflow_identical_llm_config_produces_warning(
    event_publish_mock: AsyncMock,
) -> None:
    controller = _template(archetype="controleur", llm_model="claude-3-5-sonnet-20241022")
    producer = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    service, wrepo, _trepo = _make_service({controller.id: controller, producer.id: producer})

    response = await service.create_workflow(
        name="same-config",
        nodes=[
            WorkflowNodeRequest(node_id="producer", agent_template_id=producer.id),
            WorkflowNodeRequest(node_id="controller", agent_template_id=controller.id),
        ],
        edges=[WorkflowEdgeRequest(from_node_id="producer", to_node_id="controller")],
    )
    assert len(response.warnings) == 1
    warning = response.warnings[0]
    assert warning.controller_node_id == "controller"
    assert warning.producer_node_id == "producer"
    wrepo.create_in_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_workflow_diverse_llm_config_produces_no_warning(
    event_publish_mock: AsyncMock,
) -> None:
    controller = _template(archetype="controleur", llm_model="claude-3-5-sonnet-20241022")
    producer = _template(archetype="producteur", llm_model="gpt-4o")
    service, _wrepo, _trepo = _make_service({controller.id: controller, producer.id: producer})

    response = await service.create_workflow(
        name="diverse-config",
        nodes=[
            WorkflowNodeRequest(node_id="producer", agent_template_id=producer.id),
            WorkflowNodeRequest(node_id="controller", agent_template_id=controller.id),
        ],
        edges=[WorkflowEdgeRequest(from_node_id="producer", to_node_id="controller")],
    )
    assert response.warnings == []


@pytest.mark.asyncio
async def test_create_workflow_incomplete_llm_config_produces_no_warning(
    event_publish_mock: AsyncMock,
) -> None:
    """Neither config incomplete nor diverse — never a false alarm."""
    controller = _template(archetype="controleur")  # no llm_model set
    producer = _template(archetype="producteur", llm_model="gpt-4o")
    service, _wrepo, _trepo = _make_service({controller.id: controller, producer.id: producer})

    response = await service.create_workflow(
        name="incomplete-config",
        nodes=[
            WorkflowNodeRequest(node_id="producer", agent_template_id=producer.id),
            WorkflowNodeRequest(node_id="controller", agent_template_id=controller.id),
        ],
        edges=[WorkflowEdgeRequest(from_node_id="producer", to_node_id="controller")],
    )
    assert response.warnings == []


@pytest.mark.asyncio
async def test_create_workflow_partial_llm_params_produces_no_warning(
    event_publish_mock: AsyncMock,
) -> None:
    """P-02 (Story 2.8) mirrored at this layer: a non-empty but partial
    llm_params (temperature present, max_tokens missing) must never
    fabricate a default for the missing key — treated as incomplete, never
    a false-positive/false-negative warning."""
    controller = _template(archetype="controleur")
    controller.config = {
        "llm_model": "claude-3-5-sonnet-20241022",
        "llm_params": {"temperature": 0.7},
    }
    producer = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    service, _wrepo, _trepo = _make_service({controller.id: controller, producer.id: producer})

    response = await service.create_workflow(
        name="partial-config",
        nodes=[
            WorkflowNodeRequest(node_id="producer", agent_template_id=producer.id),
            WorkflowNodeRequest(node_id="controller", agent_template_id=controller.id),
        ],
        edges=[WorkflowEdgeRequest(from_node_id="producer", to_node_id="controller")],
    )
    assert response.warnings == []


@pytest.mark.asyncio
async def test_create_workflow_no_controller_node_produces_no_warnings(
    event_publish_mock: AsyncMock,
) -> None:
    tpl_a = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    tpl_b = _template(archetype="analyste", llm_model="claude-3-5-sonnet-20241022")
    service, _wrepo, _trepo = _make_service({tpl_a.id: tpl_a, tpl_b.id: tpl_b})

    response = await service.create_workflow(
        name="no-controller",
        nodes=[
            WorkflowNodeRequest(node_id="a", agent_template_id=tpl_a.id),
            WorkflowNodeRequest(node_id="b", agent_template_id=tpl_b.id),
        ],
        edges=[WorkflowEdgeRequest(from_node_id="a", to_node_id="b")],
    )
    assert response.warnings == []


@pytest.mark.asyncio
async def test_create_workflow_multiple_producers_to_same_controller_one_warning_each(
    event_publish_mock: AsyncMock,
) -> None:
    controller = _template(archetype="controleur", llm_model="claude-3-5-sonnet-20241022")
    producer_1 = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    producer_2 = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    service, _wrepo, _trepo = _make_service(
        {controller.id: controller, producer_1.id: producer_1, producer_2.id: producer_2}
    )

    response = await service.create_workflow(
        name="fan-in",
        nodes=[
            WorkflowNodeRequest(node_id="p1", agent_template_id=producer_1.id),
            WorkflowNodeRequest(node_id="p2", agent_template_id=producer_2.id),
            WorkflowNodeRequest(node_id="controller", agent_template_id=controller.id),
        ],
        edges=[
            WorkflowEdgeRequest(from_node_id="p1", to_node_id="controller"),
            WorkflowEdgeRequest(from_node_id="p2", to_node_id="controller"),
        ],
    )
    assert len(response.warnings) == 2
    assert {w.producer_node_id for w in response.warnings} == {"p1", "p2"}


@pytest.mark.asyncio
async def test_create_workflow_outgoing_edge_from_controller_is_not_checked(
    event_publish_mock: AsyncMock,
) -> None:
    """D84 — only INCOMING edges to a controleur node are evaluated for diversity."""
    controller = _template(archetype="controleur", llm_model="claude-3-5-sonnet-20241022")
    downstream = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    service, _wrepo, _trepo = _make_service({controller.id: controller, downstream.id: downstream})

    response = await service.create_workflow(
        name="outgoing-edge",
        nodes=[
            WorkflowNodeRequest(node_id="controller", agent_template_id=controller.id),
            WorkflowNodeRequest(node_id="downstream", agent_template_id=downstream.id),
        ],
        edges=[WorkflowEdgeRequest(from_node_id="controller", to_node_id="downstream")],
    )
    assert response.warnings == []


@pytest.mark.asyncio
async def test_create_workflow_duplicate_edge_produces_single_warning(
    event_publish_mock: AsyncMock,
) -> None:
    """The same (from_node_id, to_node_id) pair submitted twice must not
    double the diversity warning — duplicate edges aren't forbidden by AC1,
    but a repeated producer -> controller pair is one relationship."""
    controller = _template(archetype="controleur", llm_model="claude-3-5-sonnet-20241022")
    producer = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    service, _wrepo, _trepo = _make_service({controller.id: controller, producer.id: producer})

    response = await service.create_workflow(
        name="duplicate-edge",
        nodes=[
            WorkflowNodeRequest(node_id="producer", agent_template_id=producer.id),
            WorkflowNodeRequest(node_id="controller", agent_template_id=controller.id),
        ],
        edges=[
            WorkflowEdgeRequest(from_node_id="producer", to_node_id="controller"),
            WorkflowEdgeRequest(from_node_id="producer", to_node_id="controller"),
        ],
    )
    assert len(response.warnings) == 1


# ═══════════════════════════════════════════════════════════════════════
# WorkflowExecutionService — Story 4.2 T10.1
# ═══════════════════════════════════════════════════════════════════════


class _FakeCompiledGraph:
    """Test double for a LangGraph compiled graph — ``astream`` replays a
    fixed sequence of ``{node_id: update}`` dicts (optionally raising
    partway through), ``aget_state`` returns a controllable snapshot."""

    def __init__(
        self,
        updates: list[dict[str, Any]],
        final_state: dict[str, Any],
        *,
        raise_after_updates: bool = False,
        # `BaseException`, not `Exception` — the shutdown path deliberately
        # injects `asyncio.CancelledError` here (cf the cancellation test).
        raise_exc: BaseException | None = None,
        next_nodes: tuple[str, ...] = (),
    ) -> None:
        self._updates = updates
        self._final_state = final_state
        self._raise_after_updates = raise_after_updates
        self._raise_exc = raise_exc or RuntimeError("boom")
        self._next_nodes = next_nodes
        self.aget_state_call_count = 0
        # Accumulated state as the stream progresses. The previous double
        # returned `final_state` from EVERY `aget_state`, including reads
        # taken mid-stream — so "the checkpoint written after node a
        # contains only node a" was unobservable, and the redundant per-node
        # state reads in a fan-out (review finding #21) were structurally
        # invisible to these tests.
        self._progressive: dict[str, Any] = {
            "node_outputs": {},
            "node_metrics": {},
            "routing_decisions": {},
        }
        self._stream_finished = False

    async def astream(
        self, _state_input: Any, _config: Any, *, stream_mode: str
    ) -> AsyncIterator[dict[str, Any]]:
        assert stream_mode == "updates"
        for update in self._updates:
            for node_update in update.values():
                for key in ("node_outputs", "node_metrics", "routing_decisions"):
                    self._progressive[key].update((node_update or {}).get(key) or {})
            yield update
        # Set BEFORE the raise: streaming is over either way, so the
        # failure path's own state lookup still sees `final_state` (which is
        # what the failure tests describe). Only MID-stream reads are
        # progressive.
        self._stream_finished = True
        if self._raise_after_updates:
            raise self._raise_exc

    async def aget_state(self, _config: Any) -> SimpleNamespace:
        self.aget_state_call_count += 1
        values = self._final_state if self._stream_finished else dict(self._progressive)
        return SimpleNamespace(values=values, next=self._next_nodes)


class _FakeGraphBuilder:
    def __init__(self, compiled: _FakeCompiledGraph) -> None:
        self._compiled = compiled

    def compile(self, *, checkpointer: Any) -> _FakeCompiledGraph:
        return self._compiled


def _patch_build_state_graph(monkeypatch: pytest.MonkeyPatch, compiled: _FakeCompiledGraph) -> None:
    import agentive_backend.features.workflow_engine.service as svc_module

    monkeypatch.setattr(
        svc_module, "build_state_graph", lambda *a, **kw: _FakeGraphBuilder(compiled)
    )


def _workflow(*, status: str = "active", dag: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        status=status,
        dag=dag if dag is not None else {"nodes": [], "edges": []},
        tenant_id=None,
    )


def _workflow_run(
    *,
    workflow_id: UUID | None = None,
    correlation_id: UUID | None = None,
    status: str = "running",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workflow_id=workflow_id or uuid4(),
        correlation_id=correlation_id or uuid4(),
        started_at=datetime.now(UTC),
        checkpoint=None,
        status=status,
    )


def _make_execution_service() -> tuple[WorkflowExecutionService, AsyncMock, AsyncMock, AsyncMock]:
    """Returns (service, workflow_repo, workflow_run_repo, template_repo)."""
    session_mock = AsyncMock()
    session_mock.flush = AsyncMock()
    session_mock.refresh = AsyncMock()
    session_mock.add = MagicMock()

    @asynccontextmanager
    async def _with_tenant(_tenant_id: Any) -> AsyncIterator[AsyncMock]:
        yield session_mock

    workflow_repo = AsyncMock()
    workflow_run_repo = AsyncMock()
    workflow_run_repo.with_tenant = _with_tenant
    # `update_status` returns a ROWCOUNT the service now branches on (the
    # `only_if_status="running"` compare-and-set). Left as a bare AsyncMock
    # it would return a MagicMock, which compares unequal to 0 by accident —
    # the tests would pass for the wrong reason.
    workflow_run_repo.update_status = AsyncMock(return_value=1)
    template_repo = AsyncMock()

    service = WorkflowExecutionService(
        workflow_repo=workflow_repo,
        workflow_run_repo=workflow_run_repo,
        template_repo=template_repo,
        llm_router=AsyncMock(),
        checkpointer=AsyncMock(),
        # Explicitly empty: these tests inject `routing_decisions` straight
        # into the fake graph's updates, so no rule ever needs to evaluate.
        routing_rules=(),
    )
    return service, workflow_repo, workflow_run_repo, template_repo


# ─── start_run — AC1 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_run_unknown_workflow_raises_not_found(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.side_effect = NotFoundError(detail="Workflow 'x' not found")

    with pytest.raises(NotFoundError):
        await service.start_run(workflow_id=uuid4(), run_input={})
    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_run_inactive_workflow_raises_422(event_publish_mock: AsyncMock) -> None:
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="draft")

    with pytest.raises(ValidationError, match="not active"):
        await service.start_run(workflow_id=uuid4(), run_input={})
    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_run_returns_201_immediately_without_awaiting_drive_run(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — the response must not block on the run's execution."""
    set_correlation_id(str(uuid4()))
    workflow_id = uuid4()
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    run = _workflow_run(workflow_id=workflow_id)
    workflow_run_repo.create_in_session.return_value = run

    gate = asyncio.Event()

    async def _slow_drive(*_args: Any, **_kwargs: Any) -> None:
        await gate.wait()

    service._drive_run = AsyncMock(side_effect=_slow_drive)  # type: ignore[method-assign]

    try:
        response = await service.start_run(workflow_id=workflow_id, run_input={"x": 1})
        assert response.run_id == run.id
        assert response.status == "running"
        # The background task exists but is still gated — proves start_run
        # returned before `_drive_run` completed.
        assert not service._drive_run.await_args_list  # type: ignore[union-attr]
        await asyncio.sleep(0)
        service._drive_run.assert_awaited_once()  # type: ignore[union-attr]
    finally:
        gate.set()
        await asyncio.sleep(0)

    event_type = event_publish_mock.await_args_list[0].args[0]
    assert event_type == "workflow_engine.workflow_run.started"


@pytest.mark.asyncio
async def test_start_run_preloads_templates_per_dag_node(event_publish_mock: AsyncMock) -> None:
    set_correlation_id(str(uuid4()))
    workflow_id = uuid4()
    template_id = uuid4()
    service, workflow_repo, workflow_run_repo, template_repo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(
        status="active",
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(template_id)}], "edges": []},
    )
    run = _workflow_run(workflow_id=workflow_id)
    workflow_run_repo.create_in_session.return_value = run
    template_repo.get_by_id.return_value = SimpleNamespace(config={})
    service._drive_run = AsyncMock()  # type: ignore[method-assign]

    await service.start_run(workflow_id=workflow_id, run_input={"seed": 1})

    # `get_by_id` + explicit 500, NOT `require_by_id` + 404: a template that
    # vanished is a server-side inconsistency, and this endpoint's 404 already
    # means "unknown workflow_id".
    template_repo.get_by_id.assert_awaited_once_with(template_id, tenant_id=None)
    # The caller's input is stamped on the row so a crash before LangGraph's
    # first checkpoint can still be restarted from START (AC3).
    create_kwargs = workflow_run_repo.create_in_session.await_args.kwargs
    assert create_kwargs["checkpoint"]["task_input"] == {"seed": 1}
    # Config fingerprints are stamped alongside so a resume can tell whether
    # the templates changed across the crash gap (intent gap 5).
    assert set(create_kwargs["checkpoint"]["template_fingerprints"]) == {"a"}


# ─── _execute (via _drive_run) — AC2/AC4 ────────────────────────────


@pytest.mark.asyncio
async def test_drive_run_syncs_checkpoint_and_publishes_step_completed(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    workflow = _workflow(
        status="active",
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []},
    )
    compiled = _FakeCompiledGraph(
        updates=[
            {"a": {"node_outputs": {"a": {"x": 1}}, "node_metrics": {"a": {"duration_ms": 42}}}}
        ],
        final_state={
            "node_outputs": {"a": {"x": 1}},
            "node_metrics": {"a": {"duration_ms": 42, "input_tokens": 1, "output_tokens": 1}},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    run_id = uuid4()
    templates = {"a": SimpleNamespace(config={})}
    await service._drive_run(run_id, workflow, templates, {"foo": "bar"}, correlation_id=uuid4())

    workflow_run_repo.update_checkpoint.assert_awaited_once()
    checkpoint_kwargs = workflow_run_repo.update_checkpoint.await_args.kwargs
    assert checkpoint_kwargs["checkpoint"]["last_node_id"] == "a"
    assert checkpoint_kwargs["checkpoint"]["node_statuses"] == {"a": "success"}

    published_types = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.step_completed" in published_types
    assert "workflow_engine.workflow_run.completed" in published_types

    workflow_run_repo.update_status.assert_awaited_once()
    status_kwargs = workflow_run_repo.update_status.await_args.kwargs
    assert status_kwargs["status"] == "completed"
    assert status_kwargs["metrics"]["per_node"]["a"]["input_tokens"] == 1


@pytest.mark.asyncio
async def test_drive_run_error_path_marks_run_failed_without_reraising(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """T5.3 — a node exception must be caught, mapped to status=error +
    a `failed` event with partial metrics, and NEVER re-raised (the caller
    is a bare `asyncio.Task`)."""
    workflow = _workflow(
        status="active",
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []},
    )
    compiled = _FakeCompiledGraph(
        updates=[
            {"a": {"node_outputs": {"a": {"x": 1}}, "node_metrics": {"a": {"duration_ms": 10}}}}
        ],
        final_state={
            "node_outputs": {"a": {"x": 1}},
            "node_metrics": {"a": {"duration_ms": 10, "input_tokens": 1, "output_tokens": 1}},
        },
        raise_after_updates=True,
        raise_exc=RuntimeError("LLM provider exploded"),
        next_nodes=("b",),
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    run_id = uuid4()
    # No exception should propagate out of `_drive_run`.
    await service._drive_run(run_id, workflow, {}, {}, correlation_id=uuid4())

    status_kwargs = workflow_run_repo.update_status.await_args.kwargs
    assert status_kwargs["status"] == "error"
    # AC4 — partial metrics from the already-completed node stay aggregated.
    assert status_kwargs["metrics"]["per_node"]["a"]["input_tokens"] == 1

    checkpoint_kwargs = workflow_run_repo.update_checkpoint.await_args.kwargs
    assert "LLM provider exploded" in checkpoint_kwargs["checkpoint"]["last_error"]
    assert checkpoint_kwargs["checkpoint"]["last_node_id"] == "b"

    published_types = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.failed" in published_types
    assert "workflow_engine.workflow_run.completed" not in published_types


@pytest.mark.asyncio
async def test_resume_run_passes_none_as_graph_input(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """T5.5 — resuming passes `None` as the graph input so LangGraph resumes
    from the last committed checkpoint instead of restarting at START."""
    workflow = _workflow(status="active", dag={"nodes": [], "edges": []})
    compiled = _FakeCompiledGraph(updates=[], final_state={"node_outputs": {}, "node_metrics": {}})
    captured_inputs: list[Any] = []

    async def _astream(state_input: Any, _config: Any, *, stream_mode: str) -> AsyncIterator[Any]:
        captured_inputs.append(state_input)
        return
        yield  # pragma: no cover - makes this an async generator

    compiled.astream = _astream  # type: ignore[method-assign]
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    run = _workflow_run(workflow_id=workflow.id)
    workflow_run_repo.get_by_id.return_value = run

    await service._resume_run(run.id, workflow, {})

    assert captured_inputs == [None]


@pytest.mark.asyncio
async def test_resume_run_missing_run_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run vanished between the recovery worker's scan and this call —
    not an error, just nothing to do."""
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_run_repo.get_by_id.return_value = None

    await service._resume_run(uuid4(), _workflow(), {})

    workflow_run_repo.update_status.assert_not_awaited()


# ─── Intent gap 2 — shutdown cancels instead of burying ────────────────


@pytest.mark.asyncio
async def test_execute_cancellation_does_not_mark_the_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole basis of the shutdown fix.

    `cancel_inflight_runs` relies on `asyncio.CancelledError` deriving from
    `BaseException` so it passes straight through `_execute`'s
    `except Exception` — no `failed` transition, the row stays `running` and
    the next process's recovery sweep can claim it. If `_execute` ever grew
    a `BaseException` handler (or LangGraph re-wrapped the cancellation as a
    plain `Exception`), every run interrupted by a deploy would be buried in
    the terminal `error` status instead.
    """
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[],
        final_state={"node_outputs": {}, "node_metrics": {}},
        raise_after_updates=True,
        raise_exc=asyncio.CancelledError(),
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    with pytest.raises(asyncio.CancelledError):
        await service._execute(
            uuid4(),
            workflow,
            {},
            initial_input={"x": 1},
            correlation_id=uuid4(),
            started_at=datetime.now(UTC),
        )

    workflow_run_repo.update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_inflight_runs_cancels_and_reports() -> None:
    """Shutdown must not wait for runs to FINISH (NFR3) — only for the
    cancellations it just issued to unwind."""
    from agentive_backend.features.workflow_engine.service import (
        _background_tasks,
        cancel_inflight_runs,
    )

    started = asyncio.Event()

    async def _never_ends() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_ends())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    await started.wait()

    try:
        assert await cancel_inflight_runs() == 1
        assert task.cancelled()
    finally:
        _background_tasks.discard(task)

    # Idempotent — a second shutdown pass has nothing left to cancel.
    assert await cancel_inflight_runs() == 0


# ─── Lot 1 — findings #10-14 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_build_failure_marks_the_run_error(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#10 — compilation used to sit OUTSIDE the try. A stored DAG that no
    longer parses (or a `node_id` LangGraph reserves) escaped into the bare
    asyncio.Task: no `error` status, no `failed` event, row stuck on
    `running` forever and re-resumed by every recovery sweep."""
    import agentive_backend.features.workflow_engine.service as svc_module

    def _boom(*_a: Any, **_kw: Any) -> Any:
        raise ValueError("`__start__` is a reserved node id")

    monkeypatch.setattr(svc_module, "build_state_graph", _boom)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    workflow = _workflow()

    await service._execute(
        uuid4(),
        workflow,
        {},
        initial_input={"x": 1},
        correlation_id=uuid4(),
        started_at=datetime.now(UTC),
    )

    assert workflow_run_repo.update_status.await_args.kwargs["status"] == "error"
    assert event_publish_mock.await_args.args[0] == "workflow_engine.workflow_run.failed"


@pytest.mark.asyncio
async def test_completion_failure_marks_the_run_error(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#10 (other half) — the final `aget_state` + `_mark_completed` sat
    AFTER the except block, so a corrupt final state froze the row too."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(updates=[], final_state={"node_outputs": {}, "node_metrics": {}})

    call_count = 0

    async def _aget_state(_config: Any) -> Any:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("state read exploded")

    compiled.aget_state = _aget_state  # type: ignore[method-assign]
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._execute(
        uuid4(),
        workflow,
        {},
        initial_input={"x": 1},
        correlation_id=uuid4(),
        started_at=datetime.now(UTC),
    )

    assert workflow_run_repo.update_status.await_args.kwargs["status"] == "error"


@pytest.mark.asyncio
async def test_checkpoint_sync_failure_does_not_abort_a_healthy_run(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#11 — the applicative checkpoint is diagnostic (LangGraph's own
    checkpoint is the technical source of truth), yet a transient DB error
    on it used to mark a run that had executed perfectly as `error`."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_metrics": {"a": {"duration_ms": 12}}}}],
        final_state={"node_outputs": {"a": {"ok": True}}, "node_metrics": {"a": {}}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_run_repo.update_checkpoint = AsyncMock(side_effect=RuntimeError("db hiccup"))

    await service._execute(
        uuid4(),
        workflow,
        {},
        initial_input={"x": 1},
        correlation_id=uuid4(),
        started_at=datetime.now(UTC),
    )

    assert workflow_run_repo.update_status.await_args.kwargs["status"] == "completed"


@pytest.mark.asyncio
async def test_mark_failed_does_not_overwrite_an_already_terminal_run(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#12 — the compare-and-set. A 0 rowcount means another writer already
    finished this run; emitting a `failed` event anyway would tell every SSE
    client the opposite of what the row says."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[],
        final_state={"node_outputs": {}, "node_metrics": {}},
        raise_after_updates=True,
        raise_exc=RuntimeError("boom"),
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_run_repo.update_status = AsyncMock(return_value=0)

    await service._execute(
        uuid4(),
        workflow,
        {},
        initial_input={"x": 1},
        correlation_id=uuid4(),
        started_at=datetime.now(UTC),
    )

    assert workflow_run_repo.update_status.await_args.kwargs["only_if_status"] == "running"
    workflow_run_repo.update_checkpoint.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_run_refuses_a_run_that_is_no_longer_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#12 — checking only for `None` missed the run that still EXISTS but
    is already terminal."""
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_run_repo.get_by_id.return_value = _workflow_run(status="completed")

    await service._resume_run(uuid4(), _workflow(), {})

    workflow_run_repo.update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_summary_redacts_a_dsn_password(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#13 — NFR9. `str(exc)` on a psycopg error carries the full DSN; this
    string is persisted in `checkpoint.last_error` AND streamed verbatim to
    SSE clients."""
    workflow = _workflow()
    leak = "connection failed: postgresql://agentive_app:sup3r-s3cret@db:5432/agentive"
    compiled = _FakeCompiledGraph(
        updates=[],
        final_state={"node_outputs": {}, "node_metrics": {}},
        raise_after_updates=True,
        raise_exc=RuntimeError(leak),
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._execute(
        uuid4(),
        workflow,
        {},
        initial_input={"x": 1},
        correlation_id=uuid4(),
        started_at=datetime.now(UTC),
    )

    persisted = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]["last_error"]
    streamed = event_publish_mock.await_args.args[1].error_summary
    for value in (persisted, streamed):
        assert "sup3r-s3cret" not in value
        assert "agentive_app" not in value
        assert "[REDACTED]" in value
        assert "db:5432/agentive" in value  # the diagnostic part survives


@pytest.mark.asyncio
async def test_failed_run_reports_its_real_duration(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#14 — AC4. `total_duration_ms` was hardcoded to 0 on the failure
    path, so a run that burned nine minutes before dying was
    indistinguishable from one that failed instantly."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[],
        final_state={"node_outputs": {}, "node_metrics": {}},
        raise_after_updates=True,
        raise_exc=RuntimeError("boom"),
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._execute(
        uuid4(),
        workflow,
        {},
        initial_input={"x": 1},
        correlation_id=uuid4(),
        started_at=datetime.now(UTC) - timedelta(minutes=9),
    )

    metrics = workflow_run_repo.update_status.await_args.kwargs["metrics"]
    assert metrics["total_duration_ms"] >= 9 * 60 * 1000


# ─── Lot 3 — findings #21-26, #31 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_fanout_superstep_syncs_the_checkpoint_once(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#21 — a fan-out delivers every parallel node in ONE `update` dict. The
    per-node `aget_state` + UPDATE issued N identical round-trips writing N
    times the same snapshot; one read and one write per superstep is enough,
    and `step_completed` stays per node."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "b": {"node_metrics": {"b": {"duration_ms": 10}}},
                "c": {"node_metrics": {"c": {"duration_ms": 20}}},
            }
        ],
        final_state={"node_outputs": {"b": {}, "c": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    templates = {"b": SimpleNamespace(config={}), "c": SimpleNamespace(config={})}

    await service._drive_run(uuid4(), workflow, templates, {}, correlation_id=uuid4())

    workflow_run_repo.update_checkpoint.assert_awaited_once()
    step_events = [
        call.args[1]
        for call in event_publish_mock.await_args_list
        if call.args[0].endswith("step_completed")
    ]
    assert {event.node_id for event in step_events} == {"b", "c"}


@pytest.mark.asyncio
async def test_fanout_last_node_id_is_deterministic(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#21 (second half) — `last_node_id` used to hold whichever node dict
    iteration happened to visit last, and AC3's `resumed_from_node_id`
    inherits it."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[{"c": {}, "a": {}, "b": {}}],
        final_state={"node_outputs": {}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    templates = {name: SimpleNamespace(config={}) for name in ("a", "b", "c")}

    await service._drive_run(uuid4(), workflow, templates, {}, correlation_id=uuid4())

    checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert checkpoint["last_node_id"] == "c"


@pytest.mark.asyncio
async def test_langgraph_bookkeeping_keys_are_not_treated_as_nodes(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#22 — `stream_mode="updates"` also surfaces `__interrupt__` and
    friends. Treated as a node, each produced a phantom `step_completed` for
    a node that does not exist."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[{"__interrupt__": {}}, {"a": {"node_metrics": {"a": {"duration_ms": 5}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    step_nodes = [
        call.args[1].node_id
        for call in event_publish_mock.await_args_list
        if call.args[0].endswith("step_completed")
    ]
    assert step_nodes == ["a"]
    workflow_run_repo.update_checkpoint.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_node_is_marked_error_in_node_statuses(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#23 — a node that raises never commits, so it was absent from
    `node_outputs`; `dict.fromkeys(node_outputs, "success")` therefore
    described a FAILED run as one where everything succeeded, with the
    culprit appearing nowhere."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
        raise_after_updates=True,
        raise_exc=RuntimeError("node b exploded"),
        next_nodes=("b",),
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(uuid4(), workflow, {}, {}, correlation_id=uuid4())

    checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert checkpoint["node_statuses"] == {"a": "success", "b": "error"}


@pytest.mark.asyncio
async def test_missing_template_is_an_internal_error_not_a_404(
    event_publish_mock: AsyncMock,
) -> None:
    """#25 — this endpoint's 404 already means "unknown workflow_id". A
    template that vanished from a stored DAG is a server-side inconsistency;
    reporting it as 404 told the caller their valid id was wrong."""
    set_correlation_id(str(uuid4()))
    workflow_id = uuid4()
    service, workflow_repo, _wrrepo, template_repo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(
        status="active",
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []},
    )
    template_repo.get_by_id.return_value = None

    with pytest.raises(InternalError, match="no longer exists"):
        await service.start_run(workflow_id=workflow_id, run_input={})


@pytest.mark.asyncio
async def test_resumed_run_duration_excludes_the_crash_gap(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#26 — measuring a resumed run from the row's original `started_at`
    folded in the entire crash gap, so a run resumed a day later reported a
    24h `total_duration_ms` for a few seconds of work."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[], final_state={"node_outputs": {"a": {}}, "node_metrics": {}}
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    run = _workflow_run()
    run.started_at = datetime.now(UTC) - timedelta(days=1)
    run.checkpoint = {"last_node_id": "a"}
    workflow_run_repo.get_by_id.return_value = run

    await service._resume_run(run.id, workflow, {})

    metrics = workflow_run_repo.update_status.await_args.kwargs["metrics"]
    assert metrics["total_duration_ms"] < 60_000


@pytest.mark.asyncio
async def test_resume_restarts_from_start_when_no_checkpoint_exists(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#31 — the crash-before-first-checkpoint case AC3 names explicitly.
    `astream(None, ...)` on a virgin thread raises `EmptyInputError`, which
    `_execute` could only read as a run failure, so recovery buried the run
    in a terminal `error`. Verified against langgraph 1.1.8."""
    workflow = _workflow()
    captured: list[Any] = []

    compiled = _FakeCompiledGraph(updates=[], final_state={"node_outputs": {}, "node_metrics": {}})

    async def _aget_state(_config: Any) -> Any:
        return SimpleNamespace(values={}, next=())  # virgin thread

    async def _astream(state_input: Any, _config: Any, *, stream_mode: str) -> Any:
        captured.append(state_input)
        return
        yield  # pragma: no cover — makes this an async generator

    compiled.aget_state = _aget_state  # type: ignore[method-assign]
    compiled.astream = _astream  # type: ignore[method-assign]
    _patch_build_state_graph(monkeypatch, compiled)

    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    run = _workflow_run()
    run.checkpoint = {"task_input": {"seed": 7}}
    workflow_run_repo.get_by_id.return_value = run

    await service._resume_run(run.id, workflow, {})

    # Restarted from START with the preserved input, NOT `None`.
    assert captured[0] is not None
    assert captured[0]["task_input"] == {"seed": 7}


@pytest.mark.asyncio
async def test_resume_without_a_preserved_input_fails_explicitly(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#31 — a legacy row with no stored `task_input` genuinely cannot be
    recovered. It must say so, not surface LangGraph's opaque
    `EmptyInputError`."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(updates=[], final_state={})

    async def _aget_state(_config: Any) -> Any:
        return SimpleNamespace(values={}, next=())

    compiled.aget_state = _aget_state  # type: ignore[method-assign]
    _patch_build_state_graph(monkeypatch, compiled)

    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    run = _workflow_run()
    run.checkpoint = None
    workflow_run_repo.get_by_id.return_value = run

    await service._resume_run(run.id, workflow, {})

    checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert "no task_input was preserved" in checkpoint["last_error"]


# ─── Lot 4 — findings #38, #40 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_superstep_checkpoints_the_state_known_at_that_point(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """#40 — the payoff of a progressive test double.

    AC2 says the applicative summary is synced "après CHAQUE node terminé".
    With a double that returned the FINAL state from every `aget_state`, a
    checkpoint written after node `a` looked identical to one written after
    node `b`, so nothing distinguished a correct per-node sync from a single
    sync at the end — nor from the redundant repeated writes of finding #21.
    """
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[
            {"a": {"node_outputs": {"a": {"v": 1}}, "node_metrics": {"a": {"duration_ms": 1}}}},
            {"b": {"node_outputs": {"b": {"v": 2}}, "node_metrics": {"b": {"duration_ms": 2}}}},
        ],
        final_state={
            "node_outputs": {"a": {"v": 1}, "b": {"v": 2}},
            "node_metrics": {"a": {"duration_ms": 1}, "b": {"duration_ms": 2}},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    templates = {"a": SimpleNamespace(config={}), "b": SimpleNamespace(config={})}

    await service._drive_run(uuid4(), workflow, templates, {}, correlation_id=uuid4())

    checkpoints = [
        call.kwargs["checkpoint"] for call in workflow_run_repo.update_checkpoint.await_args_list
    ]
    assert len(checkpoints) == 2
    # After node a: only a is known. After node b: both.
    assert checkpoints[0]["last_node_id"] == "a"
    assert set(checkpoints[0]["node_statuses"]) == {"a"}
    assert checkpoints[1]["last_node_id"] == "b"
    assert set(checkpoints[1]["node_statuses"]) == {"a", "b"}


# ─── Intent gaps 3-5 ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_run_rechecks_llm_diversity(event_publish_mock: AsyncMock) -> None:
    """Gap 3 — `epics.md` assigns a SECOND diversity evaluation point to this
    story ("un contrôle à la création se périme"). `check_llm_diversity` was
    only ever called at creation, and `agent_templates` rows are mutable: a
    workflow validated as diverse can be running two identical models by the
    time anyone starts it."""
    set_correlation_id(str(uuid4()))
    same_model = "claude-3-5-sonnet-20241022"
    producer = _template(llm_model=same_model)
    controller = _template(archetype="controleur", llm_model=same_model)
    service, workflow_repo, workflow_run_repo, template_repo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(
        status="active",
        dag={
            "nodes": [
                {"node_id": "p", "agent_template_id": str(producer.id)},
                {"node_id": "c", "agent_template_id": str(controller.id)},
            ],
            "edges": [{"from_node_id": "p", "to_node_id": "c", "condition": None}],
        },
    )
    by_id = {producer.id: producer, controller.id: controller}
    template_repo.get_by_id = AsyncMock(side_effect=lambda tid, **_kw: by_id[tid])
    workflow_run_repo.create_in_session.return_value = _workflow_run()
    service._drive_run = AsyncMock()  # type: ignore[method-assign]

    response = await service.start_run(workflow_id=uuid4(), run_input={})

    # Non-blocking, exactly like AC4's creation-time check — a 201 with a
    # warning, never a refusal.
    assert response.status == "running"
    assert len(response.warnings) == 1
    assert response.warnings[0].controller_node_id == "c"
    assert response.warnings[0].producer_node_id == "p"


@pytest.mark.asyncio
async def test_start_run_reports_no_warning_when_models_differ(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    producer = _template(llm_model="claude-3-5-sonnet-20241022")
    controller = _template(archetype="controleur", llm_model="gpt-4o")
    service, workflow_repo, workflow_run_repo, template_repo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(
        status="active",
        dag={
            "nodes": [
                {"node_id": "p", "agent_template_id": str(producer.id)},
                {"node_id": "c", "agent_template_id": str(controller.id)},
            ],
            "edges": [{"from_node_id": "p", "to_node_id": "c", "condition": None}],
        },
    )
    by_id = {producer.id: producer, controller.id: controller}
    template_repo.get_by_id = AsyncMock(side_effect=lambda tid, **_kw: by_id[tid])
    workflow_run_repo.create_in_session.return_value = _workflow_run()
    service._drive_run = AsyncMock()  # type: ignore[method-assign]

    response = await service.start_run(workflow_id=uuid4(), run_input={})

    assert response.warnings == []


@pytest.mark.asyncio
async def test_completed_run_marks_nodes_that_never_executed_as_skipped(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """Gap 4 — a run whose branch conditions all evaluate false falls back to
    `[END]`, dropping the entire downstream while still reporting
    `completed`. Same status, same response shape, nothing saying a branch
    was skipped."""
    workflow = _workflow(
        dag={
            "nodes": [
                {"node_id": "a", "agent_template_id": str(uuid4())},
                {"node_id": "b", "agent_template_id": str(uuid4())},
                {"node_id": "c", "agent_template_id": str(uuid4())},
            ],
            "edges": [
                {"from_node_id": "a", "to_node_id": "b", "condition": "output.go == true"},
                {"from_node_id": "b", "to_node_id": "c", "condition": None},
            ],
        }
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {"go": False}}, "node_metrics": {"a": {}}}}],
        final_state={"node_outputs": {"a": {"go": False}}, "node_metrics": {"a": {}}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    # Still `completed` — the reachable graph genuinely finished.
    assert workflow_run_repo.update_status.await_args.kwargs["status"] == "completed"
    # But no longer silent about it.
    final_checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert final_checkpoint["skipped_nodes"] == ["b", "c"]
    assert final_checkpoint["node_statuses"] == {"a": "success", "b": "skipped", "c": "skipped"}


@pytest.mark.asyncio
async def test_fully_executed_run_records_no_skipped_nodes(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """The nominal path must not acquire a `skipped_nodes` key."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {"a": {}}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    for call in workflow_run_repo.update_checkpoint.await_args_list:
        assert "skipped_nodes" not in call.kwargs["checkpoint"]


def test_template_fingerprint_tracks_config_content_not_version() -> None:
    """Gap 5 — `update_template` mutates the row in place and only bumps
    `version` for a `system_prompt` change, so a model or temperature edit
    leaves both the id AND the version untouched. Only the content
    identifies what a node will actually execute."""
    from agentive_backend.features.workflow_engine.service import _template_fingerprints

    before = _template_fingerprints({"a": SimpleNamespace(config={"llm_model": "sonnet"})})
    after = _template_fingerprints({"a": SimpleNamespace(config={"llm_model": "haiku"})})
    same = _template_fingerprints({"a": SimpleNamespace(config={"llm_model": "sonnet"})})

    assert before != after
    assert before == same


@pytest.mark.asyncio
async def test_resume_reports_template_config_drift(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """Gap 5 — `start_run` snapshots templates, a resume reloads them. A
    template edited across the crash gap makes the second half of the run
    execute under a configuration the first half never saw, with nothing in
    the record to say so."""
    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[], final_state={"node_outputs": {"a": {}}, "node_metrics": {}}
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    run = _workflow_run()
    run.checkpoint = {"last_node_id": "a", "template_fingerprints": {"a": "stale-fingerprint"}}
    workflow_run_repo.get_by_id.return_value = run

    # structlog renders straight to stdout, so `caplog` sees nothing — assert
    # on the module logger itself.
    import agentive_backend.features.workflow_engine.service as svc_module

    warn_mock = MagicMock()
    monkeypatch.setattr(svc_module._log, "warning", warn_mock)

    await service._resume_run(run.id, workflow, {"a": SimpleNamespace(config={"x": 1})})

    events = [call.args[0] for call in warn_mock.call_args_list]
    assert "workflow_run_template_config_drift" in events
    drift_call = next(c for c in warn_mock.call_args_list if c.args[0].endswith("config_drift"))
    assert drift_call.kwargs["drifted_nodes"] == ["a"]


@pytest.mark.asyncio
async def test_resume_is_silent_when_templates_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """The nominal resume must not cry drift — otherwise the warning becomes
    noise and stops being read."""
    import agentive_backend.features.workflow_engine.service as svc_module

    workflow = _workflow()
    compiled = _FakeCompiledGraph(
        updates=[], final_state={"node_outputs": {"a": {}}, "node_metrics": {}}
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    templates = {"a": SimpleNamespace(config={"x": 1})}
    run = _workflow_run()
    run.checkpoint = {
        "last_node_id": "a",
        "template_fingerprints": svc_module._template_fingerprints(templates),
    }
    workflow_run_repo.get_by_id.return_value = run

    warn_mock = MagicMock()
    monkeypatch.setattr(svc_module._log, "warning", warn_mock)

    await service._resume_run(run.id, workflow, templates)

    events = [call.args[0] for call in warn_mock.call_args_list]
    assert "workflow_run_template_config_drift" not in events


# ═══════════════════════════════════════════════════════════════════════
# Story 4.3 T9.4/T9.6/T9.7/T11.7 — hybrid-routing aggregation & publication
# ═══════════════════════════════════════════════════════════════════════


def test_aggregate_metrics_includes_routing_counts_by_mode() -> None:
    import agentive_backend.features.workflow_engine.service as svc_module

    metrics = svc_module._aggregate_metrics(
        {},
        total_duration_ms=0,
        routing_decisions={
            "a": {"mode": "deterministic", "source": "dsl"},
            "b": {"mode": "deterministic", "source": "rules"},
            "c": {"mode": "llm_escalated", "source": "llm"},
        },
    )
    assert metrics["routing"]["deterministic"] == 2
    assert metrics["routing"]["llm_escalated"] == 1


def test_aggregate_metrics_routing_defaults_to_zero_counts_when_absent() -> None:
    """A workflow with no decision points reports explicit zeros, never a
    missing key (Dev Notes § Définition du point de décision)."""
    import agentive_backend.features.workflow_engine.service as svc_module

    metrics = svc_module._aggregate_metrics({}, total_duration_ms=0)
    assert metrics["routing"]["deterministic"] == 0
    assert metrics["routing"]["llm_escalated"] == 0


@pytest.mark.asyncio
async def test_completed_run_persists_routing_counts_in_metrics(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "a": {
                    "node_outputs": {"a": {"status": "ok"}},
                    "node_metrics": {"a": {}},
                    "routing_decisions": {
                        "a": {"mode": "deterministic", "source": "dsl", "targets": []}
                    },
                }
            }
        ],
        final_state={
            "node_outputs": {"a": {"status": "ok"}},
            "node_metrics": {"a": {}},
            "routing_decisions": {"a": {"mode": "deterministic", "source": "dsl", "targets": []}},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    metrics = workflow_run_repo.update_status.await_args.kwargs["metrics"]
    assert metrics["routing"]["deterministic"] == 1
    assert metrics["routing"]["llm_escalated"] == 0
    checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert checkpoint["routing_decisions"]["a"]["mode"] == "deterministic"


@pytest.mark.asyncio
async def test_failed_run_still_aggregates_partial_routing_counts(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """AC4 mirrors 4.2 AC4 — partial routing counts survive a failed run,
    exactly like partial token/cost metrics already do."""
    workflow = _workflow(
        dag={
            "nodes": [
                {"node_id": "a", "agent_template_id": str(uuid4())},
                {"node_id": "b", "agent_template_id": str(uuid4())},
            ],
            "edges": [],
        }
    )
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "a": {
                    "node_outputs": {"a": {}},
                    "node_metrics": {"a": {}},
                    "routing_decisions": {
                        "a": {"mode": "deterministic", "source": "dsl", "targets": []}
                    },
                }
            }
        ],
        final_state={
            "node_outputs": {"a": {}},
            "node_metrics": {"a": {}},
            "routing_decisions": {"a": {"mode": "deterministic", "source": "dsl", "targets": []}},
        },
        raise_after_updates=True,
        next_nodes=("b",),
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(),
        workflow,
        {"a": SimpleNamespace(config={}), "b": SimpleNamespace(config={})},
        {},
        correlation_id=uuid4(),
    )

    assert workflow_run_repo.update_status.await_args.kwargs["status"] == "error"
    metrics = workflow_run_repo.update_status.await_args.kwargs["metrics"]
    assert metrics["routing"]["deterministic"] == 1
    assert metrics["routing"]["llm_escalated"] == 0


@pytest.mark.asyncio
async def test_escalated_decision_publishes_routing_escalated_event(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    workflow = _workflow(
        dag={
            "nodes": [
                {"node_id": "a", "agent_template_id": str(uuid4())},
                {"node_id": "b", "agent_template_id": str(uuid4())},
            ],
            "edges": [{"from_node_id": "a", "to_node_id": "b", "condition": "output.x == 1"}],
        }
    )
    decision = {
        "mode": "llm_escalated",
        "source": "llm",
        "targets": ["b"],
        "confidence": 0.0,
        "rule_id": None,
        "reason": "best guess",
        "llm_model": "claude-haiku-4-5",
        "llm_latency_ms": 42,
    }
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "a": {
                    "node_outputs": {"a": {"x": 2}},
                    "node_metrics": {"a": {}},
                    "routing_decisions": {"a": decision},
                }
            },
            {"b": {"node_outputs": {"b": {}}, "node_metrics": {"b": {}}}},
        ],
        final_state={
            "node_outputs": {"a": {"x": 2}, "b": {}},
            "node_metrics": {"a": {}, "b": {}},
            "routing_decisions": {"a": decision},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, _workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(),
        workflow,
        {"a": SimpleNamespace(config={}), "b": SimpleNamespace(config={})},
        {},
        correlation_id=uuid4(),
    )

    published_types = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.routing_escalated" in published_types
    escalation_call = next(
        call
        for call in event_publish_mock.await_args_list
        if call.args[0] == "workflow_engine.workflow_run.routing_escalated"
    )
    event_payload = escalation_call.args[1]
    assert event_payload.node_id == "a"
    assert event_payload.decision_target == ["b"]
    assert event_payload.candidates == ["b"]
    assert event_payload.llm_latency_ms == 42
    assert "x" not in event_payload.context  # own_output never in the payload (T7.3)


@pytest.mark.asyncio
async def test_escalated_decision_published_only_once_per_node(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """T9.7 — defends against a node_id appearing more than once in the
    `update` stream within a single execution (idempotence guard)."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    decision = {
        "mode": "llm_escalated",
        "source": "llm",
        "targets": [],
        "confidence": 0.0,
        "rule_id": None,
        "reason": "x",
        "llm_model": "claude-haiku-4-5",
        "llm_latency_ms": 5,
    }
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "a": {
                    "node_outputs": {"a": {}},
                    "node_metrics": {"a": {}},
                    "routing_decisions": {"a": decision},
                }
            },
            {
                "a": {
                    "node_outputs": {"a": {}},
                    "node_metrics": {"a": {}},
                    "routing_decisions": {"a": decision},
                }
            },
        ],
        final_state={
            "node_outputs": {"a": {}},
            "node_metrics": {"a": {}},
            "routing_decisions": {"a": decision},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, _workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    published_types = [
        call.args[0]
        for call in event_publish_mock.await_args_list
        if call.args[0] == "workflow_engine.workflow_run.routing_escalated"
    ]
    assert len(published_types) == 1


@pytest.mark.asyncio
async def test_routing_metrics_recorded_once_per_node_like_the_event(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """The Prometheus counter sat OUTSIDE the guard that deduplicates the
    event, so the same decision could increment it twice while publishing
    once — making the counters disagree with `metrics["routing"]` and
    `/routing-stats`, both computed from the state and therefore counting
    each decision exactly once."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    decision = {
        "mode": "llm_escalated",
        "source": "llm",
        "targets": [],
        "confidence": 0.0,
        "rule_id": None,
        "reason": "x",
        "llm_model": "claude-haiku-4-5",
        "llm_latency_ms": 5,
    }
    update = {
        "a": {
            "node_outputs": {"a": {}},
            "node_metrics": {"a": {}},
            "routing_decisions": {"a": decision},
        }
    }
    compiled = _FakeCompiledGraph(
        updates=[update, update],
        final_state={
            "node_outputs": {"a": {}},
            "node_metrics": {"a": {}},
            "routing_decisions": {"a": decision},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, _workflow_run_repo, _trepo = _make_execution_service()
    record_mock = MagicMock()
    monkeypatch.setattr(service, "_record_routing_metrics_safely", record_mock)

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    assert record_mock.call_count == 1


@pytest.mark.asyncio
async def test_resume_does_not_republish_decisions_from_the_restored_checkpoint(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """T9.7 literally — the guard has to hold "for this RUN", not for this
    execution. Seeded empty on every resume, it re-published (and re-counted)
    every escalation the run had already taken before the crash, permanently
    over-representing resumed runs in the raw material of innovation #1.
    """
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    decision = {
        "mode": "llm_escalated",
        "source": "llm",
        "targets": [],
        "confidence": 0.0,
        "rule_id": None,
        "reason": "decided before the crash",
        "llm_model": "claude-haiku-4-5",
        "llm_latency_ms": 5,
    }
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "a": {
                    "node_outputs": {"a": {}},
                    "node_metrics": {"a": {}},
                    "routing_decisions": {"a": decision},
                }
            }
        ],
        final_state={
            "node_outputs": {"a": {}},
            "node_metrics": {"a": {}},
            "routing_decisions": {"a": decision},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    run = _workflow_run(workflow_id=workflow.id)
    # The applicative checkpoint `_sync_checkpoint` left behind before the crash.
    run.checkpoint = {"routing_decisions": {"a": decision}}
    workflow_run_repo.get_by_id.return_value = run

    await service._resume_run(run.id, workflow, {"a": SimpleNamespace(config={})})

    republished = [
        call.args[0]
        for call in event_publish_mock.await_args_list
        if call.args[0] == "workflow_engine.workflow_run.routing_escalated"
    ]
    assert republished == []


@pytest.mark.asyncio
async def test_routing_settings_and_rules_are_forwarded_to_build_state_graph(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    import agentive_backend.features.workflow_engine.service as svc_module
    from agentive_backend.features.workflow_engine.domain.routing_rules import RoutingRule
    from agentive_backend.shared.config import settings

    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {"a": {}}},
    )
    captured: dict[str, Any] = {}

    def _fake_build(*args: Any, **kwargs: Any) -> _FakeGraphBuilder:
        captured["rules"] = kwargs.get("rules")
        captured["routing_settings"] = kwargs.get("routing_settings")
        return _FakeGraphBuilder(compiled)

    monkeypatch.setattr(svc_module, "build_state_graph", _fake_build)

    rule = RoutingRule(
        rule_id="r1",
        description="d",
        when=(),
        verdict="terminate",
        base_confidence=0.9,
        penalties=(),
    )
    service, _wrepo, _workflow_run_repo, _trepo = _make_execution_service()
    service._routing_rules = (rule,)  # type: ignore[attr-defined]

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    assert captured["rules"] == (rule,)
    assert captured["routing_settings"] is not None
    assert captured["routing_settings"].threshold == pytest.approx(
        settings.routing_confidence_threshold
    )


# ─── IG3 — the node's paid work survives a failed routing decision ────────


@pytest.mark.asyncio
async def test_failed_routing_decision_persists_the_node_work_it_carried(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """LangGraph discards the state update of a node that raises, so a node
    whose ROUTING decision failed left no trace at all — the run ended
    `error` with no record of a node that had executed, and its already-paid
    tokens missing from the run's totals.

    The run must still fail (posture unchanged) and `last_error` must still
    describe the REAL cause, not the carrier wrapper.
    """
    from agentive_backend.features.workflow_engine.domain.routing_rules import (
        RoutingEscalationError,
    )
    from agentive_backend.features.workflow_engine.engine.graph_builder import (
        RoutingDecisionFailedError,
    )

    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    cause = RoutingEscalationError(
        "escalation response is not valid JSON", failure_reason="unparsable"
    )
    carrier = RoutingDecisionFailedError(
        node_id="a",
        node_update={
            "node_outputs": {"a": {"status": "unknown"}},
            "node_metrics": {"a": {"input_tokens": 11, "output_tokens": 3, "cost_usd": "0.002"}},
        },
    )
    carrier.__cause__ = cause

    compiled = _FakeCompiledGraph(
        updates=[],
        # The node's update never reached the state — that is the whole point.
        final_state={"node_outputs": {}, "node_metrics": {}, "routing_decisions": {}},
        raise_after_updates=True,
        raise_exc=carrier,
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    run_id = uuid4()
    await service._drive_run(
        run_id, workflow, {"a": SimpleNamespace(config={})}, {}, correlation_id=uuid4()
    )

    status_call = workflow_run_repo.update_status.await_args
    assert status_call.kwargs["status"] == "error"

    metrics = status_call.kwargs["metrics"]
    # The work the run paid for is accounted, not silently dropped.
    assert metrics["total_tokens"] == {"input": 11, "output": 3}
    assert metrics["total_cost_usd"] == "0.002"
    assert metrics["per_node"]["a"]["cost_usd"] == "0.002"

    # The node's STEP failed, so its status is `error` — its own LLM call
    # succeeded but the decision that completes the step did not. What IG3
    # fixes is that its output is no longer absent from the checkpoint: an
    # operator can now see what the node produced before the step died.
    checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert checkpoint["node_statuses"]["a"] == "error"
    assert checkpoint["last_node_id"] == "a"
    assert "a" in checkpoint["node_outputs_preview"]
    # And the recorded error is the CAUSE, not the carrier's own message.
    assert "routing decision failed" not in checkpoint["last_error"]
    assert "not valid JSON" in checkpoint["last_error"]
