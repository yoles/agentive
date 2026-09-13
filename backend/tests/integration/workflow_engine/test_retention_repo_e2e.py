"""Integration tests — Story 4.10 AC1/AC2/AC3 repo methods.

Real Postgres (``migrated_db``), the same posture as
``test_recovery_claim_e2e.py``: these behaviors live entirely in SQL
(a partial/expression index actually being chosen by the planner, a
compare-and-set purge marker), so they are not meaningfully mockable.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import WorkflowRunRepo

pytestmark = pytest.mark.integration

SeedRun = Callable[..., Awaitable[UUID]]


@pytest.fixture
async def seed_run(
    migrated_db: str,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[SeedRun]:
    """Insert a ``workflows`` + ``workflow_runs`` pair with explicit control
    over ``status``/``ended_at``/``last_checkpoint_at``/``checkpoint_purged_at``
    — mirror ``test_recovery_claim_e2e.py``'s ``seed_run`` fixture."""
    workflow_ids: list[UUID] = []
    run_ids: list[UUID] = []

    async def _seed(
        *,
        status: str = "completed",
        ended_at: datetime | None = None,
        last_checkpoint_at: datetime | None = None,
        checkpoint_purged_at: datetime | None = None,
    ) -> UUID:
        workflow_id = uuid4()
        run_id = uuid4()
        async with app_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO workflows (id, name, version, dag, status) "
                    "VALUES (:id, :name, 1, CAST(:dag AS jsonb), 'active')"
                ),
                {
                    "id": str(workflow_id),
                    "name": f"retention-test-{workflow_id}",
                    "dag": json.dumps({"nodes": [], "edges": []}),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(id, workflow_id, status, correlation_id, ended_at, "
                    " last_checkpoint_at, checkpoint_purged_at) "
                    "VALUES (:id, :wf, :status, :cid, :ended_at, :lca, :purged)"
                ),
                {
                    "id": str(run_id),
                    "wf": str(workflow_id),
                    "status": status,
                    "cid": str(uuid4()),
                    "ended_at": ended_at,
                    "lca": last_checkpoint_at,
                    "purged": checkpoint_purged_at,
                },
            )
            await session.commit()
        workflow_ids.append(workflow_id)
        run_ids.append(run_id)
        return run_id

    yield _seed

    async with app_session_factory() as session:
        await session.execute(
            text("DELETE FROM workflow_runs WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(rid) for rid in run_ids]},
        )
        await session.execute(
            text("DELETE FROM workflows WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(wid) for wid in workflow_ids]},
        )
        await session.commit()


# ─── AC1 — list_purgeable / mark_checkpoint_purged ─────────────────────


@pytest.mark.asyncio
async def test_list_purgeable_returns_old_terminal_unpurged_runs(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    now = datetime.now(UTC)
    old_completed = await seed_run(status="completed", ended_at=now - timedelta(days=100))
    recent_completed = await seed_run(status="completed", ended_at=now - timedelta(days=1))
    old_running = await seed_run(status="running", ended_at=None)

    repo = WorkflowRunRepo(session_factory=app_session_factory)
    runs = await repo.list_purgeable(
        terminal_statuses=("completed", "error", "cancelled"),
        older_than=now - timedelta(days=90),
    )
    ids = {run.id for run in runs}

    assert old_completed in ids
    assert recent_completed not in ids  # not old enough
    assert old_running not in ids  # not terminal


@pytest.mark.asyncio
async def test_list_purgeable_excludes_already_purged_runs(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    now = datetime.now(UTC)
    already_purged = await seed_run(
        status="completed", ended_at=now - timedelta(days=100), checkpoint_purged_at=now
    )

    repo = WorkflowRunRepo(session_factory=app_session_factory)
    runs = await repo.list_purgeable(
        terminal_statuses=("completed", "error", "cancelled"),
        older_than=now - timedelta(days=90),
    )

    assert already_purged not in {run.id for run in runs}


@pytest.mark.asyncio
async def test_mark_checkpoint_purged_stamps_the_column_and_excludes_from_future_passes(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    now = datetime.now(UTC)
    run_id = await seed_run(status="completed", ended_at=now - timedelta(days=100))
    repo = WorkflowRunRepo(session_factory=app_session_factory)

    before = await repo.list_purgeable(
        terminal_statuses=("completed",), older_than=now - timedelta(days=90)
    )
    assert run_id in {run.id for run in before}

    await repo.mark_checkpoint_purged(run_id)

    after = await repo.list_purgeable(
        terminal_statuses=("completed",), older_than=now - timedelta(days=90)
    )
    assert run_id not in {run.id for run in after}


# ─── AC2 — count_stale_paused ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_count_stale_paused_reports_count_and_oldest(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    now = datetime.now(UTC)
    oldest = await seed_run(status="paused", last_checkpoint_at=now - timedelta(days=30))
    await seed_run(status="paused", last_checkpoint_at=now - timedelta(days=10))
    await seed_run(status="paused", last_checkpoint_at=now - timedelta(days=1))  # not stale
    await seed_run(status="running", last_checkpoint_at=now - timedelta(days=30))  # wrong status

    repo = WorkflowRunRepo(session_factory=app_session_factory)
    count, oldest_id, oldest_staleness = await repo.count_stale_paused(
        older_than=now - timedelta(days=7)
    )

    assert count == 2
    assert oldest_id == oldest
    assert oldest_staleness is not None
    assert oldest_staleness < now - timedelta(days=7)


@pytest.mark.asyncio
async def test_count_stale_paused_is_zero_when_none_are_stale(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    now = datetime.now(UTC)
    await seed_run(status="paused", last_checkpoint_at=now - timedelta(hours=1))

    repo = WorkflowRunRepo(session_factory=app_session_factory)
    count, oldest_id, oldest_staleness = await repo.count_stale_paused(
        older_than=now - timedelta(days=7)
    )

    assert count == 0
    assert oldest_id is None
    assert oldest_staleness is None


# ─── the purge against a real checkpointer ─────────────────────────────


@pytest.mark.asyncio
async def test_the_worker_purges_through_a_real_checkpointer(
    migrated_db: str,
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """``AsyncPostgresSaver.adelete_thread`` is
    the single external call this entire feature rests on, and nothing
    exercised it. Every double in ``tests/unit/workflow_engine/test_retention.py``
    is an ``AsyncMock``, which auto-creates any attribute: were the method
    renamed or removed by a ``langgraph-checkpoint-postgres`` bump (a strict
    pin the project's own G3 policy says to re-validate on every bump), an
    ``AttributeError`` would pass every unit test, be swallowed per-item at
    runtime, and still emit ``retention_run_completed`` — the very line the
    runbook tells operators to grep as proof the worker works.

    This drives the real worker against the real saver and the real tables,
    so the call, its signature and the grants behind it are all covered."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agentive_backend.features.workflow_engine.retention import CheckpointRetentionWorker

    now = datetime.now(UTC)
    run_id = await seed_run(status="completed", ended_at=now - timedelta(days=120))

    # `migrated_db` yields a SQLAlchemy URL; psycopg's raw conninfo parser
    # rejects the `+psycopg` driver suffix. Same normalisation the runtime
    # does via `settings.psycopg_dsn` (`lifespan._workflow_checkpoint_dsn`).
    dsn = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)

    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        # Write a real checkpoint for this thread, so the purge has
        # something to delete rather than passing on an empty no-op.
        config = {"configurable": {"thread_id": str(run_id), "checkpoint_ns": ""}}
        await checkpointer.aput(
            config,
            {"v": 1, "id": str(uuid4()), "ts": now.isoformat(), "channel_values": {"x": 1}},
            {"source": "input", "step": 0, "parents": {}},
            {},
        )
        assert await checkpointer.aget(config) is not None, "precondition: a checkpoint exists"

        worker = CheckpointRetentionWorker(
            checkpointer=checkpointer,
            session_factory=app_session_factory,
            interval_s=86_400.0,
            retention_days=90,
            paused_alert_after_days=7,
        )
        summary = await worker.run_once()

        assert summary.purged_count >= 1
        assert summary.purge_failed_count == 0, "a real adelete_thread call must not fail"
        assert await checkpointer.aget(config) is None, "the blob must actually be gone"

    async with app_session_factory() as session:
        purged_at = (
            await session.execute(
                text("SELECT checkpoint_purged_at FROM workflow_runs WHERE id = :id"),
                {"id": run_id},
            )
        ).scalar_one()
    assert purged_at is not None, "the marker must be stamped only after a real delete"


# ─── AC3 — the partial expression index is actually used ───────────────


@pytest.fixture
async def bulk_runs(
    migrated_db: str,
    app_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Callable[[int], Awaitable[UUID]]]:
    """Seed N runs of ONE workflow, then ``ANALYZE``.

    The ``EXPLAIN`` assertions below are claims
    about what the PLANNER chooses, and a planner with no statistics on an
    empty table chooses a sequential scan every time — so a test that seeds
    nothing asserts a property of whatever rows earlier tests happened to
    leave behind, i.e. of the test ORDER. Seeding a realistic row count and
    refreshing the statistics is what makes the assertion mean what its name
    says.
    """
    workflow_ids: list[UUID] = []

    async def _seed(count: int) -> UUID:
        workflow_id = uuid4()
        async with app_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO workflows (id, name, version, dag, status) "
                    "VALUES (:id, :name, 1, CAST(:dag AS jsonb), 'active')"
                ),
                {
                    "id": str(workflow_id),
                    "name": f"plan-test-{workflow_id}",
                    "dag": json.dumps({"nodes": [], "edges": []}),
                },
            )
            # `generate_series` rather than N round trips: most rows terminal
            # (the realistic shape the partial index is sized against), a
            # minority `running` and stale.
            await session.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(id, workflow_id, status, correlation_id, started_at, "
                    " last_checkpoint_at, ended_at) "
                    "SELECT gen_random_uuid(), :wf, "
                    "       CASE WHEN i % 50 = 0 THEN 'running' ELSE 'completed' END, "
                    "       gen_random_uuid(), "
                    "       now() - (i || ' minutes')::interval, "
                    "       now() - (i || ' minutes')::interval, "
                    "       CASE WHEN i % 50 = 0 THEN NULL "
                    "            ELSE now() - (i || ' minutes')::interval END "
                    "FROM generate_series(1, :n) AS i"
                ),
                {"wf": str(workflow_id), "n": count},
            )
            await session.commit()
        async with app_session_factory() as session:
            await session.execute(text("ANALYZE workflow_runs"))
            await session.commit()
        workflow_ids.append(workflow_id)
        return workflow_id

    yield _seed

    async with app_session_factory() as session:
        await session.execute(
            text("DELETE FROM workflow_runs WHERE workflow_id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(wid) for wid in workflow_ids]},
        )
        await session.execute(
            text("DELETE FROM workflows WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(wid) for wid in workflow_ids]},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_claim_stale_running_query_uses_the_partial_index(
    app_session_factory: async_sessionmaker[AsyncSession],
    bulk_runs: Callable[[int], Awaitable[UUID]],
) -> None:
    """T3.3 — the index existing is not the same claim as the planner
    CHOOSING it. Verified via a real ``EXPLAIN``, not just `\\d` output —
    and against a populated, ANALYZEd table, because on an empty one
    the planner picks a seq scan regardless of what indexes exist."""
    await bulk_runs(5_000)

    async with app_session_factory() as session:
        result = await session.execute(
            text(
                "EXPLAIN SELECT id FROM workflow_runs "
                "WHERE status = 'running' "
                "AND COALESCE(last_checkpoint_at, started_at) < now() - interval '1500 seconds' "
                "ORDER BY COALESCE(last_checkpoint_at, started_at) LIMIT 50"
            )
        )
        plan = "\n".join(row[0] for row in result.all())

    assert "ix_workflow_runs_stale_running" in plan, plan
    assert "Seq Scan" not in plan, plan


@pytest.mark.asyncio
async def test_the_windowed_metrics_aggregate_uses_the_composite_index(
    app_session_factory: async_sessionmaker[AsyncSession],
    bulk_runs: Callable[[int], Awaitable[UUID]],
) -> None:
    """Story 4.10 AC4 added a trailing
    ``started_at`` window to ``routing-stats``/``handoff-stats`` on the
    ground that the endpoints "paid a cost proportional to a number the
    CALLER controls". The window bounded how many rows were AGGREGATED, but
    no index could serve ``workflow_id = ? AND started_at >= ?``:
    ``ix_workflow_runs_workflow`` covers the first column alone, and the
    partial index is scoped to ``status = 'running'`` while these aggregates
    read terminal rows. Postgres therefore still read every run of the
    workflow and filtered afterwards — the full scan the AC exists to
    remove."""
    workflow_id = await bulk_runs(5_000)

    async with app_session_factory() as session:
        result = await session.execute(
            text(
                "EXPLAIN SELECT count(*) FROM workflow_runs "
                "WHERE workflow_id = :wf AND started_at >= now() - interval '90 days'"
            ),
            {"wf": str(workflow_id)},
        )
        plan = "\n".join(row[0] for row in result.all())

    assert "ix_workflow_runs_workflow_started" in plan, plan
    assert "Seq Scan" not in plan, plan
