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
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from agentive_backend.features.workflow_engine.domain.mise_en_place import (
    CheckResult,
    MiseEnPlaceReport,
    build_report,
)
from agentive_backend.features.workflow_engine.schemas import (
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)
from agentive_backend.features.workflow_engine.service import (
    WorkflowExecutionService,
    WorkflowService,
    _already_executed_node_ids,
    _request_fingerprint,
)
from agentive_backend.shared.contracts.events import WorkflowRunMiseEnPlaceRefusedEvent
from agentive_backend.shared.correlation import set_correlation_id
from agentive_backend.shared.exceptions import (
    BusinessRuleError,
    ConflictError,
    DependencyError,
    InternalError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)


@pytest.fixture
def event_publish_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """Patch ``service.publish`` + ``service.notify_best_effort`` so we don't touch the bus."""
    pub_mock = AsyncMock(return_value=uuid4())
    notify_mock = AsyncMock(return_value=None)
    import agentive_backend.features.workflow_engine.service as svc_module

    monkeypatch.setattr(svc_module, "publish", pub_mock)
    monkeypatch.setattr(svc_module, "notify_best_effort", notify_mock)
    yield pub_mock


@pytest.fixture
def event_publish_and_commit_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    """Patch ``service.publish_and_commit`` — the refused-launch audit trace
    (Story 4.5 AC2, review BS2) owns its session and commits on its own, so
    it does NOT go through ``publish``. Kept as a separate fixture on
    purpose: every `assert_not_awaited()` on ``event_publish_mock`` keeps
    meaning "no ``started`` event was published"."""
    mock = AsyncMock(return_value=uuid4())
    import agentive_backend.features.workflow_engine.service as svc_module

    monkeypatch.setattr(svc_module, "publish_and_commit", mock)
    yield mock


