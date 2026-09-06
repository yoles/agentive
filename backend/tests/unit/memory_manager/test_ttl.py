"""Unit tests for :class:`MemoryArchivalWorker` (Story 3.3 T10.4).

Pattern: AsyncMock repos, monkeypatched ``publish``/``notify_best_effort``
(mirrors ``tests/unit/memory_manager/test_service.py``). Only ``run_once``
is exercised — never ``start()``/``stop()`` with the real 86400s interval
(this story's Testing Standards Summary).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import structlog.testing
from prometheus_client import REGISTRY

from agentive_backend.features.memory_manager import ttl as ttl_module
from agentive_backend.features.memory_manager.ttl import MemoryArchivalWorker


def _configure_with_tenant(repo: MagicMock, session: MagicMock) -> None:
    """Wire ``repo.with_tenant(...)`` as an async context manager yielding
    ``session`` — mirror of ``tests/unit/memory_manager/test_service.py``."""
    repo.with_tenant = MagicMock()
    repo.with_tenant.return_value.__aenter__ = AsyncMock(return_value=session)
    repo.with_tenant.return_value.__aexit__ = AsyncMock(return_value=False)


def _make_namespace(*, archive_after_seconds: int | None = None) -> SimpleNamespace:
    policy: dict[str, int] = {}
    if archive_after_seconds is not None:
        policy["archive_after_seconds"] = archive_after_seconds
    return SimpleNamespace(id=uuid4(), name="ns-test", retention_policy=policy)


def _make_chunk(*, namespace_id: object) -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), namespace_id=namespace_id, created_at=datetime.now(UTC))


def _make_worker(
    *,
    namespaces: list[SimpleNamespace] | None = None,
    find_expired_side_effect: list[list[SimpleNamespace]] | None = None,
    find_archivable_side_effect: list[list[SimpleNamespace]] | None = None,
    mark_archived_return: int = 1,
    batch_size: int = 1_000,
) -> tuple[MemoryArchivalWorker, AsyncMock, AsyncMock]:
    worker = MemoryArchivalWorker(session_factory=MagicMock(), batch_size=batch_size)

    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.find_expired = AsyncMock(side_effect=find_expired_side_effect or [[]])
    memory_chunk_repo.find_archivable_in_namespace = AsyncMock(
        side_effect=find_archivable_side_effect or [[]]
    )
    memory_chunk_repo.mark_archived_in_session = AsyncMock(return_value=mark_archived_return)
    _configure_with_tenant(memory_chunk_repo, MagicMock())

    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=namespaces or [])

    worker._memory_chunk_repo = memory_chunk_repo  # type: ignore[assignment]
    worker._namespace_repo = namespace_repo  # type: ignore[assignment]
    return worker, memory_chunk_repo, namespace_repo


def _patch_event_bus(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    published: list[tuple[str, object]] = []

    async def _fake_publish(event_type: str, event: object, **_kw: object) -> object:
        published.append((event_type, event))
        return uuid4()

    monkeypatch.setattr(ttl_module, "publish", _fake_publish)
    monkeypatch.setattr(ttl_module, "notify_best_effort", AsyncMock())
    return published


# ─── AC1 — TTL expiry ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_archives_expired_chunk_and_publishes_ttl_expired_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _make_namespace()
    chunk = _make_chunk(namespace_id=namespace.id)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace], find_expired_side_effect=[[chunk], []]
    )
    published = _patch_event_bus(monkeypatch)
    now = datetime(2030, 1, 1, tzinfo=UTC)

    summary = await worker.run_once(now=now)

    assert summary.ttl_expired_count == 1
    assert summary.archive_after_seconds_count == 0
    memory_chunk_repo.mark_archived_in_session.assert_awaited_once()
    call = memory_chunk_repo.mark_archived_in_session.await_args
    assert call.args[1] == [chunk.id]
    assert call.kwargs["archived_at"] == now

    assert len(published) == 1
    event_type, event = published[0]
    assert event_type == "memory_manager.chunk.archived"
    assert event.chunk_id == chunk.id
    assert event.namespace_id == namespace.id
    assert event.namespace == namespace.name
    assert event.reason == "ttl_expired"


@pytest.mark.asyncio
async def test_run_once_does_not_publish_when_already_archived_by_a_concurrent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mark_archived_in_session` returning 0 (the `archived_at IS NULL`
    guard didn't match) must not publish a duplicate event."""
    namespace = _make_namespace()
    chunk = _make_chunk(namespace_id=namespace.id)
    worker, _memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace],
        find_expired_side_effect=[[chunk], []],
        mark_archived_return=0,
    )
    published = _patch_event_bus(monkeypatch)

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 0
    assert published == []


