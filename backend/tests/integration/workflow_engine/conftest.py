"""Integration fixtures for ``features.workflow_engine`` (Story 4.1, 4.2).

Reuses the shared ``make_e2e_app`` (Postgres bootstrap + agents/tools/
playground/memory/workflows routers — Story 4.1 extended it with the
workflows router).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.config import settings
from agentive_backend.shared.event_bus import OutboxWorker
from tests.integration.agent_registry.conftest import (  # noqa: F401
    E2E_AUTH_TOKEN,
    app_session_factory,
    audit_admin_session_factory,
    e2e_auth_headers,
    make_e2e_app,
    migrated_db,
    owner_session_factory,
    roles_provisioned,
    seed_session_factory,
)


def _workflow_checkpoint_dsn() -> str:
    """Mirror ``app.lifespan._workflow_checkpoint_dsn`` exactly — the
    ``agentive_app`` role DSN. ``settings`` is mutated in-place by the
    session-scoped ``migrated_db`` fixture to point at the testcontainer,
    so this resolves correctly for the duration of the test session.
    """
    return settings.psycopg_dsn


@pytest.fixture
async def workflow_checkpointer(migrated_db: str) -> AsyncIterator[AsyncPostgresSaver]:  # noqa: F811
    """Real :class:`AsyncPostgresSaver` against the migrated testcontainer.

    NO ``.setup()`` call here — mirror T9.2's production posture: the T1.2
    migration already created ``checkpoints``/``checkpoint_writes``/
    ``checkpoint_blobs`` (as ``agentive_owner``) and granted DML to
    ``agentive_app``, which is exactly the role this DSN connects as.
    """
    async with AsyncPostgresSaver.from_conn_string(_workflow_checkpoint_dsn()) as saver:
        yield saver


@pytest.fixture
async def outbox_worker(
    app_session_factory: async_sessionmaker[AsyncSession],  # noqa: F811
) -> AsyncIterator[OutboxWorker]:
    """A running :class:`OutboxWorker` — required for the SSE endpoint's
    in-process ``subscribe()`` handlers to ever fire (T10.6).

    ``publish()`` only INSERTs into ``outbox_events``; dispatch to
    subscribers happens exclusively via a running ``OutboxWorker``'s
    LISTEN/NOTIFY loop (``shared/event_bus/outbox.py``), which
    ``make_e2e_app`` does NOT start (unlike the full ``app.lifespan``).
    Mirror ``tests/integration/event_bus/conftest.py``'s ``outbox_worker``
    fixture — faster poll interval than production so a missed NOTIFY still
    resolves quickly.
    """
    worker = OutboxWorker(session_factory=app_session_factory, poll_interval_s=0.5)
    await worker.start()
    yield worker
    await worker.stop()


class _AlwaysPassMiseEnPlaceService:
    """Stub :class:`~.mise_en_place.MiseEnPlaceService` for e2e tests that
    exercise execution/recovery/routing machinery unrelated to Story 4.5 —
    always returns an all-passing report so ``start_run`` behaves exactly as
    it did before that story landed (no real MCP/namespace/budget/provider
    I/O). Story 4.5's own e2e coverage
    (``tests/integration/workflow_engine/test_mise_en_place_e2e.py``) wires
    the real :class:`~.mise_en_place.MiseEnPlaceService` instead.
    """

    async def run_checks(self, **_kwargs: Any) -> Any:
        from agentive_backend.features.workflow_engine.domain.mise_en_place import (
            CHECK_CODES,
            CheckResult,
            build_report,
        )

        return build_report(
            [
                CheckResult(code=code, passed=True, detail="stubbed for integration test")
                for code in CHECK_CODES
            ]
        )


def always_pass_mise_en_place_service() -> _AlwaysPassMiseEnPlaceService:
    return _AlwaysPassMiseEnPlaceService()


def wire_execution_service(app: Any) -> None:
    """Mirror ``app.lifespan``'s T9.3 wiring on a hand-built test app.

    The lifespan builds ONE :class:`WorkflowExecutionService` and shares it
    with both the router and the recovery worker; ``_build_execution_service``
    reads it from ``app.state`` rather than rebuilding a service and three
    repos per request (Story 4.2 review, finding #37). These e2e tests bypass
    the lifespan, so they must do the same wiring themselves — a fixture that
    sets only the raw resources would leave the endpoint returning 503.

    Call AFTER ``session_factory``/``workflow_checkpointer``/``llm_router``
    are on ``app.state``.
    """
    from agentive_backend.features.workflow_engine.routing_catalog import load_routing_rules
    from agentive_backend.features.workflow_engine.service import WorkflowExecutionService
    from agentive_backend.shared.repositories import (
        AgentTemplateRepo,
        AgentTemplateToolRepo,
        WorkflowRepo,
        WorkflowRunRepo,
    )

    session_factory = app.state.session_factory
    # Story 4.3 T9.1 — mirror `app.lifespan`'s catalog load on this hand-built
    # test app too, so `_build_execution_service`'s dependency-diagnostics
    # list stays meaningful and a decision-point workflow actually routes.
    app.state.routing_rules = load_routing_rules()
    app.state.workflow_execution_service = WorkflowExecutionService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        workflow_run_repo=WorkflowRunRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
        tool_hub_repo=AgentTemplateToolRepo(session_factory=session_factory),
        llm_router=app.state.llm_router,
        checkpointer=app.state.workflow_checkpointer,
        routing_rules=app.state.routing_rules,
        mise_en_place_service=always_pass_mise_en_place_service(),
    )


def wire_execution_service_with_real_mise_en_place(
    app: Any,
    *,
    tool_ping_timeout_s: float = 2.0,
    budget_cap_usd: Decimal | None = None,
) -> None:
    """Mirror ``app.lifespan``'s Story 4.5 wiring (T7.1) on a hand-built
    test app — the REAL :class:`MiseEnPlaceService`, not
    :func:`wire_execution_service`'s always-pass stub.

    Vit ici depuis la Story 5.1 : `test_dev_lead_e2e.py` en a besoin pour
    prouver qu'un namespace manquant FAIT REFUSER le lancement — la raison
    d'être de l'ordre « namespaces avant templates » du provisioning. Le
    dupliquer aurait donné deux câblages à garder d'accord avec
    `app.lifespan`.

    Call AFTER ``session_factory``/``workflow_checkpointer``/``llm_router``
    are on ``app.state`` (same precondition as ``.conftest.wire_execution_service``).
    """
    from agentive_backend.features.workflow_engine.dry_run import DryRunService, DryRunSettings
    from agentive_backend.features.workflow_engine.mise_en_place import (
        MiseEnPlaceService,
        MiseEnPlaceSettings,
    )
    from agentive_backend.features.workflow_engine.routing_catalog import load_routing_rules
    from agentive_backend.features.workflow_engine.service import WorkflowExecutionService
    from agentive_backend.shared.repositories import (
        AgentTemplateRepo,
        AgentTemplateToolRepo,
        NamespaceRepo,
        ToolServerRepo,
        WorkflowRepo,
        WorkflowRunRepo,
    )

    session_factory = app.state.session_factory
    app.state.routing_rules = load_routing_rules()

    dry_run_service = DryRunService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        workflow_run_repo=WorkflowRunRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
        settings=DryRunSettings(
            history_limit=20,
            fallback_input_tokens=500,
            fallback_output_tokens=500,
            budget_cap_usd=budget_cap_usd,
        ),
    )
    mise_en_place_service = MiseEnPlaceService(
        template_tool_repo=AgentTemplateToolRepo(session_factory=session_factory),
        tool_server_repo=ToolServerRepo(session_factory=session_factory),
        namespace_repo=NamespaceRepo(session_factory=session_factory),
        dry_run_service=dry_run_service,
        settings=MiseEnPlaceSettings(
            tool_ping_timeout_s=tool_ping_timeout_s,
            check_timeout_s=15.0,
            budget_cap_usd=budget_cap_usd,
            # Fixed True — this test suite is about `mcp_tools_reachable`
            # and the bypass/no-op paths, not about provider-key presence
            # (covered by `test_mise_en_place_service.py`'s unit tests).
            anthropic_api_key_present=True,
            openai_api_key_present=True,
        ),
    )
    app.state.workflow_execution_service = WorkflowExecutionService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        workflow_run_repo=WorkflowRunRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
        tool_hub_repo=AgentTemplateToolRepo(session_factory=session_factory),
        llm_router=app.state.llm_router,
        checkpointer=app.state.workflow_checkpointer,
        routing_rules=app.state.routing_rules,
        mise_en_place_service=mise_en_place_service,
    )