def _template(
    *,
    archetype: str = "producteur",
    name: str | None = None,
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
    template_id = uuid4()
    # Story 5.1 — `name` manquait à ce faux alors que `agent_templates.name`
    # est NOT NULL : aucun template réel n'en est dépourvu. L'accusé de
    # réception (AC2) le lit pour composer « Je mobilise […] », et c'est ce
    # qui a rendu le manque visible.
    return SimpleNamespace(
        id=template_id,
        name=name if name is not None else f"tpl-{template_id}",
        archetype=archetype,
        config=config,
    )


def _make_service(
    templates_by_id: dict[UUID, SimpleNamespace],
) -> tuple[WorkflowService, AsyncMock, AsyncMock]:
    """Returns (service, workflow_repo, template_repo)."""
    # Story 4.8 review P3 — a FRESH session object per `with_tenant` entry,
    # and a record of how many times it was entered. The harness used to
    # yield one shared `session_mock` forever, which made
    # `assert resolve_session is insert_session` (the assertion advertised as
    # the proof of AC2's single transactional window) pass just as happily
    # against two separate transactions. Identity only proves anything if
    # distinct transactions have distinct identities.
    opened_sessions: list[AsyncMock] = []
    # Story 4.14 T2 — the kwarg `create_workflow` passes, RECORDED rather than
    # discarded. The mock originally did `del lock_timeout_ms`, which made the
    # wiring untestable: deleting `lock_timeout_ms=` from the service left the
    # whole suite green while production reverted to an unbounded wait (review
    # 4.14, finding 2). The e2e test cannot cover this either — it drives the
    # repo directly with its own literal, bypassing the service and the
    # setting.
    lock_timeouts: list[int | None] = []

    @asynccontextmanager
    async def _with_tenant(
        _tenant_id: Any, *, lock_timeout_ms: int | None = None
    ) -> AsyncIterator[AsyncMock]:
        lock_timeouts.append(lock_timeout_ms)
        session_mock = AsyncMock()
        session_mock.flush = AsyncMock()
        session_mock.refresh = AsyncMock()
        session_mock.add = MagicMock()
        opened_sessions.append(session_mock)
        yield session_mock

    workflow_repo = AsyncMock()
    workflow_repo.with_tenant = _with_tenant
    # Exposed so tests can assert on the NUMBER of transactions opened, which
    # is the other half of the same property.
    workflow_repo.opened_sessions = opened_sessions
    workflow_repo.lock_timeouts = lock_timeouts

    async def _mock_create(
        _session: object,
        *,
        name: str,
        dag: dict[str, Any],
        version: int = 1,
        status: str = "active",
        tenant_id: Any | None = None,
        request_fingerprint: str | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid4(),
            name=name,
            dag=dag,
            version=version,
            status=status,
            tenant_id=tenant_id,
            request_fingerprint=request_fingerprint,
            created_at=datetime.now(UTC),
        )

    workflow_repo.create_in_session = AsyncMock(side_effect=_mock_create)

    template_repo = AsyncMock()

    async def _get_by_id(template_id: UUID, *, tenant_id: Any | None = None) -> Any:
        return templates_by_id.get(template_id)

    template_repo.get_by_id = AsyncMock(side_effect=_get_by_id)

    # Story 4.8 T6.1 — `create_workflow` resolves the whole DAG in ONE locked
    # batch query instead of N `get_by_id` calls. Both are stubbed: `get_by_id`
    # still serves the execution-path helpers exercised elsewhere in this file.
    async def _list_by_ids_in_session(
        _session: object, template_ids: Any, *, lock: bool = False
    ) -> dict[UUID, Any]:
        return {tid: templates_by_id[tid] for tid in set(template_ids) if tid in templates_by_id}

    template_repo.list_by_ids_in_session = AsyncMock(side_effect=_list_by_ids_in_session)

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


# ─── Story 4.8 AC1 — idempotence on replay ─────────────────────────────


@pytest.mark.asyncio
async def test_create_workflow_replay_returns_the_original_without_publishing(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — a fingerprint collision is a replay, not a failure: return the
    workflow the FIRST request created, and publish nothing."""
    tpl = _template()
    service, wrepo, _trepo = _make_service({tpl.id: tpl})
    original = SimpleNamespace(id=uuid4(), version=1)
    wrepo.create_in_session = AsyncMock(side_effect=ConflictError(detail="already created"))
    wrepo.get_by_request_fingerprint = AsyncMock(return_value=original)

    response = await service.create_workflow(
        name="replayed",
        nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
        edges=[],
    )

    assert response.idempotent_replay is True
    assert response.workflow_id == original.id
    assert response.version == original.version
    # No second row is the repo's business; no second EVENT is this method's,
    # and it holds because `publish` sits after the flush that collided.
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_nominal_path_is_not_flagged_as_a_replay(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — the flag must be False on creation, not merely absent: a client
    branching on it would otherwise read a missing key as a replay."""
    tpl = _template()
    service, _wrepo, _trepo = _make_service({tpl.id: tpl})

    response = await service.create_workflow(
        name="fresh",
        nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
        edges=[],
    )

    assert response.idempotent_replay is False
    event_publish_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_workflow_replay_whose_original_vanished_raises_409(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — the row collided with is gone between the failed INSERT and the
    re-read. Surface the 409; never loop retrying, which would race the same
    deletion forever."""
    tpl = _template()
    service, wrepo, _trepo = _make_service({tpl.id: tpl})
    wrepo.create_in_session = AsyncMock(side_effect=ConflictError(detail="already created"))
    wrepo.get_by_request_fingerprint = AsyncMock(return_value=None)

    with pytest.raises(ConflictError):
        await service.create_workflow(
            name="ghost",
            nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
            edges=[],
        )

    wrepo.create_in_session.assert_awaited_once()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_workflow_passes_a_stable_fingerprint_to_the_insert(
    event_publish_mock: AsyncMock,
) -> None:
    """AC1 — the value persisted is the fingerprint of the submitted body, and
    two identical submissions produce the same one."""
    tpl = _template()
    service, wrepo, _trepo = _make_service({tpl.id: tpl})

    kwargs = {
        "name": "stable",
        "nodes": [WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
        "edges": [],
    }
    await service.create_workflow(**kwargs)  # type: ignore[arg-type]
    first = wrepo.create_in_session.await_args.kwargs["request_fingerprint"]
    await service.create_workflow(**kwargs)  # type: ignore[arg-type]
    second = wrepo.create_in_session.await_args.kwargs["request_fingerprint"]

    assert first == second
    assert first == _request_fingerprint(
        "stable",
        {"nodes": [{"node_id": "a", "agent_template_id": str(tpl.id)}], "edges": []},
    )


# ─── Story 4.8 — `_request_fingerprint` ────────────────────────────────


def test_request_fingerprint_is_a_full_length_deterministic_sha256() -> None:
    """Never truncated, unlike `_template_fingerprints`: this one backs a
    unique index, and a shortened hash manufactures collisions."""
    payload = {"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}

    digest = _request_fingerprint("wf", payload)

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest == _request_fingerprint("wf", payload)


def test_request_fingerprint_matches_its_frozen_golden_vector() -> None:
    """Review P10 — the ONE test that protects the fingerprints already in
    the database.

    Every other test here compares `_request_fingerprint` against itself, so
    they all stay green through a change that alters the hash: the production
    value moves, the expected value moves with it, nothing fails — and every
    fingerprint persisted before the change silently stops matching. The
    unique index becomes dead weight and replays start creating twins, with
    no signal anywhere.

    This vector is a LITERAL. If it fails, the hash function or the
    `dag_payload` projection changed, and that is a migration problem (every
    stored `workflows.request_fingerprint` is now stale), not a test to
    update. Re-freeze it only together with a backfill plan.
    """
    payload = {
        "nodes": [
            {"node_id": "ingest", "agent_template_id": "11111111-1111-4111-8111-111111111111"},
            {"node_id": "review", "agent_template_id": "22222222-2222-4222-8222-222222222222"},
        ],
        "edges": [{"from_node_id": "ingest", "to_node_id": "review", "condition": None}],
    }

    assert (
        _request_fingerprint("golden-pipeline", payload)
        == "f399d9188b1cc281f46ad364f5f5e522581443c8add9bf8e493231d419194230"
    )


def test_request_fingerprint_changes_when_the_name_changes() -> None:
    payload = {"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}

    assert _request_fingerprint("wf-a", payload) != _request_fingerprint("wf-b", payload)


def test_request_fingerprint_changes_when_the_dag_changes() -> None:
    tid = str(uuid4())
    one = {"nodes": [{"node_id": "a", "agent_template_id": tid}], "edges": []}
    two = {
        "nodes": [
            {"node_id": "a", "agent_template_id": tid},
            {"node_id": "b", "agent_template_id": tid},
        ],
        "edges": [],
    }

    assert _request_fingerprint("wf", one) != _request_fingerprint("wf", two)


def test_request_fingerprint_ignores_dict_key_insertion_order() -> None:
    """`sort_keys=True` is what makes this true — the guard against a
    fingerprint that depends on how the payload dict happened to be built."""
    tid = str(uuid4())
    forward = {"nodes": [{"node_id": "a", "agent_template_id": tid}], "edges": []}
    reversed_keys = {"edges": [], "nodes": [{"agent_template_id": tid, "node_id": "a"}]}

    assert _request_fingerprint("wf", forward) == _request_fingerprint("wf", reversed_keys)


def test_request_fingerprint_distinguishes_reordered_nodes() -> None:
    """Deliberate NON-normalisation (AC1): two semantically equivalent bodies
    listing their nodes in a different order are two different requests.
    Canonicalising the DAG would turn idempotence into "isomorphic DAGs are
    the same workflow", a much stronger rule no AC asks for."""
    a = {"node_id": "a", "agent_template_id": str(uuid4())}
    b = {"node_id": "b", "agent_template_id": str(uuid4())}

    assert _request_fingerprint("wf", {"nodes": [a, b], "edges": []}) != _request_fingerprint(
        "wf", {"nodes": [b, a], "edges": []}
    )


def test_request_fingerprint_accepts_a_non_ascii_name() -> None:
    """`ensure_ascii=False` + explicit `.encode()` — a unicode name must hash,
    not raise."""
    payload: dict[str, Any] = {"nodes": [], "edges": []}

    assert len(_request_fingerprint("workflow-résumé-日本", payload)) == 64


# ─── Story 4.8 AC2 — one transactional window ──────────────────────────


@pytest.mark.asyncio
async def test_create_workflow_resolves_templates_inside_the_insert_transaction(
    event_publish_mock: AsyncMock,
) -> None:
    """AC2 — the window is single: exactly ONE transaction is opened, and the
    session handed to the template resolution is the same object the INSERT
    is written through.

    Both halves are needed, and the harness has to cooperate for either to
    mean anything (review P3): `_make_service` now yields a distinct session
    per `with_tenant` entry, so `is` genuinely discriminates, and it records
    the entries, so a refactor that resolved in one transaction and inserted
    in a second fails on the count even if it somehow passed on identity."""
    tpl = _template()
    service, wrepo, trepo = _make_service({tpl.id: tpl})

    await service.create_workflow(
        name="one-window",
        nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
        edges=[],
    )

    assert len(wrepo.opened_sessions) == 1, (
        f"AC2 requires ONE transaction; {len(wrepo.opened_sessions)} were opened"
    )
    resolve_session = trepo.list_by_ids_in_session.await_args.args[0]
    insert_session = wrepo.create_in_session.await_args.args[0]
    assert resolve_session is insert_session
    # ...and it is locked, so a concurrent `PUT /agents/templates/{id}` waits
    # for the commit instead of slipping between validation and INSERT.
    assert trepo.list_by_ids_in_session.await_args.kwargs["lock"] is True


@pytest.mark.asyncio
async def test_create_workflow_bounds_its_transaction_with_the_configured_lock_timeout(
    event_publish_mock: AsyncMock,
) -> None:
    """Story 4.14 AC2 — the WIRING, which nothing pinned before (review 4.14,
    finding 2).

    The e2e companion drives `repo.with_tenant(None, lock_timeout_ms=200)`
    directly with its own literal, so it proves the repo mechanism and says
    nothing about whether the service ever asks for it. Deleting
    `lock_timeout_ms=lock_timeout_ms` from `create_workflow`, or getting the
    unit wrong (passing seconds where milliseconds are expected), left the
    whole suite green.

    Asserting the exact derived value rather than "not None" is deliberate:
    a seconds/milliseconds mix-up yields `5` instead of `5000` — a bound
    1000x tighter that aborts every contended create instantly — and "not
    None" cannot tell the two apart.
    """
    from agentive_backend.shared.config import settings

    tpl = _template()
    service, wrepo, _trepo = _make_service({tpl.id: tpl})

    await service.create_workflow(
        name="bounded-window",
        nodes=[WorkflowNodeRequest(node_id="a", agent_template_id=tpl.id)],
        edges=[],
    )

    expected_ms = max(1, round(settings.workflow_create_lock_timeout_s * 1000))
    assert wrepo.lock_timeouts == [expected_ms], (
        "create_workflow must bound its transaction with "
        f"{expected_ms}ms (derived from "
        "settings.workflow_create_lock_timeout_s); got "
        f"{wrepo.lock_timeouts}"
    )
    # And the derived value must be one Postgres reads as a bound at all:
    # `lock_timeout = 0` means DISABLED, so a floor of 1ms is part of the
    # contract, not an implementation detail (review 4.14, finding 4).
    assert expected_ms >= 1


@pytest.mark.asyncio
async def test_create_workflow_reports_the_cycle_when_a_template_is_also_unknown(
    event_publish_mock: AsyncMock,
) -> None:
    """AC2 — precedence change, locked deliberately. Structural checks moved
    out of the transaction (they read nothing), so the cycle is now found
    first. Both remain 422s; only which one wins changed. This test exists so
    a future rewrite cannot flip it back unnoticed."""
    service, wrepo, trepo = _make_service({})  # no template resolves

    with pytest.raises(ValidationError, match="cycle"):
        await service.create_workflow(
            name="cyclic-and-unknown",
            nodes=[
                WorkflowNodeRequest(node_id="a", agent_template_id=uuid4()),
                WorkflowNodeRequest(node_id="b", agent_template_id=uuid4()),
            ],
            edges=[
                WorkflowEdgeRequest(from_node_id="a", to_node_id="b"),
                WorkflowEdgeRequest(from_node_id="b", to_node_id="a"),
            ],
        )

    # Nothing was read: the structural pass never opens a transaction.
    trepo.list_by_ids_in_session.assert_not_awaited()
    wrepo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


# ─── Story 4.8 AC3 — batch resolution at creation ──────────────────────


@pytest.mark.asyncio
async def test_create_workflow_resolves_all_nodes_in_a_single_query(
    event_publish_mock: AsyncMock,
) -> None:
    """AC3 — one `IN` query for the whole DAG, whatever the node count."""
    a, b, c = _template(), _template(), _template()
    service, _wrepo, trepo = _make_service({a.id: a, b.id: b, c.id: c})

    await service.create_workflow(
        name="three-nodes",
        nodes=[
            WorkflowNodeRequest(node_id="x", agent_template_id=a.id),
            WorkflowNodeRequest(node_id="y", agent_template_id=b.id),
            WorkflowNodeRequest(node_id="z", agent_template_id=c.id),
        ],
        edges=[],
    )

    trepo.list_by_ids_in_session.assert_awaited_once()
    assert list(trepo.list_by_ids_in_session.await_args.args[1]) == [a.id, b.id, c.id]


@pytest.mark.asyncio
async def test_create_workflow_two_nodes_sharing_a_template_get_the_same_row(
    event_publish_mock: AsyncMock,
) -> None:
    """AC3 — a template referenced twice is still resolved once, and both
    nodes see the same object (the map is keyed by id, not by node)."""
    shared = _template(archetype="controleur", llm_model="claude-3-5-sonnet-20241022")
    producer = _template(archetype="producteur", llm_model="claude-3-5-sonnet-20241022")
    service, _wrepo, trepo = _make_service({shared.id: shared, producer.id: producer})

    response = await service.create_workflow(
        name="shared-template",
        nodes=[
            WorkflowNodeRequest(node_id="p", agent_template_id=producer.id),
            WorkflowNodeRequest(node_id="c1", agent_template_id=shared.id),
            WorkflowNodeRequest(node_id="c2", agent_template_id=shared.id),
        ],
        edges=[
            WorkflowEdgeRequest(from_node_id="p", to_node_id="c1"),
            WorkflowEdgeRequest(from_node_id="p", to_node_id="c2"),
        ],
    )

    trepo.list_by_ids_in_session.assert_awaited_once()
    # Both controller nodes resolved to the same row, so both raise the
    # identical-LLM warning against the shared producer.
    assert {w.controller_node_id for w in response.warnings} == {"c1", "c2"}


@pytest.mark.asyncio
async def test_create_workflow_reports_the_first_unknown_template_in_declaration_order(
    event_publish_mock: AsyncMock,
) -> None:
    """AC3 — the batch changed the QUERY, not the error contract. Iterating
    the resolved map instead would make the reported node depend on
    Postgres' return order."""
    known = _template()
    missing_first, missing_second = uuid4(), uuid4()
    service, _wrepo, _trepo = _make_service({known.id: known})

    with pytest.raises(ValidationError) as exc_info:
        await service.create_workflow(
            name="two-missing",
            nodes=[
                WorkflowNodeRequest(node_id="ok", agent_template_id=known.id),
                WorkflowNodeRequest(node_id="first-bad", agent_template_id=missing_first),
                WorkflowNodeRequest(node_id="second-bad", agent_template_id=missing_second),
            ],
            edges=[],
        )

    assert exc_info.value.context["node_id"] == "first-bad"
    assert exc_info.value.context["agent_template_id"] == str(missing_first)


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
        restored_state: dict[str, Any] | None = None,
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
        # `restored_state` models a RESUMED run: LangGraph's checkpoint
        # already holds the nodes a previous process executed, so the very
        # first `aget_state` — before any superstep of this segment — is not
        # empty. Without it the double could only ever describe a run
        # starting from scratch, which is precisely the case where the
        # pre-loop control check has nothing to account for.
        seed = restored_state or {}
        self._progressive: dict[str, Any] = {
            "node_outputs": dict(seed.get("node_outputs") or {}),
            "node_metrics": dict(seed.get("node_metrics") or {}),
            "routing_decisions": dict(seed.get("routing_decisions") or {}),
        }
        self._stream_finished = False
        self.stream_closed = False

    async def astream(
        self, _state_input: Any, _config: Any, *, stream_mode: str
    ) -> AsyncIterator[dict[str, Any]]:
        assert stream_mode == "updates"
        try:
            for update in self._updates:
                for node_update in update.values():
                    for key in ("node_outputs", "node_metrics", "routing_decisions"):
                        self._progressive[key].update((node_update or {}).get(key) or {})
                yield update
        except GeneratorExit:
            # Raised at the `yield` by an explicit `aclose()` — and by
            # nothing else, so this records a deliberate close rather than a
            # normal exhaustion. `GeneratorExit` is a `BaseException`, hence
            # the explicit clause.
            self.stream_closed = True
            raise
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
    metrics: dict[str, Any] | None = None,
    control_signal: str | None = None,
    control_requested_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workflow_id=workflow_id or uuid4(),
        correlation_id=correlation_id or uuid4(),
        started_at=datetime.now(UTC),
        checkpoint=None,
        status=status,
        # The column carries `server_default="{}"`, so a real row NEVER has
        # this attribute missing — the double used to, which made it a
        # weaker stand-in than the thing it stands in for.
        metrics=metrics if metrics is not None else {},
        control_signal=control_signal,
        # Story 4.9 AC6/T6.1 — mirrors `control_signal`: a real row never
        # carries one without the other (`clear_control` nulls both
        # together, `request_control_in_session` stamps both together).
        control_requested_at=control_requested_at,
    )


def _all_passed_mise_en_place_report() -> MiseEnPlaceReport:
    """The default Mise en Place outcome for tests that don't care about
    Story 4.5 — every check passes, so `start_run`'s pre-existing behavior
    (create the row, publish `started`, no bypass event) is unaffected."""
    return build_report(
        [
            CheckResult(code=code, passed=True, detail="ok")  # type: ignore[arg-type]
            for code in (
                "mcp_tools_reachable",
                "memory_namespaces_accessible",
                "budget_available",
                "llm_providers_configured",
            )
        ]
    )


def _make_execution_service() -> tuple[WorkflowExecutionService, AsyncMock, AsyncMock, AsyncMock]:
    """Returns (service, workflow_repo, workflow_run_repo, template_repo).

    Story 4.5 — the constructed service's ``_mise_en_place_service`` is an
    ``AsyncMock`` whose ``run_checks`` returns an all-passing report by
    default (every pre-existing test in this module exercises `start_run`
    without caring about Mise en Place). Tests that DO care reach it via
    ``service._mise_en_place_service`` — same convention already used for
    ``service._drive_run`` elsewhere in this file — rather than widening
    this helper's return tuple and touching every one of its ~40 call sites.
    """
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
    # Story 4.9 AC2 — the concurrency-cap check's count, both the early
    # (self-managed) and authoritative (in-session) variants. `0` keeps
    # every pre-existing test in this module under the default cap
    # (`AGENTIVE_WORKFLOW_MAX_CONCURRENT_RUNS`, default 20) — tests of the
    # cap itself override these explicitly.
    workflow_run_repo.count_running = AsyncMock(return_value=0)
    workflow_run_repo.count_running_in_session = AsyncMock(return_value=0)
    template_repo = AsyncMock()

    # Story 4.8 T5 — `_load_templates` now resolves the whole stored DAG in ONE
    # `list_by_ids` call. This bridge delegates back to `get_by_id` so the ~40
    # existing call sites that stub `template_repo.get_by_id.return_value`
    # keep expressing what they meant. It is a TEST DOUBLE, not evidence that
    # the code batches: that is asserted directly by
    # `test_start_run_resolves_every_template_in_one_batch_query`, by the AC3
    # repo tests, and by the Postgres-real e2e suite.
    async def _list_by_ids(template_ids: Any, *, tenant_id: Any | None = None) -> dict[UUID, Any]:
        resolved: dict[UUID, Any] = {}
        for tid in set(template_ids):
            template = await template_repo.get_by_id(tid, tenant_id=tenant_id)
            if template is not None:
                resolved[tid] = template
        return resolved

    template_repo.list_by_ids = AsyncMock(side_effect=_list_by_ids)

    mise_en_place_service = AsyncMock()
    mise_en_place_service.run_checks = AsyncMock(return_value=_all_passed_mise_en_place_report())

    service = WorkflowExecutionService(
        workflow_repo=workflow_repo,
        workflow_run_repo=workflow_run_repo,
        template_repo=template_repo,
        # Revue P2 — le DAG de ces tests n'assigne aucun outil ; la requete
        # batchee rend un dict vide et chaque noeud repart sans outils.
        tool_hub_repo=AsyncMock(**{"list_resolved_for_templates.return_value": {}}),
        llm_router=AsyncMock(),
        checkpointer=AsyncMock(),
        # Explicitly empty: these tests inject `routing_decisions` straight
        # into the fake graph's updates, so no rule ever needs to evaluate.
        routing_rules=(),
        mise_en_place_service=mise_en_place_service,
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


# ─── Story 4.9 AC2/T2.2 — concurrency cap on start_run ─────────────────


@pytest.mark.asyncio
async def test_start_run_over_the_concurrency_cap_is_refused(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    workflow_run_repo.count_running = AsyncMock(return_value=20)

    with pytest.raises(RateLimitError):
        await service.start_run(workflow_id=uuid4(), run_input={})

    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_run_over_the_concurrency_cap_via_the_authoritative_recount_is_refused(
    event_publish_mock: AsyncMock,
) -> None:
    """The early check passes (a stale, racy count), but the authoritative
    recount taken in the SAME transaction as the INSERT still catches it."""
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    workflow_run_repo.count_running = AsyncMock(return_value=0)
    workflow_run_repo.count_running_in_session = AsyncMock(return_value=20)

    with pytest.raises(RateLimitError):
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
async def test_start_run_resolves_every_template_in_one_batch_query(
    event_publish_mock: AsyncMock,
) -> None:
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
    # `name` requis depuis la Story 5.1 : seul ce test passe par `start_run`,
    # qui compose l'accusé de réception à partir des noms de templates.
    template_repo.get_by_id.return_value = SimpleNamespace(config={}, id=uuid4(), name="tpl-a")
    service._drive_run = AsyncMock()  # type: ignore[method-assign]

    await service.start_run(workflow_id=workflow_id, run_input={"seed": 1})

    # Story 4.8 AC3 — ONE batch query for the whole DAG, not one per node.
    # Asserted on `list_by_ids` rather than through the harness bridge, so a
    # regression back to N sequential reads fails here.
    template_repo.list_by_ids.assert_awaited_once_with([template_id], tenant_id=None)
    # A resolution that returns nothing is an explicit 500, NOT `require_by_id`
    # + 404: a template that vanished is a server-side inconsistency, and this
    # endpoint's 404 already means "unknown workflow_id".
    # The caller's input is stamped on the row so a crash before LangGraph's
    # first checkpoint can still be restarted from START (AC3).
    create_kwargs = workflow_run_repo.create_in_session.await_args.kwargs
    assert create_kwargs["checkpoint"]["task_input"] == {"seed": 1}
    # Config fingerprints are stamped alongside so a resume can tell whether
    # the templates changed across the crash gap (intent gap 5).
    assert set(create_kwargs["checkpoint"]["template_fingerprints"]) == {"a"}


# ─── start_run — Mise en Place hook (Story 4.5 AC1-AC3) ────────────────


def _failing_mise_en_place_report(
    *, failed_code: str = "budget_available", retryable: bool = False
) -> MiseEnPlaceReport:
    codes = (
        "mcp_tools_reachable",
        "memory_namespaces_accessible",
        "budget_available",
        "llm_providers_configured",
    )
    checks = [
        CheckResult(
            code=code,  # type: ignore[arg-type]
            passed=code != failed_code,
            detail=f"{code} {'ok' if code != failed_code else 'failed'}",
            suggested_action=None if code != failed_code else f"fix {code}",
            retryable=retryable and code == failed_code,
        )
        for code in codes
    ]
    return build_report(checks)


def _passing_mise_en_place_report() -> MiseEnPlaceReport:
    """All four checks green — `failed_code` matches nothing."""
    return _failing_mise_en_place_report(failed_code="__none__")


@pytest.mark.asyncio
async def test_start_run_persists_mise_en_place_report_on_success(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    workflow_id = uuid4()
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    run = _workflow_run(workflow_id=workflow_id)
    workflow_run_repo.create_in_session.return_value = run
    service._drive_run = AsyncMock()  # type: ignore[method-assign]

    response = await service.start_run(workflow_id=workflow_id, run_input={})

    # The persisted document is the SAME shape the API returns (review
    # P13): `all_passed` is a real stored key, so an audit query can filter
    # on `mise_en_place->>'all_passed'` instead of recomputing it.
    create_kwargs = workflow_run_repo.create_in_session.await_args.kwargs
    persisted = create_kwargs["mise_en_place"]
    assert len(persisted["checks"]) == 4
    assert all(check["passed"] for check in persisted["checks"])
    assert persisted["all_passed"] is True
    assert persisted["bypassed"] is False
    assert response.mise_en_place.all_passed is True
    assert len(response.mise_en_place.checks) == 4


@pytest.mark.asyncio
async def test_start_run_blocks_when_mise_en_place_check_fails_without_force(
    event_publish_mock: AsyncMock,
) -> None:
    """AC2 — a failing check with no ``force`` refuses the launch, and NO
    row/event is ever created (``assert_not_awaited``, mirror the pattern
    already used for the unknown-workflow/inactive-workflow tests above).

    A budget overrun is NOT retryable, so the refusal is a 422, not a 503
    (review BS5)."""
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    service._mise_en_place_service.run_checks = AsyncMock(  # type: ignore[attr-defined]
        return_value=_failing_mise_en_place_report(failed_code="budget_available")
    )

    with pytest.raises(BusinessRuleError) as exc_info:
        await service.start_run(workflow_id=uuid4(), run_input={})

    assert exc_info.value.status == 422
    assert exc_info.value.context["failed_checks"] == ["budget_available"]
    # NOT `context["detail"]` — `app.main`'s RFC 7807 handler drops any
    # context key colliding with a reserved field (`detail` included), so
    # the readable summary must live on the exception's own `detail`.
    assert "budget_available" in exc_info.value.detail
    # Plural key (review P15): it is a list, and the singular name collided
    # with the per-check `suggested_action: str | None`.
    assert exc_info.value.context["suggested_actions"] == ["fix budget_available"]
    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_run_when_refused_should_publish_an_audit_event(
    event_publish_mock: AsyncMock,
    event_publish_and_commit_mock: AsyncMock,
) -> None:
    """A refused launch creates no row, so its report has nowhere to be
    persisted (AC2) — it is traced in the outbox instead, or it vanishes
    the moment the error is returned (review BS2)."""
    set_correlation_id(str(uuid4()))
    workflow_id = uuid4()
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    service._mise_en_place_service.run_checks = AsyncMock(  # type: ignore[attr-defined]
        return_value=_failing_mise_en_place_report(failed_code="budget_available")
    )

    with pytest.raises(BusinessRuleError):
        await service.start_run(workflow_id=workflow_id, run_input={})

    # Still no row and no `started` event — the audit trace must not
    # resurrect any part of the launch (AC2).
    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()  # no `started` event

    event_publish_and_commit_mock.assert_awaited_once()
    args = event_publish_and_commit_mock.await_args.args
    assert args[1] == WorkflowRunMiseEnPlaceRefusedEvent.event_type
    payload = args[2]
    assert payload.workflow_id == workflow_id
    assert payload.failed_checks == ["budget_available"]
    assert payload.retryable is False
    assert payload.mise_en_place["all_passed"] is False
    # No run on the `start_run` path — Story 4.12 AC4 added `run_id` to this
    # event for the `resume` path, but it stays `None` here since there
    # genuinely is no run to name.
    assert payload.run_id is None


@pytest.mark.asyncio
async def test_start_run_when_refusal_audit_fails_should_still_raise_the_real_error(
    event_publish_and_commit_mock: AsyncMock,
) -> None:
    """Best-effort audit: replacing a precise "your budget is exceeded" with
    an opaque database error would trade a useful answer for a useless one."""
    set_correlation_id(str(uuid4()))
    service, workflow_repo, _run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    service._mise_en_place_service.run_checks = AsyncMock(  # type: ignore[attr-defined]
        return_value=_failing_mise_en_place_report(failed_code="budget_available")
    )
    event_publish_and_commit_mock.side_effect = RuntimeError("outbox down")

    with pytest.raises(BusinessRuleError) as exc_info:
        await service.start_run(workflow_id=uuid4(), run_input={})

    assert "budget_available" in exc_info.value.detail


@pytest.mark.asyncio
async def test_start_run_blocks_with_503_when_every_failing_check_is_retryable(
    event_publish_mock: AsyncMock,
) -> None:
    """An MCP server that is down MAY come back on its own, so 503 (which
    tells clients and proxies to retry) is honest there — and only there
    (review BS5)."""
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    service._mise_en_place_service.run_checks = AsyncMock(  # type: ignore[attr-defined]
        return_value=_failing_mise_en_place_report(
            failed_code="mcp_tools_reachable", retryable=True
        )
    )

    with pytest.raises(DependencyError) as exc_info:
        await service.start_run(workflow_id=uuid4(), run_input={})

    assert exc_info.value.status == 503
    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_run_blocks_with_422_when_any_failing_check_is_not_retryable(
    event_publish_mock: AsyncMock,
) -> None:
    """Mixed failures: retrying cannot fix the permanent half, so advertising
    "retry later" would be a lie. The permanent half decides."""
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    mixed = build_report(
        [
            CheckResult(code="mcp_tools_reachable", passed=False, detail="down", retryable=True),
            CheckResult(code="memory_namespaces_accessible", passed=True, detail="ok"),
            CheckResult(code="budget_available", passed=True, detail="ok"),
            CheckResult(
                code="llm_providers_configured", passed=False, detail="no key", retryable=False
            ),
        ]
    )
    service._mise_en_place_service.run_checks = AsyncMock(  # type: ignore[attr-defined]
        return_value=mixed
    )

    with pytest.raises(BusinessRuleError) as exc_info:
        await service.start_run(workflow_id=uuid4(), run_input={})

    assert exc_info.value.status == 422
    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_run_force_without_reason_raises_422(event_publish_mock: AsyncMock) -> None:
    """AC3 — ``force=true`` with a blank/absent ``reason`` is rejected
    unconditionally, BEFORE the checks even run (defensive re-validation of
    what ``StartRunRequest``'s Pydantic validator already guarantees at the
    HTTP boundary — this method is a real surface a caller can reach
    directly)."""
    set_correlation_id(str(uuid4()))
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")

    with pytest.raises(ValidationError, match="reason is required when force=true"):
        await service.start_run(workflow_id=uuid4(), run_input={}, force=True)

    service._mise_en_place_service.run_checks.assert_not_awaited()  # type: ignore[attr-defined]
    workflow_run_repo.create_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_run_bypasses_failing_check_with_force_and_reason(
    event_publish_mock: AsyncMock,
) -> None:
    """AC3 — ``force=true`` + a non-blank ``reason`` starts the run despite
    the failing check, persists ``bypassed=true``/``bypass_reason``, and
    publishes ``mise_en_place_bypassed`` alongside ``started`` in the same
    transaction."""
    set_correlation_id(str(uuid4()))
    workflow_id = uuid4()
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    run = _workflow_run(workflow_id=workflow_id)
    workflow_run_repo.create_in_session.return_value = run
    service._mise_en_place_service.run_checks = AsyncMock(  # type: ignore[attr-defined]
        return_value=_failing_mise_en_place_report(failed_code="mcp_tools_reachable")
    )
    service._drive_run = AsyncMock()  # type: ignore[method-assign]

    response = await service.start_run(
        workflow_id=workflow_id, run_input={}, force=True, reason="incident P1"
    )

    workflow_run_repo.create_in_session.assert_awaited_once()
    create_kwargs = workflow_run_repo.create_in_session.await_args.kwargs
    assert create_kwargs["mise_en_place"]["bypassed"] is True
    assert create_kwargs["mise_en_place"]["bypass_reason"] == "incident P1"

    published_types = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.started" in published_types
    assert "workflow_engine.workflow_run.mise_en_place_bypassed" in published_types

    bypass_call = next(
        call
        for call in event_publish_mock.await_args_list
        if call.args[0] == "workflow_engine.workflow_run.mise_en_place_bypassed"
    )
    bypass_event = bypass_call.args[1]
    assert bypass_event.reason == "incident P1"
    assert bypass_event.failed_checks == ["mcp_tools_reachable"]
    assert bypass_event.run_id == run.id

    assert response.mise_en_place.bypassed is True
    assert response.mise_en_place.bypass_reason == "incident P1"


@pytest.mark.asyncio
async def test_start_run_force_with_all_checks_passing_publishes_no_bypass_event(
    event_publish_mock: AsyncMock,
) -> None:
    """AC3's "no-op silencieux" — nothing was actually bypassed, so no
    ``mise_en_place_bypassed`` event is published even though ``force`` and
    a ``reason`` were both provided."""
    set_correlation_id(str(uuid4()))
    workflow_id = uuid4()
    service, workflow_repo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_repo.require_by_id.return_value = _workflow(status="active")
    run = _workflow_run(workflow_id=workflow_id)
    workflow_run_repo.create_in_session.return_value = run
    service._drive_run = AsyncMock()  # type: ignore[method-assign]

    response = await service.start_run(
        workflow_id=workflow_id, run_input={}, force=True, reason="just in case"
    )

    published_types = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.mise_en_place_bypassed" not in published_types
    assert response.mise_en_place.bypassed is False
    create_kwargs = workflow_run_repo.create_in_session.await_args.kwargs
    assert create_kwargs["mise_en_place"]["bypassed"] is False


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
    templates = {"a": SimpleNamespace(config={}, id=uuid4())}
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
    templates = {
        "b": SimpleNamespace(config={}, id=uuid4()),
        "c": SimpleNamespace(config={}, id=uuid4()),
    }

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
    templates = {name: SimpleNamespace(config={}, id=uuid4()) for name in ("a", "b", "c")}

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
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
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
    templates = {
        "a": SimpleNamespace(config={}, id=uuid4()),
        "b": SimpleNamespace(config={}, id=uuid4()),
    }

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
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    # Still `completed` — the reachable graph genuinely finished.
    assert workflow_run_repo.update_status.await_args.kwargs["status"] == "completed"
    # But no longer silent about it.
    final_checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert final_checkpoint["skipped_nodes"] == ["b", "c"]
    assert final_checkpoint["node_statuses"] == {"a": "success", "b": "skipped", "c": "skipped"}


@pytest.mark.asyncio
async def test_completed_run_with_skipped_nodes_still_reflects_handoffs_in_checkpoint(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """T5.3 — the SECOND checkpoint-rebuild spot (the skipped-nodes branch of
    `_mark_completed`) mirrors `handoffs` too, not just `_sync_checkpoint`."""
    workflow = _workflow(
        dag={
            "nodes": [
                {"node_id": "a", "agent_template_id": str(uuid4())},
                {"node_id": "b", "agent_template_id": str(uuid4())},
            ],
            "edges": [
                {"from_node_id": "a", "to_node_id": "b", "condition": "output.go == true"},
            ],
        }
    )
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "a": {
                    "node_outputs": {"a": {"go": False}},
                    "node_metrics": {"a": {}},
                    "handoffs": {"a": {"decisions": ["done"]}},
                }
            }
        ],
        final_state={
            "node_outputs": {"a": {"go": False}},
            "node_metrics": {"a": {}},
            "handoffs": {"a": {"decisions": ["done"]}},
        },
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    final_checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert final_checkpoint["skipped_nodes"] == ["b"]
    assert final_checkpoint["handoffs"] == {"a": {"decisions": ["done"]}}


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
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
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


# ═══════════════════════════════════════════════════════════════════════
# Story 4.7 AC3 — handoff-summary token-reduction aggregation
# ═══════════════════════════════════════════════════════════════════════


def test_aggregate_handoffs_computes_reduction_ratio_from_real_tokens() -> None:
    import agentive_backend.features.workflow_engine.service as svc_module

    # B-01 — the ratio reads the CONSUMER-side channel: what prompts really
    # substituted, not what producers happened to summarize.
    result = svc_module._aggregate_handoffs(
        {},
        {
            "b": {"raw_tokens_replaced": 100, "summary_tokens": 20, "sources": ["a"]},
            "c": {"raw_tokens_replaced": 200, "summary_tokens": 40, "sources": ["a", "b"]},
        },
    )

    assert result["raw_tokens_replaced"] == 300
    assert result["summary_tokens"] == 60
    assert result["reduction_ratio"] == 0.8


def test_aggregate_handoffs_when_no_raw_tokens_replaced_should_report_none_ratio() -> None:
    import agentive_backend.features.workflow_engine.service as svc_module

    result = svc_module._aggregate_handoffs({})

    assert result == {
        "raw_tokens_replaced": 0,
        "summary_tokens": 0,
        "reduction_ratio": None,
        "tokens": {"input": 0, "output": 0},
        "cost_usd": None,
    }


def test_aggregate_handoffs_ignores_corrupt_entries_without_poisoning_the_total() -> None:
    """Mirror `_aggregate_routing`'s defensive posture — a single corrupt
    entry (not a dict) is skipped, never raises."""
    import agentive_backend.features.workflow_engine.service as svc_module

    result = svc_module._aggregate_handoffs(
        {},
        {
            "b": {"raw_tokens_replaced": 100, "summary_tokens": 20},
            "c": "not a dict",
        },
    )

    assert result["raw_tokens_replaced"] == 100
    assert result["summary_tokens"] == 20


@pytest.mark.parametrize("corrupt", ["n/a", {"nested": 1}, [1, 2], None, True, -5])
def test_aggregate_handoffs_survives_a_corrupt_token_value(corrupt: object) -> None:
    """P-5 — the docstring promised "a corrupt entry is skipped, never poisons
    the whole aggregate" while only checking `isinstance(entry, dict)`:
    `int("n/a")` raises `ValueError`, `int({...})` raises `TypeError`.

    That mattered because `_mark_completed` calls `_aggregate_metrics` with NO
    `try`/`except` (unlike the partial path, which has one), so one corrupt
    value meant `update_status("completed")` was never reached: the run stayed
    `running`, got claimed by the recovery sweep, was RE-EXECUTED up to
    `MAX_RECOVERY_ATTEMPTS` and finally marked `error` — a run that had in
    fact succeeded.
    """
    import agentive_backend.features.workflow_engine.service as svc_module

    result = svc_module._aggregate_handoffs(
        {"p": {"summary_input_tokens": corrupt, "summary_output_tokens": corrupt}},
        {
            "b": {"raw_tokens_replaced": 100, "summary_tokens": 20},
            "c": {"raw_tokens_replaced": corrupt, "summary_tokens": corrupt},
        },
    )

    assert result["raw_tokens_replaced"] == 100
    assert result["summary_tokens"] == 20
    assert result["tokens"] == {"input": 0, "output": 0}


def test_aggregate_handoffs_ignores_an_unparsable_cost() -> None:
    """P-5, cost side — same posture, and `Decimal("oops")` raises
    `InvalidOperation`, which is not a `ValueError` subclass worth relying on
    by accident."""
    import agentive_backend.features.workflow_engine.service as svc_module

    result = svc_module._aggregate_handoffs(
        {
            "a": {"summary_cost_usd": "0.001", "summary_output_tokens": 5},
            "b": {"summary_cost_usd": "oops", "summary_output_tokens": 5},
        },
        {},
    )

    assert result["cost_usd"] == "0.001"


def test_aggregate_handoffs_warns_when_the_summaries_came_out_bigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-10 — a ratio below zero means the optimization costs more than it
    saves. The value is reported as measured (clamping would hide the only
    signal that says so), but it is no longer reported in silence: a workflow
    of short-output control nodes would otherwise publish
    `reduction_ratio_pct: -180.0` through `/handoff-stats` as an economy."""
    import agentive_backend.features.workflow_engine.service as svc_module

    events: list[str] = []
    monkeypatch.setattr(svc_module._log, "warning", lambda e, **kw: events.append(e))

    result = svc_module._aggregate_handoffs(
        {}, {"b": {"raw_tokens_replaced": 5, "summary_tokens": 100}}
    )

    assert result["reduction_ratio"] < 0
    assert "workflow_engine.handoff_summary_inflated" in events


def test_aggregate_metrics_with_a_handoffs_channel_of_the_wrong_type_does_not_raise() -> None:
    """P-5 — `handoffs or {}` only guards `None`; `_mark_completed` has no
    net, so `.values()` on a list ended a successful run as `error`."""
    import agentive_backend.features.workflow_engine.service as svc_module

    metrics = svc_module._aggregate_metrics(
        {"a": {"input_tokens": 1, "output_tokens": 1}},
        total_duration_ms=0,
        handoffs=["not a mapping"],  # type: ignore[arg-type]
    )

    assert metrics["handoffs"]["raw_tokens_replaced"] == 0


def test_aggregate_metrics_folds_handoffs_block_and_projects_summary_tokens_per_node() -> None:
    import agentive_backend.features.workflow_engine.service as svc_module

    metrics = svc_module._aggregate_metrics(
        {"a": {"input_tokens": 50, "output_tokens": 7}},
        total_duration_ms=0,
        handoffs={
            "a": {
                "decisions": [],
                "artifacts_refs": [],
                "blockers": [],
                "next_questions": [],
                "summary_input_tokens": 30,
                "summary_output_tokens": 9,
                "raw_output_tokens_replaced": 7,
            }
        },
        handoff_substitutions={"b": {"raw_tokens_replaced": 7, "summary_tokens": 9}},
    )

    assert metrics["handoffs"] == {
        "raw_tokens_replaced": 7,
        "summary_tokens": 9,
        "reduction_ratio": round(1 - 9 / 7, 4),
        # Review of 2026-09-12 (P-2) — the FULL spend of the summary call,
        # input included. Distinct from `summary_tokens`, which is only the
        # output side, because only the output side is what got substituted.
        "tokens": {"input": 30, "output": 9},
        "cost_usd": None,
    }
    assert metrics["per_node"]["a"]["handoff_summary_tokens"] == {"input": 30, "output": 9}
    # P-2 — and the run's totals now carry it: 50/7 from the node itself PLUS
    # 30/9 from its summary. Before this fix the run billed two LLM calls and
    # reported one, so `total_cost_usd` diverged permanently from the invoice.
    assert metrics["total_tokens"] == {"input": 80, "output": 16}


def test_aggregate_metrics_handoffs_defaults_to_zeroed_shape_when_absent() -> None:
    import agentive_backend.features.workflow_engine.service as svc_module

    metrics = svc_module._aggregate_metrics({}, total_duration_ms=0)

    assert metrics["handoffs"] == {
        "raw_tokens_replaced": 0,
        "summary_tokens": 0,
        "reduction_ratio": None,
        "tokens": {"input": 0, "output": 0},
        "cost_usd": None,
    }


@pytest.mark.asyncio
async def test_sync_checkpoint_reflects_handoffs_channel() -> None:
    """T5.2 — mirror `routing_decisions`: the applicative checkpoint always
    carries the CURRENT `handoffs` state, even when empty."""
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._sync_checkpoint(
        uuid4(),
        {
            "node_outputs": {"a": {"x": 1}},
            "handoffs": {"a": {"decisions": ["done"]}},
        },
        last_node_id="a",
        templates={},
    )

    checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert checkpoint["handoffs"] == {"a": {"decisions": ["done"]}}


@pytest.mark.asyncio
async def test_sync_checkpoint_handoffs_defaults_to_empty_dict_when_absent() -> None:
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()

    await service._sync_checkpoint(uuid4(), {"node_outputs": {}}, last_node_id="a", templates={})

    checkpoint = workflow_run_repo.update_checkpoint.await_args.kwargs["checkpoint"]
    assert checkpoint["handoffs"] == {}


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
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
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
        {"a": SimpleNamespace(config={}, id=uuid4()), "b": SimpleNamespace(config={}, id=uuid4())},
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
        {"a": SimpleNamespace(config={}, id=uuid4()), "b": SimpleNamespace(config={}, id=uuid4())},
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
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
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
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
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

    await service._resume_run(run.id, workflow, {"a": SimpleNamespace(config={}, id=uuid4())})

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
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    assert captured["rules"] == (rule,)
    assert captured["routing_settings"] is not None
    assert captured["routing_settings"].threshold == pytest.approx(
        settings.routing_confidence_threshold
    )


@pytest.mark.asyncio
async def test_node_tools_are_resolved_and_forwarded_to_build_state_graph(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """Revue P2 — le câblage manquant, et le test qui le tenait.

    `node_tools` avait été filé jusque dans la signature de
    `build_state_graph` sans qu'aucun appelant de production ne le passe :
    chaque nœud tournait sans outils alors que la moitié moteur de l'AC1/AC2
    était annoncée livrée. Les tests du moteur passaient `resolved_tools=`
    directement à `execute_agent_node`, donc court-circuitaient exactement le
    maillon absent.

    Ce test part du SERVICE et regarde ce que `build_state_graph` reçoit —
    c'est la seule altitude à laquelle l'oubli était visible.
    """
    import agentive_backend.features.workflow_engine.service as svc_module

    template_id = uuid4()
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(template_id)}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {"a": {}}},
    )
    captured: dict[str, Any] = {}

    def _fake_build(*args: Any, **kwargs: Any) -> _FakeGraphBuilder:
        captured["node_tools"] = kwargs.get("node_tools")
        return _FakeGraphBuilder(compiled)

    monkeypatch.setattr(svc_module, "build_state_graph", _fake_build)

    tool_row = SimpleNamespace(
        id=uuid4(),
        name="search_files",
        description="Search the repo.",
        input_schema={"type": "object", "properties": {}},
    )
    server_row = SimpleNamespace(
        id=uuid4(),
        transport="stdio",
        connection_config={"command": "srv"},
    )

    service, _wrepo, _workflow_run_repo, _trepo = _make_execution_service()
    service._tool_hub_repo.list_resolved_for_templates.return_value = {  # type: ignore[attr-defined]
        template_id: [(tool_row, server_row)]
    }

    await service._drive_run(
        uuid4(),
        workflow,
        {"a": SimpleNamespace(config={}, id=template_id)},
        {},
        correlation_id=uuid4(),
    )

    node_tools = captured["node_tools"]
    assert node_tools is not None, "build_state_graph n'a reçu aucun node_tools"
    assert set(node_tools) == {"a"}
    resolved = node_tools["a"]["search_files"]
    # La projection complète : offrir un outil ne demande que la ligne `Tool`,
    # l'APPELER demande le transport et la config du serveur. Un `ResolvedTool`
    # amputé de ces deux champs compilerait et échouerait à l'exécution.
    assert resolved.tool_id == tool_row.id
    assert resolved.server_id == server_row.id
    assert resolved.transport == "stdio"
    assert resolved.connection_config == {"command": "srv"}


@pytest.mark.asyncio
async def test_a_dag_whose_templates_have_no_tools_costs_one_query_and_yields_nothing(
    monkeypatch: pytest.MonkeyPatch, event_publish_mock: AsyncMock
) -> None:
    """Le chemin d'avant la story, inchangé — et une seule requête pour tout
    le DAG, pas une par nœud (le N+1 que `_load_templates` a déjà eu à
    refermer)."""
    import agentive_backend.features.workflow_engine.service as svc_module

    template_id = uuid4()
    workflow = _workflow(
        dag={
            "nodes": [
                {"node_id": "a", "agent_template_id": str(template_id)},
                {"node_id": "b", "agent_template_id": str(template_id)},
            ],
            "edges": [],
        }
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {"a": {}}},
    )
    captured: dict[str, Any] = {}

    def _fake_build(*args: Any, **kwargs: Any) -> _FakeGraphBuilder:
        captured["node_tools"] = kwargs.get("node_tools")
        return _FakeGraphBuilder(compiled)

    monkeypatch.setattr(svc_module, "build_state_graph", _fake_build)

    service, _wrepo, _workflow_run_repo, _trepo = _make_execution_service()
    repo = service._tool_hub_repo  # type: ignore[attr-defined]
    repo.list_resolved_for_templates.return_value = {}

    template = SimpleNamespace(config={}, id=template_id)
    await service._drive_run(
        uuid4(), workflow, {"a": template, "b": template}, {}, correlation_id=uuid4()
    )

    assert captured["node_tools"] == {}
    # Deux nœuds, un seul template, UNE requête — et des ids dédupliqués.
    assert repo.list_resolved_for_templates.await_count == 1
    assert repo.list_resolved_for_templates.await_args.args[0] == [template_id]


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
        run_id, workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
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


# ─── Story 4.6 — request_run_control (AC1) ───────────────────────────


def _control_service() -> tuple[WorkflowExecutionService, AsyncMock, AsyncMock]:
    """`_make_execution_service` with the two control-path repo methods given
    real rowcounts. Left as bare `AsyncMock`s they return `MagicMock`, which
    compares unequal to 0 by accident — every "did the CAS land?" assertion
    would then pass for the wrong reason."""
    service, _wrepo, workflow_run_repo, _trepo = _make_execution_service()
    workflow_run_repo.request_control = AsyncMock(return_value=1)
    workflow_run_repo.get_control_signal = AsyncMock(return_value=None)
    # AC1 requires the row write and its audit event to commit together, so
    # `request_run_control` drives the `_in_session` variants — the
    # self-managed wrappers above commit on their own and would leave the
    # publish in a second transaction.
    workflow_run_repo.request_control_in_session = AsyncMock(return_value=1)
    workflow_run_repo.update_status_in_session = AsyncMock(return_value=1)
    # A 409 re-reads the row to describe the conflict with the state that is
    # true NOW, not the snapshot the caller raced against. Default the
    # re-read to whatever `get_by_id` was given, so a test that does not care
    # gets a coherent row instead of a `MagicMock` that compares equal to
    # nothing.
    workflow_run_repo.get_by_id_in_session = AsyncMock(
        side_effect=lambda _session, _run_id: workflow_run_repo.get_by_id.return_value
    )
    return service, workflow_run_repo, _wrepo


@pytest.mark.asyncio
async def test_request_run_control_when_pausing_a_running_run_should_record_a_deferred_signal(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="running")
    run_repo.get_by_id.return_value = run

    response = await service.request_run_control(run_id=run.id, action="pause")

    # The status is UNCHANGED — interruption is cooperative, the driver
    # settles it at the next superstep boundary.
    assert response.status == "running"
    assert response.control_signal == "pause"
    run_repo.request_control_in_session.assert_awaited_once()
    assert run_repo.request_control_in_session.await_args.kwargs["signal"] == "pause"
    assert run_repo.request_control_in_session.await_args.kwargs["only_if_status"] == "running"
    run_repo.update_status_in_session.assert_not_awaited()
    assert event_publish_mock.await_count == 1
    assert event_publish_mock.await_args.args[0] == "workflow_engine.workflow_run.pause_requested"


@pytest.mark.asyncio
async def test_request_run_control_when_cancelling_a_running_run_should_record_a_deferred_signal(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="running")
    run_repo.get_by_id.return_value = run

    response = await service.request_run_control(run_id=run.id, action="cancel")

    assert response.status == "running"
    assert response.control_signal == "cancel"
    assert run_repo.request_control_in_session.await_args.kwargs["signal"] == "cancel"
    run_repo.update_status_in_session.assert_not_awaited()
    assert event_publish_mock.await_args.args[0] == "workflow_engine.workflow_run.cancel_requested"


@pytest.mark.asyncio
async def test_request_run_control_when_cancelling_a_paused_run_should_be_terminal_immediately(
    event_publish_mock: AsyncMock,
) -> None:
    """No driver is alive to observe a signal on a paused run — the caller's
    own request has to move the row."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="paused")
    run_repo.get_by_id.return_value = run

    response = await service.request_run_control(run_id=run.id, action="cancel")

    assert response.status == "cancelled"
    assert response.control_signal is None
    run_repo.request_control_in_session.assert_not_awaited()
    status_call = run_repo.update_status_in_session.await_args
    assert status_call.kwargs["status"] == "cancelled"
    assert status_call.kwargs["only_if_status"] == "paused"
    assert status_call.kwargs["clear_control"] is True
    assert status_call.kwargs["ended_at"] is not None
    assert event_publish_mock.await_args.args[0] == "workflow_engine.workflow_run.cancelled"


@pytest.mark.asyncio
async def test_request_run_control_when_resuming_a_paused_run_should_relaunch_and_clear_signal(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_correlation_id(str(uuid4()))
    service, run_repo, workflow_repo = _control_service()
    run = _workflow_run(status="paused")
    run_repo.get_by_id.return_value = run
    workflow_repo.require_by_id.return_value = _workflow()
    resume_mock = AsyncMock()
    monkeypatch.setattr(service, "_resume_run", resume_mock)
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )

    response = await service.request_run_control(run_id=run.id, action="resume")

    assert response.status == "running"
    assert response.control_signal is None
    status_call = run_repo.update_status_in_session.await_args
    assert status_call.kwargs["status"] == "running"
    assert status_call.kwargs["only_if_status"] == "paused"
    assert status_call.kwargs["clear_control"] is True
    # `ended_at` must NOT be stamped — the run is alive again.
    assert status_call.kwargs.get("ended_at") is None
    # …and the resume counts as activity, or `claim_stale_running` reclaims
    # a long-paused run within one sweep tick and drives it a second time.
    assert status_call.kwargs["touch_last_checkpoint"] is True
    # Story 4.12 AC3 added a second event published on every resume
    # (`resume_mise_en_place_evaluated`, after this one) — check by type
    # rather than assuming `resumed` is the last call.
    published = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.resumed" in published

    # The background task must actually be scheduled, and RETAINED.
    await asyncio.sleep(0)
    resume_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("pause", "paused"),
        ("pause", "completed"),
        ("pause", "error"),
        ("pause", "cancelled"),
        ("resume", "running"),
        ("resume", "completed"),
        ("resume", "error"),
        ("resume", "cancelled"),
        ("cancel", "completed"),
        ("cancel", "error"),
        ("cancel", "cancelled"),
    ],
)
async def test_request_run_control_when_transition_is_illegal_should_conflict_without_event(
    action: str, status: str, event_publish_mock: AsyncMock
) -> None:
    """The negative assertion is the point: a 409 must leave NO trace in the
    outbox. Publishing an event for a transition that did not happen is the
    exact discipline `_mark_completed`/`_mark_failed` already enforce."""
    from agentive_backend.shared.exceptions import ConflictError

    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status=status)
    run_repo.get_by_id.return_value = run

    with pytest.raises(ConflictError) as excinfo:
        await service.request_run_control(run_id=run.id, action=action)  # type: ignore[arg-type]

    assert excinfo.value.context["run_id"] == str(run.id)
    assert excinfo.value.context["current_status"] == status
    event_publish_mock.assert_not_awaited()
    run_repo.request_control_in_session.assert_not_awaited()
    run_repo.update_status_in_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_resume_hits_a_stuck_run_should_name_the_pending_signal(
    event_publish_mock: AsyncMock,
) -> None:
    """Review lot 9 (F3) — the deadlock `pending_control_signal` was named for,
    on the one path that did not carry it.

    A process dying between writing a signal and observing it leaves the run
    `running` with a pending `pause`. The operator's next move is `resume`,
    which is illegal from `running` — so it takes the ILLEGAL-STATUS branch,
    not the compare-and-set branch, and that branch built a 409 out of four
    members with no way to say why the run was stuck. The field answered the
    CAS race and missed the deadlock.
    """
    from agentive_backend.shared.exceptions import ConflictError

    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="running", control_signal="pause")
    run_repo.get_by_id.return_value = run

    with pytest.raises(ConflictError) as excinfo:
        await service.request_run_control(run_id=run.id, action="resume")

    assert excinfo.value.context["pending_control_signal"] == "pause"
    # The full contract AC1 states, on this branch too.
    assert set(excinfo.value.context) == {
        "run_id",
        "current_status",
        "allowed_from",
        "action",
        "pending_control_signal",
    }
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_nothing_is_pending_should_say_so_explicitly(
    event_publish_mock: AsyncMock,
) -> None:
    """`None` is an answer, not an omission: it tells the operator the run is
    refusing for its status alone, so no escalation will unblock it."""
    from agentive_backend.shared.exceptions import ConflictError

    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="completed")
    run_repo.get_by_id.return_value = run

    with pytest.raises(ConflictError) as excinfo:
        await service.request_run_control(run_id=run.id, action="pause")

    assert excinfo.value.context["pending_control_signal"] is None


