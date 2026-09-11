"""Integration fixtures for ``features.workflow_engine`` (Story 4.1, 4.2).

Reuses the shared ``make_e2e_app`` (Postgres bootstrap + agents/tools/
playground/memory/workflows routers — Story 4.1 extended it with the
workflows router).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
        llm_router=app.state.llm_router,
        checkpointer=app.state.workflow_checkpointer,
        routing_rules=app.state.routing_rules,
    )
