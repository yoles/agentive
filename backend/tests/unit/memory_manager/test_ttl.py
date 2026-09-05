"""Unit tests for :class:`MemoryArchivalWorker` (Story 3.3 T10.4).

Pattern: AsyncMock repos, monkeypatched ``publish``/``notify_best_effort``
(mirrors ``tests/unit/memory_manager/test_service.py``). Only ``run_once``
is exercised — never ``start()``/``stop()`` with the real 86400s interval
(this story's Testing Standards Summary).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
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
) -> tuple[MemoryArchivalWorker, AsyncMock, AsyncMock]:
    worker = MemoryArchivalWorker(session_factory=MagicMock())

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
async def test_run_once_breaks_early_when_a_batch_makes_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every chunk in a batch fails to archive, the exact same batch
    would be re-fetched forever (still `archived_at IS NULL`) — the worker
    must stop instead of spinning until `_MAX_BATCHES_PER_RUN`."""
    monkeypatch.setattr(ttl_module, "_MAX_BATCHES_PER_RUN", 5)
    namespace = _make_namespace()
    chunk = _make_chunk(namespace_id=namespace.id)
    worker, memory_chunk_repo, _ = _make_worker(
        namespaces=[namespace],
        find_expired_side_effect=[[chunk]] * 10,
        mark_archived_return=0,
    )

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.ttl_expired_count == 0
    assert memory_chunk_repo.find_expired.await_count == 1


@pytest.mark.asyncio
async def test_run_once_stops_at_max_batches_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ttl_module, "_MAX_BATCHES_PER_RUN", 3)
    namespace = _make_namespace()
    worker, memory_chunk_repo, _ = _make_worker(namespaces=[namespace])
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
    bad_ns = SimpleNamespace(
        id=uuid4(), name="bad-ns", retention_policy={"archive_after_seconds": "not-an-int"}
    )
    worker, memory_chunk_repo, _ = _make_worker(namespaces=[bad_ns], find_expired_side_effect=[[]])

    summary = await worker.run_once(now=datetime(2030, 1, 1, tzinfo=UTC))

    assert summary.archive_after_seconds_count == 0
    memory_chunk_repo.find_archivable_in_namespace.assert_not_awaited()


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