@pytest.mark.asyncio
async def test_request_run_control_when_run_is_unknown_should_raise_not_found(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = None

    with pytest.raises(NotFoundError):
        await service.request_run_control(run_id=uuid4(), action="pause")
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_signal_cas_loses_should_conflict_without_event(
    event_publish_mock: AsyncMock,
) -> None:
    """Rowcount 0 — a concurrent caller got there first, or the run finished
    between the read and the write. Treated exactly like an illegal status."""
    from agentive_backend.shared.exceptions import ConflictError

    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running")
    run_repo.request_control_in_session.return_value = 0

    with pytest.raises(ConflictError):
        await service.request_run_control(run_id=uuid4(), action="pause")
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_status_cas_loses_should_conflict_without_event(
    event_publish_mock: AsyncMock,
) -> None:
    from agentive_backend.shared.exceptions import ConflictError

    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="paused")
    run_repo.update_status_in_session.return_value = 0

    with pytest.raises(ConflictError):
        await service.request_run_control(run_id=uuid4(), action="cancel")
    event_publish_mock.assert_not_awaited()


def _resumable_service(report: MiseEnPlaceReport) -> tuple[Any, AsyncMock, AsyncMock]:
    """`_control_service` wired for a resume: a paused run, a loadable
    workflow, and a Mise en Place verdict the caller chooses."""
    service, run_repo, workflow_repo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="paused")
    workflow_repo.require_by_id.return_value = _workflow()
    service._mise_en_place_service.run_checks = AsyncMock(return_value=report)
    return service, run_repo, workflow_repo


