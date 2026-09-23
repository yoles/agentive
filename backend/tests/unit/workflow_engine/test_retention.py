"""Unit tests — :class:`CheckpointRetentionWorker` (Story 4.10 AC1/AC2)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.features.workflow_engine.retention import (
    _MAX_ORPHANS_PER_PASS,
    CheckpointRetentionWorker,
)

#: Distingue « pas d'argument » de `ended_at=None`, qui est le cas sous test.
_SENTINEL = object()


def _run(
    *,
    run_id: object | None = None,
    workflow_id: object | None = None,
    ended_at: object | None = _SENTINEL,
    status: str = "completed",
) -> SimpleNamespace:
    """A ``WorkflowRun`` double carrying the fields the purge actually reads.

    `ended_at`/`status` are part of the shape since Story 4.15 AC3: the purge
    reports a terminal run reaching it through the `COALESCE` fallback, so a
    double without them raises `AttributeError` instead of modelling the row.
    Default is a real timestamp — the nominal case — so only a test that means
    to exercise the fallback passes `ended_at=None`.
    """
    return SimpleNamespace(
        id=run_id or uuid4(),
        workflow_id=workflow_id or uuid4(),
        ended_at=datetime.now(UTC) if ended_at is _SENTINEL else ended_at,
        status=status,
    )


def _draining(*batches: list[SimpleNamespace]) -> AsyncMock:
    """A ``list_purgeable`` double that DRAINS, like the real one.

    the purge now claims repeated batches until
    the backlog is empty, because a single capped claim per daily pass could
    never outpace ingestion. A stub returning a constant page therefore no
    longer models the repo: a successful purge stamps ``checkpoint_purged_at``
    and the row leaves ``list_purgeable``'s predicate, so the next claim must
    return something else. These doubles say so explicitly."""
    return AsyncMock(side_effect=[*batches, []])


def _lease(granted: bool) -> object:
    """A ``purge_lease`` double. The real one is an async context manager
    yielding whether THIS process holds the cluster-wide purge lease, so an
    `AsyncMock` attribute is the wrong shape entirely — it returns a
    coroutine, not a context manager."""

    def _factory() -> AsyncIterator[bool]:
        @contextlib.asynccontextmanager
        async def _cm() -> AsyncIterator[bool]:
            yield granted

        return _cm()

    return _factory


def _make_worker() -> tuple[CheckpointRetentionWorker, AsyncMock, AsyncMock]:
    checkpointer = AsyncMock()
    worker = CheckpointRetentionWorker(
        checkpointer=checkpointer,
        session_factory=MagicMock(),
        interval_s=86_400.0,
        retention_days=90,
        paused_alert_after_days=7,
    )
    workflow_run_repo = AsyncMock()
    workflow_run_repo.purge_lease = _lease(True)
    # the real signature returns a rowcount, and 0 means "the row
    # vanished under us"; be explicit rather than relying on a MagicMock
    # comparing unequal to 0 by accident.
    workflow_run_repo.mark_checkpoint_purged = AsyncMock(return_value=1)
    # Story 4.15 — sans ca, `list_orphan_checkpoint_threads` rend un MagicMock
    # (verite-vraie mais iterable vide) et la passe orpheline part droit dans
    # sa branche `no_progress` dans TOUS les tests preexistants.
    workflow_run_repo.list_orphan_checkpoint_threads = AsyncMock(return_value=[])
    workflow_run_repo.count_visible_runs = AsyncMock(return_value=42)
    worker._workflow_run_repo = workflow_run_repo
    return worker, workflow_run_repo, checkpointer


# ─── AC1 — purge pass ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_purges_every_listed_run_and_marks_it() -> None:
    run_a, run_b = _run(), _run()
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([run_a, run_b])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    summary = await worker.run_once()

    assert summary.purged_count == 2
    assert summary.purge_failed_count == 0
    checkpointer.adelete_thread.assert_any_await(str(run_a.id))
    checkpointer.adelete_thread.assert_any_await(str(run_b.id))
    assert repo.mark_checkpoint_purged.await_count == 2


@pytest.mark.asyncio
async def test_run_once_marks_purged_only_after_adelete_thread_succeeds() -> None:
    """A crash between the delete and the marker must leave the run eligible
    for a harmless re-purge, never falsely marked done."""
    run = _run()
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([run])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    checkpointer.adelete_thread = AsyncMock(side_effect=RuntimeError("connection dropped"))

    summary = await worker.run_once()

    assert summary.purged_count == 0
    assert summary.purge_failed_count == 1
    repo.mark_checkpoint_purged.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_one_poisoned_run_does_not_block_its_siblings() -> None:
    poisoned, healthy = _run(), _run()
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([poisoned, healthy])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    async def _adelete(thread_id: str) -> None:
        if thread_id == str(poisoned.id):
            raise RuntimeError("boom")

    checkpointer.adelete_thread = AsyncMock(side_effect=_adelete)

    summary = await worker.run_once()

    assert summary.purged_count == 1
    assert summary.purge_failed_count == 1
    repo.mark_checkpoint_purged.assert_awaited_once_with(healthy.id)


@pytest.mark.asyncio
async def test_purge_drains_a_backlog_larger_than_one_batch() -> None:
    """The purge capped 1000 ROWS per pass with
    no loop, against an `interval_s` defaulting to 86_400 (one day). Any
    deployment producing more than a batch of terminal runs per day never
    caught up and the unpurged set grew monotonically — so the mechanism
    that exists to BOUND `checkpoint_blobs` growth did not bound it. The
    constant's own comment invoked `MemoryArchivalWorker._MAX_BATCHES_PER_RUN`
    as precedent, which drains 1000 BATCHES, not 1000 rows."""
    first, second, third = [_run(), _run()], [_run()], [_run(), _run()]
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = _draining(first, second, third)
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    summary = await worker.run_once()

    assert summary.purged_count == 5, "every batch must be drained, not just the first"
    assert repo.list_purgeable.await_count == 4  # 3 batches + the empty one


@pytest.mark.asyncio
async def test_purge_excludes_runs_that_already_failed_in_this_pass() -> None:
    """A failed purge does NOT stamp `checkpoint_purged_at`, so it stays
    inside `list_purgeable`'s predicate. Without excluding it, batching
    re-served the same failing rows on every iteration and burned the whole
    batch budget retrying them instead of advancing the backlog."""
    poisoned, healthy = _run(), _run()
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([poisoned, healthy])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    async def _adelete(thread_id: str) -> None:
        if thread_id == str(poisoned.id):
            raise RuntimeError("boom")

    checkpointer.adelete_thread = AsyncMock(side_effect=_adelete)

    await worker.run_once()

    assert repo.list_purgeable.await_args.kwargs["exclude_run_ids"] == [poisoned.id]


@pytest.mark.asyncio
async def test_purge_stops_when_a_claim_makes_no_progress() -> None:
    """Draining relies on `mark_checkpoint_purged` removing rows from
    `list_purgeable`'s predicate. If that contract stops holding (replica
    lag, a stamp that silently matched nothing), the loop must not re-purge
    the same page `_MAX_BATCHES_PER_PASS` times."""
    stuck = [_run(), _run()]
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = AsyncMock(return_value=stuck)  # never drains
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    summary = await worker.run_once()

    assert repo.list_purgeable.await_count == 2
    assert summary.purged_count == 2


@pytest.mark.asyncio
async def test_a_run_deleted_under_the_purge_is_not_counted_as_purged() -> None:
    """`workflow_runs.workflow_id` carries
    `ondelete="CASCADE"`, so a workflow deleted between the claim and the
    mark takes its runs with it. `mark_checkpoint_purged` ignored its
    rowcount and the caller counted the run as purged work regardless."""
    vanished = _run()
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = _draining([vanished])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    repo.mark_checkpoint_purged = AsyncMock(return_value=0)

    summary = await worker.run_once()

    assert summary.purged_count == 0
    assert summary.purge_failed_count == 0, "a vanished row is not a purge FAILURE"


@pytest.mark.asyncio
async def test_a_pass_where_every_purge_failed_is_logged_as_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A total failure emitted the same
    `retention_run_completed` INFO line with `purged_count=0` that a healthy
    pass with nothing to do emits. Since that line is what the runbook tells
    operators to grep as proof the worker works, a feature that had stopped
    working entirely (a renamed `adelete_thread` after a pin bump — every
    test double here is an `AsyncMock`, which would auto-create the
    attribute and hide it) was indistinguishable from success."""
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.retention._log.error",
        lambda event, **kw: events.append((event, kw)),
    )
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([_run(), _run()])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    checkpointer.adelete_thread = AsyncMock(side_effect=RuntimeError("gone"))

    await worker.run_once()

    degraded = [kw for event, kw in events if event == "workflow_engine.retention_purge_degraded"]
    assert degraded, "a pass that purged nothing and failed everything must be loud"
    assert degraded[0]["total_failure"] is True
    assert degraded[0]["purge_failed_count"] == 2