@pytest.mark.asyncio
async def test_run_once_continues_after_a_single_chunk_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5.3 — one poison chunk must not abort the rest of the batch."""
    namespace = _make_namespace()
    chunk_bad = _make_chunk(namespace_id=namespace.id)
    chunk_good = _make_chunk(namespace_id=namespace.id)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace],
        find_expired_side_effect=[[chunk_bad, chunk_good], []],
    )
    memory_chunk_repo.mark_archived_in_session = AsyncMock(side_effect=[RuntimeError("boom"), 1])
    _patch_event_bus(monkeypatch)

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 1
    assert memory_chunk_repo.mark_archived_in_session.await_count == 2


@pytest.mark.asyncio
async def test_run_once_treats_a_concurrent_race_as_nominal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mark_archived_in_session` returning 0 means another replica won the
    race — the row now has `archived_at` set and drops out of the next query
    by itself. It must NOT be counted, and must NOT abort the phase the way
    a real failure does (code review Story 3.3, P1)."""
    monkeypatch.setattr(ttl_module, "_MAX_BATCHES_PER_RUN", 5)
    namespace = _make_namespace()
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace],
        find_expired_side_effect=[
            [_make_chunk(namespace_id=namespace.id)],
            [_make_chunk(namespace_id=namespace.id)],
            [],
        ],
        mark_archived_return=0,
        batch_size=1,
    )

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 0
    # Kept draining rather than stopping on the first "0 archived" batch.
    assert memory_chunk_repo.find_expired.await_count == 3


