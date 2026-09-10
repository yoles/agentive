"""Integration tests — ``WorkflowRunRepo.claim_stale_running`` (Story 4.2 AC3).

The unit tests around the recovery worker mock the session, so they can only
prove the worker CALLS the repo. The property that actually matters — a
claimed run becomes invisible to the very next sweep — lives entirely in the
SQL (``UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING``
plus the ``jsonb_set`` counter bump), so it can only be verified against a
real Postgres.

Regression guard for the pre-fix behaviour: the sweep was a plain ``SELECT``,
which left the row untouched, so every 30s tick re-selected the same orphaned
run and spawned another LangGraph execution on the same ``thread_id`` —
duplicate billed LLM calls and interleaved checkpoint writes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
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
    """Insert ``workflows`` + ``workflow_runs`` pairs whose staleness is
    controlled by ``last_checkpoint_age_s`` (seconds in the past), and delete
    them again on teardown.

    The cleanup is not hygiene, it is correctness: ``migrated_db`` is
    session-scoped, and these rows are deliberately ``running`` + stale
    forever. Left behind, they are exactly what a later recovery test's own
    ``run_once()`` sweep would claim — ``test_recovery_e2e`` runs its worker
    with ``stale_threshold_s=0.0`` and asserts an exact ``resume_triggered_count``.
    """
    workflow_ids: list[UUID] = []
    run_ids: list[UUID] = []

    async def _seed(
        *,
        last_checkpoint_age_s: float,
        checkpoint: dict[str, Any] | None = None,
        status: str = "running",
    ) -> UUID:
        workflow_id = uuid4()
        run_id = uuid4()
        last_checkpoint_at = datetime.now(UTC) - timedelta(seconds=last_checkpoint_age_s)
        async with app_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO workflows (id, name, version, dag, status) "
                    "VALUES (:id, :name, 1, CAST(:dag AS jsonb), 'active')"
                ),
                {
                    "id": str(workflow_id),
                    "name": f"claim-test-{workflow_id}",
                    "dag": json.dumps({"nodes": [], "edges": []}),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(id, workflow_id, status, correlation_id, checkpoint, last_checkpoint_at) "
                    "VALUES (:id, :wf, :status, :cid, CAST(:cp AS jsonb), :lca)"
                ),
                {
                    "id": str(run_id),
                    "wf": str(workflow_id),
                    "status": status,
                    "cid": str(uuid4()),
                    "cp": json.dumps(checkpoint) if checkpoint is not None else None,
                    "lca": last_checkpoint_at,
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


@pytest.mark.asyncio
async def test_a_claimed_run_is_not_reclaimed_by_the_next_sweep(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """THE property of the fix. Two back-to-back sweeps with the same
    threshold must claim the run exactly once — the first claim stamps
    ``last_checkpoint_at = now()``, which pushes the row back outside the
    staleness window for a full ``stale_threshold_s``."""
    repo = WorkflowRunRepo(session_factory=app_session_factory)
    run_id = await seed_run(last_checkpoint_age_s=600.0)
    older_than = datetime.now(UTC) - timedelta(seconds=300.0)

    first = await repo.claim_stale_running(older_than=older_than)
    second = await repo.claim_stale_running(older_than=older_than)

    assert run_id in {run.id for run in first}
    assert run_id not in {run.id for run in second}


@pytest.mark.asyncio
async def test_claim_increments_the_attempt_counter_and_returns_it(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """The counter must be readable off the RETURNING row — that is how the
    worker decides between resuming and abandoning, without a second query."""
    repo = WorkflowRunRepo(session_factory=app_session_factory)
    run_id = await seed_run(
        last_checkpoint_age_s=600.0,
        checkpoint={"last_node_id": "a", "recovery_attempts": 2},
    )

    claimed = await repo.claim_stale_running(older_than=datetime.now(UTC) - timedelta(seconds=300))

    run = next(run for run in claimed if run.id == run_id)
    assert run.checkpoint["recovery_attempts"] == 3
    # The rest of the applicative summary must survive the jsonb_set.
    assert run.checkpoint["last_node_id"] == "a"


@pytest.mark.asyncio
async def test_claim_seeds_the_counter_when_checkpoint_is_null(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """A run that crashed before its first checkpoint sync has a NULL
    ``checkpoint``; ``jsonb_set(NULL, ...)`` returns NULL, so the COALESCE to
    ``'{}'::jsonb`` is what keeps the counter working for exactly the runs
    the recovery worker exists to catch."""
    repo = WorkflowRunRepo(session_factory=app_session_factory)
    run_id = await seed_run(last_checkpoint_age_s=600.0, checkpoint=None)

    claimed = await repo.claim_stale_running(older_than=datetime.now(UTC) - timedelta(seconds=300))

    run = next(run for run in claimed if run.id == run_id)
    assert run.checkpoint == {"recovery_attempts": 1}


@pytest.mark.asyncio
async def test_claim_tolerates_a_non_numeric_stored_counter(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """``checkpoint`` is free-form JSONB. Without the ``jsonb_typeof`` guard
    the ``::int`` cast aborts the whole statement, so ONE corrupted run would
    block recovery for every other orphan in the sweep."""
    repo = WorkflowRunRepo(session_factory=app_session_factory)
    poisoned = await seed_run(
        last_checkpoint_age_s=600.0,
        checkpoint={"recovery_attempts": "lots"},
    )
    healthy = await seed_run(last_checkpoint_age_s=600.0)

    claimed = await repo.claim_stale_running(older_than=datetime.now(UTC) - timedelta(seconds=300))

    claimed_ids = {run.id for run in claimed}
    assert poisoned in claimed_ids
    assert healthy in claimed_ids
    poisoned_run = next(run for run in claimed if run.id == poisoned)
    assert poisoned_run.checkpoint["recovery_attempts"] == 1


@pytest.mark.asyncio
async def test_claim_ignores_recent_and_terminal_runs(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """A live run that simply checkpointed a moment ago, and a run already in
    a terminal status, must both stay untouched."""
    repo = WorkflowRunRepo(session_factory=app_session_factory)
    recent = await seed_run(last_checkpoint_age_s=5.0)
    terminal = await seed_run(last_checkpoint_age_s=600.0, status="completed")

    claimed = await repo.claim_stale_running(older_than=datetime.now(UTC) - timedelta(seconds=300))

    claimed_ids = {run.id for run in claimed}
    assert recent not in claimed_ids
    assert terminal not in claimed_ids


@pytest.mark.asyncio
async def test_claim_under_limit_takes_the_oldest_runs(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """``LIMIT`` without ``ORDER BY`` returned a non-deterministic SUBSET, so
    a backlog larger than the limit could starve the same runs sweep after
    sweep. Oldest-first makes the limit a queue rather than a lottery.

    Note this asserts WHICH rows the limit retains, not the order they come
    back in: ``UPDATE ... RETURNING`` does not preserve the sub-select's
    ``ORDER BY``, and the worker does not depend on the return order.
    """
    repo = WorkflowRunRepo(session_factory=app_session_factory)
    newest = await seed_run(last_checkpoint_age_s=400.0)
    oldest = await seed_run(last_checkpoint_age_s=9000.0)
    middle = await seed_run(last_checkpoint_age_s=1200.0)

    claimed = await repo.claim_stale_running(
        older_than=datetime.now(UTC) - timedelta(seconds=300), limit=2
    )

    claimed_ids = {run.id for run in claimed}
    assert claimed_ids == {oldest, middle}
    assert newest not in claimed_ids