def test_next_delay_backs_off_after_a_failure_and_never_exceeds_the_cadence() -> None:
    """`_loop` always waited the nominal
    `interval_s` (24 h by default), failure or not, so one transient error
    cost a full day of BOTH purging and stale-`paused` alerting. The module
    docstring already claimed to be a structural mirror of
    `MemoryArchivalWorker`; this is the half that was missing."""
    worker, _repo, _checkpointer = _make_worker()

    assert worker._next_delay(0) == 86_400.0
    assert worker._next_delay(1) == 60.0
    assert worker._next_delay(2) == 300.0
    assert worker._next_delay(3) == 900.0
    assert worker._next_delay(99) == 900.0, "the schedule must saturate, not index out of range"

    worker._interval_s = 30.0
    assert worker._next_delay(1) == 30.0, "a failing worker must never be SLOWER than a healthy one"


@pytest.mark.asyncio
async def test_a_replica_without_the_lease_purges_nothing_but_still_alerts() -> None:
    """`lifespan` starts this worker in EVERY process, so N replicas would
    otherwise all claim the same rows and issue the same `adelete_thread`
    calls — duplicated work and contention on `checkpoint_writes`/
    `checkpoint_blobs`. The stale-`paused` alert is deliberately OUTSIDE the
    lease: it is a read plus a log line, so emitting it N times is noise,
    whereas gating it would mean a replica that never wins the lease never
    reports a stale run either."""
    worker, repo, checkpointer = _make_worker()
    repo.purge_lease = _lease(False)
    repo.list_purgeable = _draining([_run(), _run()])
    repo.count_stale_paused = AsyncMock(return_value=(4, uuid4(), None))

    summary = await worker.run_once()

    assert summary.purged_count == 0
    checkpointer.adelete_thread.assert_not_awaited()
    repo.list_purgeable.assert_not_awaited()
    assert summary.stale_paused_count == 4


