"""Unit tests — :class:`WorkflowService.create_workflow` (Story 4.1 T8.3).

Mock-driven (``AgentTemplateRepo``/``WorkflowRepo`` mocked) — mirrors
``tests/unit/agent_registry/test_instantiate_template_service.py``. The
Postgres-real integration path (atomicity, outbox event persisted) lives in
``tests/integration/workflow_engine/test_create_workflow_e2e.py`` (T8.6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from agentive_backend.features.workflow_engine.schemas import (
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)
from agentive_backend.features.workflow_engine.service import WorkflowService
from agentive_backend.shared.exceptions import ValidationError


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