# ─── Story 4.12 AC5 — _already_executed_node_ids ───────────────────────


def test_already_executed_node_ids_reads_node_statuses_keys() -> None:
    assert _already_executed_node_ids({"node_statuses": {"a": "success", "b": "success"}}) == {
        "a",
        "b",
    }


def test_already_executed_node_ids_includes_skipped_nodes_too() -> None:
    """A structurally-skipped node (Story 4.6's truncated-branch detection)
    will not run on resume either — it belongs in the exclusion set exactly
    like a succeeded one."""
    assert _already_executed_node_ids({"node_statuses": {"a": "success", "b": "skipped"}}) == {
        "a",
        "b",
    }


@pytest.mark.parametrize(
    "checkpoint",
    [None, {}, {"node_statuses": "not a dict"}, {"node_statuses": None}, "not a dict"],
)
def test_already_executed_node_ids_tolerates_malformed_checkpoints(checkpoint: Any) -> None:
    """Best-effort like its sibling `_last_node_id`: an empty set reproduces
    the pre-4.12 behaviour (price the whole DAG) rather than raising."""
    assert _already_executed_node_ids(checkpoint) == frozenset()


# ─── Story 4.12 AC1/T1.3 — resuming a deactivated workflow ─────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_cap_usd", [None, Decimal("5.00")])
async def test_resume_of_a_deactivated_workflow_proceeds_whatever_the_budget_cap(
    budget_cap_usd: Decimal | None,
    monkeypatch: pytest.MonkeyPatch,
    event_publish_mock: AsyncMock,
) -> None:
    """Deactivating a workflow governs LAUNCH, not continuation.

    Two things made refusing the wrong call. `WorkflowRecoveryWorker` never
    came through this path — it resumes orphaned runs of a deactivated
    workflow regardless — so refusing here constrained only the operator,
    i.e. the caller with the most context. And a refusal had no exit: a
    `paused` run is excluded from `claim_stale_running` AND from
    `list_purgeable`, so it would sit unresumable and unpurgeable forever
    while `count_stale_paused` reported it daily. Killing the run remains
    available, explicitly, as `POST /cancel`.

    The outcome must also not depend on `AGENTIVE_DRY_RUN_BUDGET_CAP_USD`,
    which is why this is parametrised: with a cap set, the resume's own
    pre-flight Dry Run is what used to refuse the workflow it was pricing."""
    from agentive_backend.shared.config import settings

    monkeypatch.setattr(settings, "dry_run_budget_cap_usd", budget_cap_usd)
    service, run_repo, workflow_repo = _resumable_service(_passing_mise_en_place_report())
    workflow_repo.require_by_id.return_value = _workflow(status="draft")

    response = await service.request_run_control(run_id=uuid4(), action="resume")

    assert response.status == "running"
    run_repo.update_status_in_session.assert_awaited()
    # And the pre-flight was told it may price an inactive workflow.
    kwargs = service._mise_en_place_service.run_checks.await_args.kwargs
    assert kwargs["allow_inactive_workflow"] is True