@pytest.mark.asyncio
async def test_run_once_excludes_a_failed_chunk_from_the_rest_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk that fails deterministically must not block the chunks behind
    it: `find_expired` orders by `expires_at`, so without exclusion the same
    poison head is re-served every batch, every run, forever
    (code review Story 3.3, P2)."""
    monkeypatch.setattr(ttl_module, "_MAX_BATCHES_PER_RUN", 5)
    namespace = _make_namespace()
    poison = _make_chunk(namespace_id=namespace.id)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace],
        find_expired_side_effect=[[poison], [_make_chunk(namespace_id=namespace.id)], []],
        batch_size=1,
    )
    # Fails on the first chunk only, succeeds afterwards.
    memory_chunk_repo.mark_archived_in_session = AsyncMock(side_effect=[RuntimeError("boom"), 1])
    _patch_event_bus(monkeypatch)

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 1
    assert memory_chunk_repo.find_expired.await_count == 3
    # The poison id is excluded from every subsequent fetch of this run.
    assert memory_chunk_repo.find_expired.await_args_list[1].kwargs["exclude_ids"] == {poison.id}


@pytest.mark.asyncio
async def test_run_once_stops_at_max_batches_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttl_module, "_MAX_BATCHES_PER_RUN", 3)
    namespace = _make_namespace()
    # `batch_size=1` so a one-chunk batch is a FULL batch: the worker only
    # keeps looping while the queue still looks saturated.
    worker, memory_chunk_repo, _ = _make_worker(namespaces=[namespace], batch_size=1)
    memory_chunk_repo.find_expired = AsyncMock(
        side_effect=lambda *_a, **_k: [_make_chunk(namespace_id=namespace.id)]
    )
    memory_chunk_repo.mark_archived_in_session = AsyncMock(return_value=1)
    _patch_event_bus(monkeypatch)

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 3
    assert memory_chunk_repo.find_expired.await_count == 3


# ─── AC3 — archive_after_seconds ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_archive_after_seconds_only_targets_configured_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_ns = _make_namespace(archive_after_seconds=3600)
    unconfigured_ns = _make_namespace()
    chunk = _make_chunk(namespace_id=configured_ns.id)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[configured_ns, unconfigured_ns],
        find_expired_side_effect=[[]],
        find_archivable_side_effect=[[chunk], []],
    )
    published = _patch_event_bus(monkeypatch)

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.archive_after_seconds_count == 1
    called_namespace_ids = {
        c.args[0] for c in memory_chunk_repo.find_archivable_in_namespace.await_args_list
    }
    assert called_namespace_ids == {configured_ns.id}
    assert published[0][1].reason == "archive_after_seconds"


@pytest.mark.asyncio
async def test_run_once_skips_namespace_with_malformed_retention_policy() -> None:
    """The log assertion is the point: a namespace with no
    `archive_after_seconds` is skipped too, so `assert_not_awaited` alone
    would pass even if the targeted `except` disappeared entirely
    (code review Story 3.3, P13)."""
    bad_ns = SimpleNamespace(
        id=uuid4(), name="bad-ns", retention_policy={"archive_after_seconds": "not-an-int"}
    )
    worker, memory_chunk_repo, _ = _make_worker(namespaces=[bad_ns], find_expired_side_effect=[[]])

    with structlog.testing.capture_logs() as logs:
        summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.archive_after_seconds_count == 0
    memory_chunk_repo.find_archivable_in_namespace.assert_not_awaited()
    assert [
        entry
        for entry in logs
        if entry["event"] == "memory_manager.archival_namespace_retention_policy_invalid"
        and entry["namespace"] == "bad-ns"
    ]


@pytest.mark.asyncio
async def test_run_once_isolates_a_failing_namespace_from_the_others() -> None:
    """A transient DB error on one namespace used to escape `_archive_by_age`
    and kill the AC3 phase for every namespace after it, until the next run
    24h later (code review Story 3.3, P4)."""
    failing_ns = _make_namespace(archive_after_seconds=3600)
    healthy_ns = _make_namespace(archive_after_seconds=3600)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[failing_ns, healthy_ns], find_expired_side_effect=[[]]
    )
    memory_chunk_repo.find_archivable_in_namespace = AsyncMock(
        side_effect=[RuntimeError("connection reset"), []]
    )

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.archive_after_seconds_count == 0
    # The second namespace was still visited despite the first one blowing up.
    assert memory_chunk_repo.find_archivable_in_namespace.await_count == 2


@pytest.mark.asyncio
async def test_run_once_survives_an_absurd_archive_after_seconds() -> None:
    """`timedelta()` overflows on an out-of-range value. That call sits
    outside the `from_mapping` guard, so it used to abort the whole AC3
    phase (code review Story 3.3, P4)."""
    absurd_ns = SimpleNamespace(
        id=uuid4(), name="absurd-ns", retention_policy={"archive_after_seconds": 10**300}
    )
    healthy_ns = _make_namespace(archive_after_seconds=3600)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[absurd_ns, healthy_ns], find_expired_side_effect=[[]]
    )
    memory_chunk_repo.find_archivable_in_namespace = AsyncMock(return_value=[])

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.archive_after_seconds_count == 0
    assert memory_chunk_repo.find_archivable_in_namespace.await_count == 1


# ─── Namespace resolution (P3) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_falls_back_to_a_direct_namespace_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`list_all()` is capped, so its map can be truncated. A missing entry
    used to raise `KeyError` into a blanket `except`, leaving those chunks
    unarchived forever with an undiagnosable log (code review Story 3.3, P3)."""
    namespace = _make_namespace()
    chunk = _make_chunk(namespace_id=namespace.id)
    worker, _memory_chunk_repo, namespace_repo = _make_worker(
        namespaces=[],  # truncated listing: the chunk's namespace is absent
        find_expired_side_effect=[[chunk], []],
    )
    namespace_repo.get_by_id = AsyncMock(return_value=namespace)
    published = _patch_event_bus(monkeypatch)

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 1
    namespace_repo.get_by_id.assert_awaited_once_with(chunk.namespace_id)
    assert published[0][1].namespace == namespace.name


@pytest.mark.asyncio
async def test_archive_fails_cleanly_when_the_namespace_is_truly_gone() -> None:
    namespace = _make_namespace()
    chunk = _make_chunk(namespace_id=namespace.id)
    worker, memory_chunk_repo, namespace_repo = _make_worker(
        namespaces=[], find_expired_side_effect=[[chunk], []]
    )
    namespace_repo.get_by_id = AsyncMock(return_value=None)

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 0
    memory_chunk_repo.mark_archived_in_session.assert_not_awaited()