# ─── AC2 — stale-paused alert ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_reports_stale_paused_count_without_acting_on_it() -> None:
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = AsyncMock(return_value=[])
    stale_run_id = uuid4()
    repo.count_stale_paused = AsyncMock(return_value=(3, stale_run_id, None))

    summary = await worker.run_once()

    assert summary.stale_paused_count == 3
    # Never any write against a `paused` run — alert only, T2.1's decision.
    repo.update_status.assert_not_called()
    repo.update_status_in_session.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_reports_zero_when_nothing_is_stale() -> None:
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = AsyncMock(return_value=[])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    summary = await worker.run_once()

    assert summary.stale_paused_count == 0


# ─── lifecycle ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_twice_raises() -> None:
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = AsyncMock(return_value=[])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    await worker.start()
    try:
        with pytest.raises(RuntimeError):
            await worker.start()
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = AsyncMock(return_value=[])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    await worker.start()
    await worker.stop()
    await worker.stop()  # must not raise


# ─── Story 4.15 AC2 — balayage des threads orphelins ────────────────────


@pytest.mark.asyncio
async def test_run_once_purges_threads_whose_run_row_no_longer_exists() -> None:
    """AC2 — `ON DELETE CASCADE` efface les runs d'un workflow supprime, et
    `list_purgeable` joint sur `workflow_runs` : les blobs LangGraph
    correspondants deviennent alors introuvables par la purge, DEFINITIVEMENT.

    C'est la condition de croissance non bornee que l'AC1 de la Story 4.10
    existe pour fermer, atteinte par une porte que sa conception ne couvre
    pas. La decouverte doit donc etre independante de `workflow_runs`."""
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    orphans = ["thread-a", "thread-b"]
    repo.list_orphan_checkpoint_threads = AsyncMock(side_effect=[orphans, []])

    summary = await worker.run_once()

    assert checkpointer.adelete_thread.await_count == 2
    assert {c.args[0] for c in checkpointer.adelete_thread.await_args_list} == set(orphans)
    assert summary.orphan_purged_count == 2


@pytest.mark.asyncio
async def test_orphan_sweep_is_skipped_by_a_replica_without_the_lease() -> None:
    """Le balayage SUPPRIME des donnees : comme la passe AC1, il doit vivre
    sous le bail consultatif, sinon N repliques emettent les memes DELETE et
    se contendent sur `checkpoint_writes`/`checkpoint_blobs`."""
    worker, repo, checkpointer = _make_worker()
    repo.purge_lease = _lease(False)
    repo.list_purgeable = _draining([])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    repo.list_orphan_checkpoint_threads = AsyncMock(return_value=["thread-a"])

    summary = await worker.run_once()

    repo.list_orphan_checkpoint_threads.assert_not_awaited()
    checkpointer.adelete_thread.assert_not_awaited()
    assert summary.orphan_purged_count == 0