@pytest.mark.asyncio
async def test_resume_of_an_active_workflow_still_proceeds(
    event_publish_mock: AsyncMock,
) -> None:
    """No regression on the ordinary path — only an INACTIVE workflow is
    newly refused."""
    service, _run_repo, workflow_repo = _resumable_service(_passing_mise_en_place_report())
    workflow_repo.require_by_id.return_value = _workflow(status="active")

    response = await service.request_run_control(run_id=uuid4(), action="resume")

    assert response.status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retryable", "expected"),
    [(False, BusinessRuleError), (True, DependencyError)],
)
async def test_request_run_control_when_resume_fails_the_preflight_should_refuse_and_stay_paused(
    retryable: bool,
    expected: type[Exception],
    event_publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 4.6 review `IG2`. An environment decays while a run sits paused —
    a rotated API key, a decommissioned MCP server, an exhausted budget. An
    ungated resume walked straight into it: the row moved to `running`, the
    first node raised, `_mark_failed` closed the run `error`, and a checkpoint
    that was still resumable a second earlier was gone for good.

    Refusing leaves the row `paused`, so the run survives its environment and
    can be resumed once a human fixes it. Same 422/503 split as `start_run`:
    503 only when an identical retry could plausibly succeed on its own."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _resumable_service(
        _failing_mise_en_place_report(retryable=retryable)
    )
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )

    with pytest.raises(expected):
        await service.request_run_control(run_id=uuid4(), action="resume")

    # The row must NOT have moved, and no `resumed` event may exist for a
    # resume that did not happen.
    run_repo.update_status_in_session.assert_not_awaited()
    published = [c.args[0] for c in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.resumed" not in published


@pytest.mark.asyncio
async def test_request_run_control_when_resume_refusal_names_the_stuck_run(
    event_publish_and_commit_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 4.12 AC4 — unlike a refused `start_run` (no run exists to
    name), a refused `resume` has a real, still-`paused` run behind it. A
    workflow with several paused runs must be able to tell WHICH one this
    refusal is about."""
    set_correlation_id(str(uuid4()))
    service, _run_repo, _wrepo = _resumable_service(_failing_mise_en_place_report())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )
    run_id = uuid4()

    with pytest.raises(BusinessRuleError):
        await service.request_run_control(run_id=run_id, action="resume")

    event_publish_and_commit_mock.assert_awaited_once()
    args = event_publish_and_commit_mock.await_args.args
    assert args[1] == WorkflowRunMiseEnPlaceRefusedEvent.event_type
    payload = args[2]
    assert payload.run_id == run_id


@pytest.mark.asyncio
async def test_request_run_control_when_resume_is_forced_should_bypass_the_preflight(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch matters as much as the gate: a check that stays red
    for good (a decommissioned server, a spent budget) would otherwise make
    the run permanently unresumable — `paused` with no way out."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _resumable_service(_failing_mise_en_place_report())
    monkeypatch.setattr(service, "_resume_run", AsyncMock())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )

    response = await service.request_run_control(
        run_id=uuid4(), action="resume", force=True, reason="MCP server retired, run must finish"
    )

    assert response.status == "running"
    run_repo.update_status_in_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_run_control_when_resume_is_forced_should_publish_the_bypass_event(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review lot 9 (F1) — the audit trace a forced resume owes, and did not pay.

    `_gate_on_mise_en_place` returns `(report, bypassed)` and the resume path
    DISCARDED both, so overriding a red pre-flight left nothing on the bus.
    The asymmetry is what makes it indefensible: the REFUSAL path inside the
    same function is audited, so a refused resume was traceable and a forced
    one was not — the wrong way round for the only one of the two a human had
    to decide.

    Asserts what the `start_run` twin asserts, field for field: an audit
    consumer must not have to know whether the human overrode a launch or a
    resume to find the override.
    """
    set_correlation_id(str(uuid4()))
    service, _run_repo, _wrepo = _resumable_service(_failing_mise_en_place_report())
    monkeypatch.setattr(service, "_resume_run", AsyncMock())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )

    await service.request_run_control(
        run_id=uuid4(), action="resume", force=True, reason="MCP server retired, run must finish"
    )

    published = {call.args[0]: call.args[1] for call in event_publish_mock.await_args_list}
    assert "workflow_engine.workflow_run.mise_en_place_bypassed" in published
    bypass = published["workflow_engine.workflow_run.mise_en_place_bypassed"]
    assert bypass.reason == "MCP server retired, run must finish"
    assert bypass.failed_checks == ["budget_available"]


@pytest.mark.asyncio
async def test_request_run_control_when_resume_passes_the_preflight_should_publish_no_bypass(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: an audit event that fires when nobody overrode
    anything is worse than none at all."""
    set_correlation_id(str(uuid4()))
    service, _run_repo, _wrepo = _resumable_service(_passing_mise_en_place_report())
    monkeypatch.setattr(service, "_resume_run", AsyncMock())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )

    await service.request_run_control(run_id=uuid4(), action="resume")

    published = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.mise_en_place_bypassed" not in published


# ─── Story 4.12 AC3 — resume_mise_en_place_evaluated fires on every resume ──


@pytest.mark.asyncio
async def test_resume_that_passes_cleanly_still_publishes_its_own_gate_evaluation(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`workflow_runs.mise_en_place` stays the immutable launch record —
    Story 4.12 decided against overwriting it on resume. This event is
    where "which report authorized THIS resume" lives instead, and it must
    fire even when nothing was bypassed (a launch's own passing gate leaves
    no separate audit trail either — the row IS the record — but a resume's
    passing gate has nowhere else to be recorded at all)."""
    set_correlation_id(str(uuid4()))
    service, _run_repo, _wrepo = _resumable_service(_passing_mise_en_place_report())
    monkeypatch.setattr(service, "_resume_run", AsyncMock())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )
    run_id = uuid4()

    await service.request_run_control(run_id=run_id, action="resume")

    published = {call.args[0]: call.args[1] for call in event_publish_mock.await_args_list}
    assert "workflow_engine.workflow_run.resume_mise_en_place_evaluated" in published
    event = published["workflow_engine.workflow_run.resume_mise_en_place_evaluated"]
    assert event.run_id == run_id
    assert event.all_passed is True
    assert event.bypassed is False
    assert event.failed_checks == []


@pytest.mark.asyncio
async def test_resume_that_is_forced_publishes_both_the_bypass_and_the_evaluation_event(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_correlation_id(str(uuid4()))
    service, _run_repo, _wrepo = _resumable_service(_failing_mise_en_place_report())
    monkeypatch.setattr(service, "_resume_run", AsyncMock())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )
    run_id = uuid4()

    await service.request_run_control(
        run_id=run_id, action="resume", force=True, reason="MCP server retired, run must finish"
    )

    published = {call.args[0]: call.args[1] for call in event_publish_mock.await_args_list}
    assert "workflow_engine.workflow_run.mise_en_place_bypassed" in published
    assert "workflow_engine.workflow_run.resume_mise_en_place_evaluated" in published
    evaluation = published["workflow_engine.workflow_run.resume_mise_en_place_evaluated"]
    assert evaluation.run_id == run_id
    assert evaluation.bypassed is True
    assert evaluation.failed_checks == ["budget_available"]


@pytest.mark.asyncio
async def test_pause_and_cancel_never_publish_a_resume_evaluation_event(
    event_publish_mock: AsyncMock,
) -> None:
    """Only `resume` re-runs the Mise en Place gate — `pause`/`cancel` must
    never carry this event at all."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running")

    await service.request_run_control(run_id=uuid4(), action="pause")

    published = [call.args[0] for call in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.resume_mise_en_place_evaluated" not in published


@pytest.mark.asyncio
async def test_request_run_control_when_forcing_without_a_reason_should_be_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror of `start_run`: a bypass with no stated reason defeats the audit
    trail the field exists for (NFR8)."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _resumable_service(_failing_mise_en_place_report())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )

    with pytest.raises(ValidationError):
        await service.request_run_control(run_id=uuid4(), action="resume", force=True, reason="  ")
    run_repo.update_status_in_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_request_run_control_when_not_resuming_should_never_run_the_preflight(
    action: str, event_publish_mock: AsyncMock
) -> None:
    """Neither `pause` nor `cancel` resumes execution, so neither can walk
    into a broken environment — gating them would add latency and a failure
    mode to the two operations an operator reaches for when things are
    ALREADY going wrong."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running")
    service._mise_en_place_service.run_checks = AsyncMock(
        return_value=_failing_mise_en_place_report()
    )

    await service.request_run_control(run_id=uuid4(), action=action)  # type: ignore[arg-type]

    service._mise_en_place_service.run_checks.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_cancelling_over_a_pending_pause_should_escalate(
    event_publish_mock: AsyncMock,
) -> None:
    """ "Pause… no, kill it" is the one operator sequence the strict
    `control_signal IS NULL` guard got wrong. It is also the fix for a worse
    case: a process dying between writing a signal and observing it left the
    run answering 409 to `pause`, `resume` AND `cancel` until the recovery
    sweep — ~25 minutes of a run nobody could control.

    Escalation is terminal-direction only, which the companion test below
    pins."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="running", control_signal="pause")
    run_repo.get_by_id.return_value = run

    response = await service.request_run_control(run_id=run.id, action="cancel")

    assert response.control_signal == "cancel"
    assert run_repo.request_control_in_session.await_args.kwargs["overrides"] == (
        "pause",
        "cancel",
    )
    assert event_publish_mock.await_args.args[0] == "workflow_engine.workflow_run.cancel_requested"


@pytest.mark.asyncio
async def test_request_run_control_when_pausing_should_never_override_a_pending_signal(
    event_publish_mock: AsyncMock,
) -> None:
    """The reverse escalation must stay impossible: a pause revoking a
    cancellation someone was already told they had is exactly what T1.3's
    guard exists to prevent."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running", control_signal="cancel")

    await service.request_run_control(run_id=uuid4(), action="pause")

    assert run_repo.request_control_in_session.await_args.kwargs["overrides"] == ()