# ─── Interruptibility (P7) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_stops_between_batches_once_stop_was_requested() -> None:
    """`stop()` had no way to end a long pass other than cancelling it,
    possibly mid-transaction. Both phases now check the flag between items
    (code review Story 3.3, P7)."""
    namespace = _make_namespace(archive_after_seconds=3600)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace],
        find_expired_side_effect=[[_make_chunk(namespace_id=namespace.id)], []],
        batch_size=1,
    )
    worker._stopping.set()

    with structlog.testing.capture_logs() as logs:
        summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 0
    # The batch was fetched, but no chunk was touched and no second fetch ran.
    memory_chunk_repo.mark_archived_in_session.assert_not_awaited()
    assert memory_chunk_repo.find_expired.await_count == 1
    # The AC3 phase never even started.
    memory_chunk_repo.find_archivable_in_namespace.assert_not_awaited()
    assert [entry for entry in logs if entry["event"] == "memory_manager.archival_run_interrupted"]


# ─── stop() time cap (P5) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_lets_the_loop_unwind_before_cancelling_it() -> None:
    """A pass that honours `_stopping` must finish on its own — cancelling a
    healthy task is what risks landing inside a transaction."""
    worker, _memory_chunk_repo, _ = _make_worker()
    running = asyncio.Event()

    async def _cooperative() -> None:
        running.set()
        await worker._stopping.wait()

    task = asyncio.create_task(_cooperative())
    worker._task = task
    await running.wait()

    await worker.stop()

    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_stop_gives_up_on_a_task_that_swallows_its_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`await self._task` used to be unbounded: a cancellation delivered
    during a rollback on a dead connection froze the whole shutdown
    (code review Story 3.3, P5)."""
    monkeypatch.setattr(ttl_module, "_STOP_GRACE_S", 0.01)
    monkeypatch.setattr(ttl_module, "_STOP_CANCEL_TIMEOUT_S", 0.01)
    worker, _memory_chunk_repo, _ = _make_worker()

    async def _hung() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(30)
        await asyncio.sleep(30)  # ignores the first cancellation, like a stuck rollback

    task = asyncio.create_task(_hung())
    worker._task = task

    with structlog.testing.capture_logs() as logs:
        await worker.stop()

    assert not task.done()
    assert [
        entry for entry in logs if entry["event"] == "memory_manager.archival_worker_stop_timeout"
    ]

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_surfaces_a_loop_task_that_died_on_its_own() -> None:
    """`contextlib.suppress(Exception)` used to hide this, and would not have
    caught a `CancelledError` anyway (it is a `BaseException`)."""
    worker, _memory_chunk_repo, _ = _make_worker()

    async def _boom() -> None:
        raise RuntimeError("loop died")

    task = asyncio.create_task(_boom())
    worker._task = task
    await asyncio.gather(task, return_exceptions=True)

    with structlog.testing.capture_logs() as logs:
        await worker.stop()  # must not re-raise

    assert [
        entry for entry in logs if entry["event"] == "memory_manager.archival_worker_shutdown_error"
    ]


# ─── Retry backoff (P8) ────────────────────────────────────────────


def test_next_delay_uses_the_nominal_cadence_after_a_healthy_pass() -> None:
    worker, _memory_chunk_repo, _ = _make_worker()
    assert worker._next_delay(0) == worker._interval_s


def test_next_delay_backs_off_progressively_after_failures() -> None:
    """A run that dies on a cold pool or a failover used to leave the TTL
    unenforced for a full 24 h (code review Story 3.3, P8)."""
    worker = MemoryArchivalWorker(session_factory=MagicMock(), interval_s=86_400.0)
    delays = [worker._next_delay(n) for n in (1, 2, 3, 4, 99)]

    assert delays == [60.0, 300.0, 900.0, 900.0, 900.0]
    assert all(delay < worker._interval_s for delay in delays)


def test_next_delay_never_waits_longer_than_the_nominal_interval() -> None:
    """A short `interval_s` must not end up with a failing worker running
    LESS often than a healthy one."""
    worker = MemoryArchivalWorker(session_factory=MagicMock(), interval_s=10.0)
    assert worker._next_delay(3) == 10.0


# ─── Prometheus metric (T4.2) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_increments_the_prometheus_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _make_namespace()
    chunk = _make_chunk(namespace_id=namespace.id)
    worker, _memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace], find_expired_side_effect=[[chunk], []]
    )
    _patch_event_bus(monkeypatch)

    before = (
        REGISTRY.get_sample_value(
            "agentive_memory_manager_chunks_archived_total", {"reason": "ttl_expired"}
        )
        or 0.0
    )

    await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    after = REGISTRY.get_sample_value(
        "agentive_memory_manager_chunks_archived_total", {"reason": "ttl_expired"}
    )
    assert after == before + 1