@pytest.mark.asyncio
async def test_one_failing_orphan_does_not_block_its_siblings() -> None:
    """Meme resilience par item que toutes les autres passes du depot."""
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    repo.list_orphan_checkpoint_threads = AsyncMock(side_effect=[["bad", "good"], []])
    checkpointer.adelete_thread = AsyncMock(
        side_effect=[RuntimeError("boom"), None],
    )

    summary = await worker.run_once()

    assert summary.orphan_purged_count == 1
    assert summary.orphan_failed_count == 1


@pytest.mark.asyncio
async def test_orphan_sweep_stops_when_a_claim_makes_no_progress() -> None:
    """Le balayage ne peut pas s'appuyer sur un marqueur pour drainer (la
    ligne du run n'existe plus, il n'y a rien a estampiller) : la seule
    preuve de progression est que la reclamation suivante rende autre chose.
    Sans cette garde, un thread que `adelete_thread` echoue a vider serait
    reclame `_MAX_BATCHES_PER_PASS` fois."""
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    repo.list_orphan_checkpoint_threads = AsyncMock(return_value=["stuck"])

    summary = await worker.run_once()

    assert repo.list_orphan_checkpoint_threads.await_count == 2
    assert checkpointer.adelete_thread.await_count == 1
    assert summary.orphan_purged_count == 1


@pytest.mark.asyncio
async def test_purging_a_terminal_run_without_ended_at_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 — fermer le trou ne doit pas rendre l'anomalie invisible. Aucun
    site d'ecriture applicatif ne produit un statut terminal sans
    `ended_at` : si la purge en rencontre un, c'est qu'autre chose a ecrit
    ce statut, et cela doit se voir."""
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.retention._log.warning",
        lambda event, **kw: events.append((event, kw)),
    )
    run = _run(ended_at=None, status="completed")
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = _draining([run])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    summary = await worker.run_once()

    assert summary.purged_count == 1, "le trou est FERME : le run doit bien etre purge"
    reported = [
        kw
        for event, kw in events
        if event == "workflow_engine.retention_terminal_run_without_ended_at"
    ]
    assert reported, "...et l'anomalie doit rester visible, pas absorbee en silence"
    assert reported[0]["run_id"] == str(run.id)


@pytest.mark.asyncio
async def test_a_normal_terminal_run_is_purged_without_any_anomaly_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le pendant du test precedent : le warning doit mordre sur le cas
    anormal SEULEMENT. Sans lui, une implementation qui loggue a chaque purge
    passerait le test ci-dessus tout en noyant le signal."""
    events: list[str] = []
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.retention._log.warning",
        lambda event, **kw: events.append(event),
    )
    worker, repo, _checkpointer = _make_worker()
    repo.list_purgeable = _draining([_run()])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))

    summary = await worker.run_once()

    assert summary.purged_count == 1
    assert "workflow_engine.retention_terminal_run_without_ended_at" not in events


@pytest.mark.asyncio
async def test_orphan_sweep_refuses_a_batch_beyond_the_blast_radius_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second garde, pour toute cause de predicat faux que le premier ne
    couvre pas (RLS partielle, replica en retard, politique future). Un lot
    au-dela du plafond est traite comme un predicat faux, pas comme un
    arriere : refus complet, ERROR, zero suppression."""
    events: list[str] = []
    monkeypatch.setattr(
        "agentive_backend.features.workflow_engine.retention._log.error",
        lambda event, **kw: events.append(event),
    )
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    repo.list_orphan_checkpoint_threads = AsyncMock(
        return_value=[f"t-{i}" for i in range(_MAX_ORPHANS_PER_PASS + 1)]
    )

    summary = await worker.run_once()

    checkpointer.adelete_thread.assert_not_awaited()
    assert summary.orphan_purged_count == 0
    assert "workflow_engine.retention_orphan_refused_blast_radius" in events


@pytest.mark.asyncio
async def test_a_failing_orphan_is_excluded_from_the_next_claim() -> None:
    """Sans exclusion, un thread dont la suppression echoue toujours et qui
    trie en tete est re-reclame a chaque lot : la garde d'egalite d'ensemble
    ne mord pas (le reste du lot differe), et il est reessaye jusqu'a 1000
    fois par passe, chaque echec ecrivant une trace complete."""
    worker, repo, checkpointer = _make_worker()
    repo.list_purgeable = _draining([])
    repo.count_stale_paused = AsyncMock(return_value=(0, None, None))
    repo.list_orphan_checkpoint_threads = AsyncMock(side_effect=[["bad"], []])
    checkpointer.adelete_thread = AsyncMock(side_effect=RuntimeError("boom"))

    await worker.run_once()

    second_claim = repo.list_orphan_checkpoint_threads.await_args_list[1]
    assert list(second_claim.kwargs["exclude_threads"]) == ["bad"]