@pytest.mark.asyncio
async def test_request_run_control_when_a_signal_is_pending_should_name_it_in_the_conflict(
    event_publish_mock: AsyncMock,
) -> None:
    """A 409 saying only "a control request is already pending" left the
    caller unable to tell a race from a signal nobody will ever observe, or to
    know whether escalating would get through."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running", control_signal="cancel")
    run_repo.request_control_in_session.return_value = 0

    with pytest.raises(ConflictError) as excinfo:
        await service.request_run_control(run_id=uuid4(), action="pause")

    assert excinfo.value.context["pending_control_signal"] == "cancel"
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_losing_a_race_should_describe_the_state_that_is_true_now(
    event_publish_mock: AsyncMock,
) -> None:
    """The snapshot the caller raced AGAINST is the one state the 409 must not
    report. Two concurrent `pause`s produced `pending_control_signal: null` —
    "nothing is pending" — while a pause was, which is the single question
    this body exists to answer."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    # Read: no signal yet. Written by a concurrent caller a moment later.
    run_repo.get_by_id.return_value = _workflow_run(status="running", control_signal=None)
    run_repo.get_by_id_in_session = AsyncMock(
        return_value=_workflow_run(status="running", control_signal="pause")
    )
    run_repo.request_control_in_session.return_value = 0

    with pytest.raises(ConflictError) as excinfo:
        await service.request_run_control(run_id=uuid4(), action="pause")

    assert excinfo.value.context["pending_control_signal"] == "pause"
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_the_run_finished_mid_flight_should_report_its_real_status(
    event_publish_mock: AsyncMock,
) -> None:
    """Same defect on the other field: a 409 announcing `current_status:
    "running"` for a row that reads `completed` invites the client to retry a
    transition that can never succeed."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running")
    run_repo.get_by_id_in_session = AsyncMock(return_value=_workflow_run(status="completed"))
    run_repo.request_control_in_session.return_value = 0

    with pytest.raises(ConflictError) as excinfo:
        await service.request_run_control(run_id=uuid4(), action="cancel")

    assert excinfo.value.context["current_status"] == "completed"


@pytest.mark.asyncio
async def test_observe_control_when_the_run_was_already_paused_should_accumulate_its_duration(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`started_at` is the start of THIS execution segment, so a run paused,
    resumed and cancelled reported only its last few seconds. An API-driven
    pause/resume cycle could reset the number at will — before this story it
    took a crash to see it."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    state = {"node_outputs": {"a": {}}, "node_metrics": {}}
    compiled = _FakeCompiledGraph(updates=[], final_state=state, restored_state=state)
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(return_value="cancel")
    # 400 s already executed before the run was paused.
    run_repo.get_by_id.return_value = _workflow_run(
        status="running", metrics={"total_duration_ms": 400_000}
    )

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    metrics = run_repo.update_status.await_args.kwargs["metrics"]
    assert metrics["total_duration_ms"] >= 400_000


@pytest.mark.asyncio
async def test_request_run_control_when_resume_dependencies_are_gone_should_not_move_the_row(
    event_publish_mock: AsyncMock,
) -> None:
    """The workflow can be deleted while a run sits paused. Loading it AFTER
    the `paused → running` write left the caller a 404 on a row that was
    already `running` with no driver behind it — unpausable (nobody observes
    the signal), unresumable (`resume` from `running` is a 409), and stuck
    until the recovery sweep. Nothing may move before the reload succeeds."""
    set_correlation_id(str(uuid4()))
    service, run_repo, workflow_repo = _control_service()
    run = _workflow_run(status="paused")
    run_repo.get_by_id.return_value = run
    workflow_repo.require_by_id.side_effect = NotFoundError(detail="workflow gone")

    with pytest.raises(NotFoundError):
        await service.request_run_control(run_id=run.id, action="resume")

    run_repo.update_status_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_run_control_when_cancelling_a_paused_run_should_report_executed_time_only(
    event_publish_mock: AsyncMock,
) -> None:
    """`total_duration_ms` must be the run's EXECUTED time, taken from the
    metrics the driver aggregated when it paused — never `now() -
    run.started_at`, which folds in the whole idle window (a run paused on
    Monday and cancelled on Thursday reported three days) and contradicts
    `_execute`'s own invariant that `started_at` is a segment start."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="paused", metrics={"total_duration_ms": 4_200})
    # Paused long ago — wall-clock would dwarf the real executed time.
    run.started_at = datetime.now(UTC) - timedelta(days=3)
    run_repo.get_by_id.return_value = run

    await service.request_run_control(run_id=run.id, action="cancel")

    event = event_publish_mock.await_args.args[1]
    assert event.total_duration_ms == 4_200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "status", "method"),
    [
        ("pause", "running", "request_control_in_session"),
        ("cancel", "running", "request_control_in_session"),
        ("cancel", "paused", "update_status_in_session"),
        ("resume", "paused", "update_status_in_session"),
    ],
)
async def test_request_run_control_when_transition_is_legal_should_write_in_the_publish_session(
    action: str,
    status: str,
    method: str,
    event_publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 — "un event d'audit est publié dans la même transaction que
    l'écriture". The only way to hold that is for BOTH to run against one
    session: the self-managed repo wrappers commit on exit, so a publish that
    failed afterwards would leave a moved row with no event (SSE clients hung,
    audit silent), and a resume would answer 500 on a row already `running`.

    Asserting the session OBJECT is the same is what makes this a real
    guarantee rather than a naming convention."""
    set_correlation_id(str(uuid4()))
    service, run_repo, workflow_repo = _control_service()
    run = _workflow_run(status=status)
    run_repo.get_by_id.return_value = run
    workflow_repo.require_by_id.return_value = _workflow()
    monkeypatch.setattr(service, "_resume_run", AsyncMock())
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.service._load_templates",
        AsyncMock(return_value={}),
    )

    await service.request_run_control(run_id=run.id, action=action)  # type: ignore[arg-type]

    write_session = getattr(run_repo, method).await_args.args[0]
    assert event_publish_mock.await_args.kwargs["session"] is write_session


# ─── Story 4.9 AC2/T2.3 — concurrency cap on resume ────────────────────


@pytest.mark.asyncio
async def test_resume_over_the_concurrency_cap_is_refused_before_mise_en_place(
    event_publish_mock: AsyncMock,
) -> None:
    """The early check (before the Mise en Place gate) refuses a resume
    whose workflow is already at capacity, without paying for MCP probes or
    a Dry Run first."""
    set_correlation_id(str(uuid4()))
    service, run_repo, workflow_repo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="paused")
    workflow_repo.require_by_id.return_value = _workflow()
    run_repo.count_running = AsyncMock(return_value=20)

    with pytest.raises(RateLimitError):
        await service.request_run_control(run_id=uuid4(), action="resume")

    service._mise_en_place_service.run_checks.assert_not_called()
    run_repo.update_status_in_session.assert_not_awaited()
    event_publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_under_the_concurrency_cap_proceeds(
    event_publish_mock: AsyncMock,
) -> None:
    set_correlation_id(str(uuid4()))
    service, run_repo, workflow_repo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="paused")
    workflow_repo.require_by_id.return_value = _workflow()
    run_repo.count_running = AsyncMock(return_value=19)
    run_repo.count_running_in_session = AsyncMock(return_value=19)

    response = await service.request_run_control(run_id=uuid4(), action="resume")

    assert response.status == "running"


@pytest.mark.asyncio
async def test_resume_over_the_concurrency_cap_via_the_authoritative_recount_is_refused(
    event_publish_mock: AsyncMock,
) -> None:
    """The early check is racy by construction — it is the in-session
    recount, taken in the same transaction as the write, that the cap
    actually rests on. Its `start_run` twin was covered; the `resume` branch
    was not, so only the cheap check was ever exercised on this path."""
    set_correlation_id(str(uuid4()))
    service, run_repo, workflow_repo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="paused")
    workflow_repo.require_by_id.return_value = _workflow()
    # Passes the early check (a stale, racy count) and fails the recount.
    run_repo.count_running = AsyncMock(return_value=0)
    run_repo.count_running_in_session = AsyncMock(return_value=20)

    with pytest.raises(RateLimitError):
        await service.request_run_control(run_id=uuid4(), action="resume")


# ─── Story 4.9 AC6/T6.5 — retracting a pending control request ────────


@pytest.mark.asyncio
async def test_retract_clears_a_pending_signal_and_records_the_withdrawal(
    event_publish_mock: AsyncMock,
) -> None:
    """The `pause_requested`/`cancel_requested` a retraction undoes is a
    durable outbox row. Leaving the withdrawal unrecorded makes the bus say
    a cancellation was asked for on a run that then completed normally, with
    nothing saying who took it back — and the SSE `state` frame losing its
    `control_signal` only reaches a client connected at that instant."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run = _workflow_run(status="running", control_signal="pause")
    run_repo.get_by_id.return_value = run
    run_repo.retract_control_in_session = AsyncMock(return_value=1)

    response = await service.request_run_control(run_id=run.id, action="retract")

    assert response.status == "running"
    assert response.control_signal is None
    run_repo.retract_control_in_session.assert_awaited_once()
    _kwargs = run_repo.retract_control_in_session.await_args.kwargs
    assert _kwargs["only_if_status"] == "running"

    event_type = event_publish_mock.await_args.args[0]
    assert event_type == "workflow_engine.workflow_run.control_retracted"
    published = event_publish_mock.await_args.args[1]
    # WHICH request was withdrawn — the subject of the event is the request
    # being undone, not the `retract` action itself.
    assert published.retracted_signal == "pause"


@pytest.mark.asyncio
async def test_retract_with_nothing_pending_is_a_conflict() -> None:
    """The repo's `control_signal IS NOT NULL` guard failed — nothing was
    pending (already observed, or never requested). Same 409 shape as every
    other lost race on this surface."""
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running", control_signal=None)
    run_repo.retract_control_in_session = AsyncMock(return_value=0)

    with pytest.raises(ConflictError):
        await service.request_run_control(run_id=uuid4(), action="retract")


@pytest.mark.asyncio
async def test_retract_from_a_non_running_status_is_a_conflict() -> None:
    set_correlation_id(str(uuid4()))
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="paused")

    with pytest.raises(ConflictError):
        await service.request_run_control(run_id=uuid4(), action="retract")


# ─── Story 4.6 — cooperative control observation in _execute (AC2) ───


@pytest.mark.asyncio
async def test_execute_when_pause_signal_is_observed_should_pause_and_never_complete(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(
        dag={
            "nodes": [
                {"node_id": "a", "agent_template_id": str(uuid4())},
                {"node_id": "b", "agent_template_id": str(uuid4())},
            ],
            "edges": [{"from_node_id": "a", "to_node_id": "b"}],
        }
    )
    compiled = _FakeCompiledGraph(
        updates=[
            {"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}},
            {"b": {"node_outputs": {"b": {}}, "node_metrics": {"b": {"duration_ms": 1}}}},
        ],
        final_state={"node_outputs": {"a": {}, "b": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    # `None` before the loop, `"pause"` at the first superstep boundary.
    run_repo.get_control_signal = AsyncMock(side_effect=[None, "pause"])

    run_id = uuid4()
    templates = {
        "a": SimpleNamespace(config={}, id=uuid4()),
        "b": SimpleNamespace(config={}, id=uuid4()),
    }
    await service._drive_run(run_id, workflow, templates, {}, correlation_id=uuid4())

    statuses = [c.kwargs["status"] for c in run_repo.update_status.await_args_list]
    assert "paused" in statuses
    assert "completed" not in statuses
    paused_call = next(
        c for c in run_repo.update_status.await_args_list if c.kwargs["status"] == "paused"
    )
    assert paused_call.kwargs["only_if_status"] == "running"
    assert paused_call.kwargs["clear_control"] is True
    # A paused run is NOT over — `ended_at` must stay NULL.
    assert paused_call.kwargs.get("ended_at") is None

    published = [c.args[0] for c in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.paused" in published
    assert "workflow_engine.workflow_run.completed" not in published


@pytest.mark.asyncio
async def test_execute_when_cancel_signal_is_observed_should_mark_cancelled_with_ended_at(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[
            {
                "a": {
                    "node_outputs": {"a": {}},
                    "node_metrics": {
                        "a": {"duration_ms": 5, "input_tokens": 7, "output_tokens": 2}
                    },
                }
            }
        ],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(side_effect=[None, "cancel"])

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    cancelled_call = next(
        c for c in run_repo.update_status.await_args_list if c.kwargs["status"] == "cancelled"
    )
    assert cancelled_call.kwargs["ended_at"] is not None
    assert cancelled_call.kwargs["clear_control"] is True
    # Partial spend is aggregated, never dropped (mirror `_mark_failed`).
    assert cancelled_call.kwargs["metrics"]["total_tokens"] == {"input": 7, "output": 2}

    published = [c.args[0] for c in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.cancelled" in published
    assert "workflow_engine.workflow_run.completed" not in published


@pytest.mark.asyncio
async def test_execute_when_signal_predates_the_loop_should_execute_zero_nodes(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery-worker case: a run resumed while a pause request had
    never been observed must settle without running a single node."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(return_value="pause")

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    # No superstep was ever consumed — no `step_completed`, no completion.
    published = [c.args[0] for c in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.step_completed" not in published
    assert "workflow_engine.workflow_run.paused" in published
    assert run_repo.update_checkpoint.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("raise_after_updates", [False, True])
async def test_execute_when_run_reaches_a_terminal_status_should_consume_any_pending_signal(
    raise_after_updates: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control request written while the LAST superstep is still running is
    never observed — the run reaches `END` (or dies) first. `request_control`
    and `_mark_completed`/`_mark_failed` are both compare-and-set on
    `running`, so they interleave without conflicting, and nothing else
    cleared the column: the row settled `completed` while still advertising
    `"pause"`, which the SSE `state` frame then reported forever."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
        raise_after_updates=raise_after_updates,
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    terminal_call = run_repo.update_status.await_args
    assert terminal_call.kwargs["status"] == ("error" if raise_after_updates else "completed")
    assert terminal_call.kwargs["clear_control"] is True


@pytest.mark.asyncio
async def test_execute_when_control_settles_should_close_the_astream_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaking out of `astream` leaves the generator suspended inside
    LangGraph's Pregel loop, holding a checkpointer context and its own
    background tasks. The two pre-existing exits (stream exhausted, exception)
    finalise it themselves; the `break` this story added does not, so it must
    close it explicitly rather than leave it to the GC."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    # Nothing pending before the loop, `pause` at the end of the first
    # superstep — the only sequence that actually exercises the `break`. A
    # signal present from the start returns BEFORE `astream` is ever called.
    run_repo.get_control_signal = AsyncMock(side_effect=[None, "pause"])

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    assert compiled.stream_closed is True


@pytest.mark.asyncio
async def test_execute_when_cancel_predates_the_loop_should_still_account_the_work_already_paid(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-loop branch settles runs the recovery worker picked up — runs
    that have ALREADY executed and been billed. It used to hand
    `_observe_control` an empty state, so the cancellation of a run that had
    burned real money was recorded as free: `per_node` empty,
    `total_cost_usd` null, `cancelled_at_node_id` null. The state is right
    there in the checkpoint; not reading it was the bug."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    already_run = {
        "node_outputs": {"a": {}},
        "node_metrics": {"a": {"duration_ms": 5, "input_tokens": 11, "cost_usd": "0.002"}},
    }
    compiled = _FakeCompiledGraph(updates=[], final_state=already_run, restored_state=already_run)
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(return_value="cancel")

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    status_call = run_repo.update_status.await_args
    assert status_call.kwargs["status"] == "cancelled"
    metrics = status_call.kwargs["metrics"]
    assert metrics["total_cost_usd"] == "0.002"
    assert metrics["per_node"]["a"]["input_tokens"] == 11
    # …and the event names where it stopped, instead of `None`.
    event = event_publish_mock.await_args.args[1]
    assert event.cancelled_at_node_id == "a"


@pytest.mark.asyncio
async def test_execute_when_cancel_metrics_are_corrupt_should_still_cancel_not_fail(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`node_metrics` is free-form graph state, and `_aggregate_metrics` does
    `Decimal(str(cost_usd))` on it. Unguarded, one corrupt entry raised into
    `_execute`'s handler and `_mark_failed_safely` rewrote the run as
    `error` — a user's cancellation DESTROYING the run instead of stopping
    it. The per-node breakdown may be lost; the cancellation may not."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    corrupt = {"node_outputs": {"a": {}}, "node_metrics": {"a": {"cost_usd": "not-a-decimal"}}}
    compiled = _FakeCompiledGraph(updates=[], final_state=corrupt, restored_state=corrupt)
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(return_value="cancel")

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    assert run_repo.update_status.await_args.kwargs["status"] == "cancelled"
    published = [c.args[0] for c in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.cancelled" in published
    assert "workflow_engine.workflow_run.failed" not in published


@pytest.mark.asyncio
async def test_execute_when_control_read_fails_should_let_the_run_finish(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort on the READ: a DB hiccup on `get_control_signal` must not
    kill an otherwise healthy run (mirror `_sync_checkpoint_safely`)."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(side_effect=RuntimeError("db down"))

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    statuses = [c.kwargs["status"] for c in run_repo.update_status.await_args_list]
    assert statuses == ["completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signal", "settled_status"), [("pause", "paused"), ("cancel", "cancelled")]
)
async def test_execute_when_settle_cas_loses_should_keep_running_without_event(
    signal: str,
    settled_status: str,
    event_publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rowcount 0 on the settle CAS means the row moved under the driver —
    somebody else closed the run, or the signal was escalated. Either way it
    must not publish a transition the row contradicts.

    Parametrized over BOTH signals by the review (lot 12): only the `pause`
    branch was covered, and the `cancel` one matters more — returning `None`
    there sends `_execute` on to `_mark_completed`, so a run whose cancel CAS
    lost then attempts a `completed` transition. That second write is itself
    guarded on `running`, so it no-ops; nothing proved it."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(side_effect=[None, signal])
    run_repo.update_status = AsyncMock(return_value=0)

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    published = [c.args[0] for c in event_publish_mock.await_args_list]
    assert f"workflow_engine.workflow_run.{settled_status}" not in published
    # …and the run was not closed some other way either: every write lost its
    # compare-and-set, so no terminal event may be announced at all.
    assert "workflow_engine.workflow_run.completed" not in published
    assert "workflow_engine.workflow_run.failed" not in published


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("signal", "settled_status"), [("pause", "paused"), ("cancel", "cancelled")]
)
async def test_execute_settle_write_is_guarded_on_the_signal_it_read(
    signal: str,
    settled_status: str,
    event_publish_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review lot 9 (F2) — the guard that closes the escalation race.

    Two DB round-trips separate the read of `control_signal` from the settle
    write, and `status` stays `running` across both, so `only_if_status`
    alone cannot notice the signal changing underneath. Without the second
    predicate, a `cancel` escalating over this very `pause` is answered 202
    and then ERASED by `clear_control=True`.
    """
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(side_effect=[None, signal])

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    settle = next(
        call
        for call in run_repo.update_status.await_args_list
        if call.kwargs.get("status") == settled_status
    )
    assert settle.kwargs["only_if_control_signal"] == signal
    assert settle.kwargs["only_if_status"] == "running"
    assert settle.kwargs["clear_control"] is True


@pytest.mark.asyncio
async def test_execute_when_a_cancel_escalates_over_the_pause_being_settled(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What happens once the guard BITES — not the guard itself.

    The sibling above pins the predicate (and fails without it). This one
    starts from its outcome, a rowcount of 0 on the pause settle, which it
    manufactures directly: so it would pass with or without
    `only_if_control_signal`, and it is not a regression test for F2. What
    it does pin is the branch the Edge Case Hunter found uncovered — the
    driver must NOT publish a `paused` the row contradicts, and must NOT
    treat the run as settled. Returning to the loop is what lets it observe
    the escalated cancel at the next boundary and honour the 202 the
    operator already holds; settling here instead would leave the run
    `paused` with no pending signal — nothing for anyone to observe, and a
    `cancel_requested` on the bus that no `cancelled` ever follows.
    """
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(side_effect=[None, "pause"])

    # The pause settle loses (the signal is no longer `pause`); everything
    # else still writes.
    async def _update_status(_run_id: Any, **kwargs: Any) -> int:
        return 0 if kwargs.get("status") == "paused" else 1

    run_repo.update_status = AsyncMock(side_effect=_update_status)

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    published = [c.args[0] for c in event_publish_mock.await_args_list]
    assert "workflow_engine.workflow_run.paused" not in published
    # The driver went back to its loop rather than stopping: the graph ran to
    # the end, so the completion path — not the pause path — closed the run.
    assert "workflow_engine.workflow_run.completed" in published


@pytest.mark.asyncio
async def test_execute_when_control_signal_is_unrecognised_should_keep_running(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`control_signal` is a free `VARCHAR(20)`: a value from a future story
    or a manual edit must be IGNORED, not guessed at — and must not take the
    run down on its way past.

    This also pins the logging of that value. The driver reports the
    unexpected signal, and it must report it COERCED: handing a raw object
    to a structured-log renderer sent it into infinite recursion (a
    `MagicMock` grows a fresh child on every attribute access), which
    surfaced as `RecursionError` swallowed into `checkpoint.last_error` —
    and only once an integration test had installed the JSON renderer, so
    the unit suite alone never saw it.
    """
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_control_signal = AsyncMock(return_value="pausing")

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    statuses = [c.kwargs["status"] for c in run_repo.update_status.await_args_list]
    assert statuses == ["completed"]


@pytest.mark.asyncio
async def test_execute_when_control_repo_is_an_unconfigured_mock_should_still_complete(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shape every OTHER `_drive_run` test in this module uses: a
    bare `AsyncMock` repo whose `get_control_signal` was never configured,
    so it answers with a `MagicMock`.

    Kept as its own test rather than left implicit in the others, because
    that implicit coverage is what failed silently: the value reached the
    logger, the logger recursed, and eighteen unrelated tests started
    failing with assertions that had nothing to do with the cause.
    """
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 1}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, _wrepo, run_repo, _trepo = _make_execution_service()

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    statuses = [c.kwargs["status"] for c in run_repo.update_status.await_args_list]
    assert statuses == ["completed"]


# ─── Story 4.6 AC3 / review lot 12 — bounding and redacting the breakdown ──
#
# `_failure_attempts` is persisted in `workflow_runs.checkpoint` AND streamed
# to SSE clients. Its three safety properties — redaction, at most
# `_MAX_PERSISTED_ATTEMPTS` entries, at most `_ATTEMPT_FIELD_MAX_CHARS` per
# field — had no assertion anywhere: the only reader in the suite checked
# that a 2-entry list carried the right provider names (review lot 12).


def _failed_with(attempts: Any) -> Exception:
    from agentive_backend.shared.llm.exceptions import LLMAllProvidersFailedError

    exc = LLMAllProvidersFailedError(detail="all providers failed")
    exc.context["attempts"] = attempts
    return exc


def test_failure_attempts_redacts_secrets_a_provider_leaked_into_the_detail() -> None:
    """NFR9. The router redacts on its way in; this re-redacts rather than
    trusting that, because the value is about to be written to a column and
    pushed to every SSE subscriber. The cost of re-running a regex is nothing
    against the cost of leaking a DSN."""
    from agentive_backend.features.workflow_engine.service import _failure_attempts

    leaked = "connect failed: postgresql://agentive_app:hunter2@db:5432/agentive"
    [entry] = _failure_attempts(_failed_with([{"provider": "openai", "error_detail": leaked}]))

    assert "hunter2" not in entry["error_detail"]
    assert entry["provider"] == "openai"


def test_failure_attempts_caps_the_number_of_entries() -> None:
    """A chain is at most two providers, but this reads a free JSONB context
    off an exception — a malformed or hostile one is not bounded by that."""
    from agentive_backend.features.workflow_engine.service import (
        _MAX_PERSISTED_ATTEMPTS,
        _failure_attempts,
    )

    kept = _failure_attempts(_failed_with([{"provider": f"p{i}"} for i in range(50)]))

    assert len(kept) == _MAX_PERSISTED_ATTEMPTS == 5
    # The FIRST attempts are kept — the earliest failure is the one that
    # explains the chain, the last is just the final symptom.
    assert kept[0]["provider"] == "p0"


def test_failure_attempts_truncates_an_unbounded_field() -> None:
    """`error_detail` comes from a provider's response body. Unbounded, one
    node's failure could push a megabyte into the checkpoint column and into
    every open SSE stream."""
    from agentive_backend.features.workflow_engine.service import (
        _ATTEMPT_FIELD_MAX_CHARS,
        _failure_attempts,
    )

    [entry] = _failure_attempts(
        _failed_with([{"provider": "openai", "error_detail": "x" * 10_000}])
    )

    assert len(entry["error_detail"]) == _ATTEMPT_FIELD_MAX_CHARS == 500


@pytest.mark.parametrize(
    "attempts",
    [
        "not-a-list",
        None,
        [{"provider": "openai"}, "not-a-dict", 42],
    ],
)
def test_failure_attempts_survives_a_context_that_is_not_the_expected_shape(attempts: Any) -> None:
    """`context` is a free dict on an exception that may not be the router's.
    Raising here would replace a real failure with a confusing one, on the
    path that is already handling a failure."""
    from agentive_backend.features.workflow_engine.service import _failure_attempts

    result = _failure_attempts(_failed_with(attempts))

    assert isinstance(result, list)
    assert all(isinstance(entry, dict) for entry in result)


# ─── Revue 2026-09-12 — `total_duration_ms` après une pause ────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_terminal_write_carries_the_duration_billed_before_the_pause(
    outcome: str, event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Triple convergence of the 2026-09-12 review.

    `started_at` is the start of THIS execution segment in both drivers —
    deliberately, so a crash gap is not billed as execution time. Only the
    pause/cancel path carried the earlier total forward, so a run that ran
    ten minutes, paused, then finished in two seconds persisted
    `total_duration_ms = 2000`: a number that goes DOWN, and that contradicts
    the `per_node` map written beside it in the same JSONB (LangGraph's
    `node_metrics` channel accumulates across the pause, so the parts summed
    to ~600 s while the whole said 2 s). Comparability between the two is the
    stated point of the field.
    """
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    if outcome == "completed":
        compiled = _FakeCompiledGraph(
            updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 5}}}}],
            final_state={"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 5}}},
        )
    else:
        compiled = _FakeCompiledGraph(
            updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 5}}}}],
            final_state={"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 5}}},
            raise_after_updates=True,
            raise_exc=RuntimeError("node blew up"),
        )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    # What a previous segment already billed, as persisted on the row.
    run_repo.get_by_id.return_value = _workflow_run(
        status="running", metrics={"total_duration_ms": 600_000}
    )

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    terminal = next(
        call
        for call in run_repo.update_status.await_args_list
        if call.kwargs.get("status") in ("completed", "error")
    )
    assert terminal.kwargs["metrics"]["total_duration_ms"] >= 600_000


@pytest.mark.asyncio
async def test_terminal_write_is_unchanged_for_a_run_that_never_paused(
    event_publish_mock: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accumulation must not change Story 4.2's crash-resume semantics.

    A run that never paused persisted no segment total, so
    `_prior_duration_ms` returns 0 and the number stays what it always was.
    That is what keeps this fix contained to the case it is about."""
    workflow = _workflow(
        dag={"nodes": [{"node_id": "a", "agent_template_id": str(uuid4())}], "edges": []}
    )
    compiled = _FakeCompiledGraph(
        updates=[{"a": {"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 5}}}}],
        final_state={"node_outputs": {"a": {}}, "node_metrics": {"a": {"duration_ms": 5}}},
    )
    _patch_build_state_graph(monkeypatch, compiled)
    service, run_repo, _wrepo = _control_service()
    run_repo.get_by_id.return_value = _workflow_run(status="running", metrics={})

    await service._drive_run(
        uuid4(), workflow, {"a": SimpleNamespace(config={}, id=uuid4())}, {}, correlation_id=uuid4()
    )

    terminal = next(
        call
        for call in run_repo.update_status.await_args_list
        if call.kwargs.get("status") == "completed"
    )
    # Seconds, not ten minutes: nothing was carried forward.
    assert terminal.kwargs["metrics"]["total_duration_ms"] < 60_000


def test_handoff_settings_are_none_when_the_kill_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I-04 — `None` is the deployment kill switch, and it is the path
    `build_state_graph`/`execute_agent_node` already default to: no new
    branch, the pre-4.7 behaviour byte for byte.

    AC2 makes summarization the default, which silently changes the prompt of
    every template already in production — one reading
    `upstream_outputs["a"]["invoice_id"]` stops finding that field, with no
    change to its own config and no template versioning to roll back to. The
    per-template opt-out exists but is per-template: recovering from a bad
    rollout meant editing every consumer under incident.
    """
    import agentive_backend.features.workflow_engine.service as svc_module

    monkeypatch.setattr(svc_module.settings, "workflow_handoff_summary_enabled", False)
    built = (
        svc_module.HandoffSettings(
            model=svc_module.settings.workflow_handoff_summary_model,
            max_tokens=svc_module.settings.workflow_handoff_summary_max_tokens,
            timeout_s=svc_module.settings.workflow_handoff_summary_timeout_s,
        )
        if svc_module.settings.workflow_handoff_summary_enabled
        else None
    )
    assert built is None

    monkeypatch.setattr(svc_module.settings, "workflow_handoff_summary_enabled", True)
    assert svc_module.settings.workflow_handoff_summary_enabled is True
