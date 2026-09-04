"""Unit tests for ``MemoryManagerService`` — Story 3.1 T6.1.

Pattern: AsyncMock repos + a fake :class:`Embedder` (deterministic vectors,
no network) — mirrors ``tests/unit/playground/test_service.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from agentive_backend.features.memory_manager import service as service_module
from agentive_backend.features.memory_manager.service import (
    EMBEDDING_MODEL,
    MemoryManagerService,
)
from agentive_backend.shared.exceptions import DependencyError, InternalError, NotFoundError


def _make_namespace(
    *,
    name: str = "ns-test",
    retention_policy: dict | None = None,
    embedding_backend: str = "cloud",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        retention_policy=retention_policy or {},
        embedding_backend=embedding_backend,
    )


def _make_chunk(
    *,
    content: str = "hello",
    ttl_seconds: int | None = None,
    expires_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        content=content,
        ttl_seconds=ttl_seconds,
        expires_at=expires_at,
        created_at=datetime.now(UTC),
    )


def _make_service(
    *,
    namespace: SimpleNamespace | None,
    namespace_not_found: bool = False,
    created_chunk: SimpleNamespace | None = None,
    search_rows: list[tuple[SimpleNamespace, float]] | None = None,
) -> tuple[MemoryManagerService, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    namespace_repo = AsyncMock()
    if namespace_not_found:
        namespace_repo.require_by_name = AsyncMock(
            side_effect=NotFoundError(detail="Namespace 'ghost' not found", context={})
        )
    else:
        namespace_repo.require_by_name = AsyncMock(return_value=namespace)

    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.create = AsyncMock(return_value=created_chunk)

    chunk_embedding_repo = AsyncMock()
    chunk_embedding_repo.upsert = AsyncMock(return_value=None)
    chunk_embedding_repo.search_ann = AsyncMock(return_value=search_rows or [])

    embedder = AsyncMock()
    embedder.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    # Deterministic default so tests that don't care about `source` get a
    # real string rather than an auto-vivified child Mock.
    embedder.provider_name = "fake"

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=chunk_embedding_repo,
        namespace_repo=namespace_repo,
        embedder=embedder,
    )
    return service, namespace_repo, memory_chunk_repo, chunk_embedding_repo, embedder


# ─── create_chunk ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_chunk_no_ttl_no_policy_never_expires() -> None:
    namespace = _make_namespace(retention_policy={})
    chunk = _make_chunk(ttl_seconds=None, expires_at=None)
    service, _ns_repo, chunk_repo, embedding_repo, embedder = _make_service(
        namespace=namespace, created_chunk=chunk
    )

    result = await service.create_chunk(
        namespace_name=namespace.name, content="hello", ttl_seconds=None
    )

    assert result.expires_at is None
    create_kwargs = chunk_repo.create.await_args.kwargs
    assert create_kwargs["expires_at"] is None
    embedder.embed.assert_awaited_once_with(["hello"], model=EMBEDDING_MODEL)
    embedding_repo.upsert.assert_awaited_once()
    assert result.embedding_model == EMBEDDING_MODEL
    assert result.namespace == namespace.name
    assert result.chunk_id == chunk.id


@pytest.mark.asyncio
async def test_create_chunk_ttl_override_wins_over_policy_default() -> None:
    """A per-chunk ``ttl`` overrides the namespace policy default."""
    namespace = _make_namespace(retention_policy={"default_ttl_seconds": 999_999})
    chunk = _make_chunk(ttl_seconds=60)
    service, _ns_repo, chunk_repo, _embedding_repo, _embedder = _make_service(
        namespace=namespace, created_chunk=chunk
    )

    before = datetime.now(UTC)
    await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=60)
    after = datetime.now(UTC)

    create_kwargs = chunk_repo.create.await_args.kwargs
    expires_at = create_kwargs["expires_at"]
    assert expires_at is not None
    # ttl_override=60s wins over the policy's 999_999s default.
    assert before + timedelta(seconds=60) <= expires_at <= after + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_create_chunk_falls_back_to_policy_default_ttl_when_no_override() -> None:
    namespace = _make_namespace(retention_policy={"default_ttl_seconds": 3600})
    chunk = _make_chunk(ttl_seconds=None)
    service, _ns_repo, chunk_repo, _embedding_repo, _embedder = _make_service(
        namespace=namespace, created_chunk=chunk
    )

    before = datetime.now(UTC)
    result = await service.create_chunk(
        namespace_name=namespace.name, content="hello", ttl_seconds=None
    )
    after = datetime.now(UTC)

    expires_at = chunk_repo.create.await_args.kwargs["expires_at"]
    assert expires_at is not None
    assert before + timedelta(seconds=3600) <= expires_at <= after + timedelta(seconds=3600)
    # BS3 : the stored row keeps the raw override (`None`, checked via
    # `chunk_repo.create`'s kwargs above), but the response must surface the
    # TTL actually applied, or a caller reading `ttl_seconds: null` wrongly
    # concludes the chunk never expires even though `expires_at` is set.
    assert result.ttl_seconds == 3600


@pytest.mark.asyncio
async def test_create_chunk_unknown_namespace_raises_not_found() -> None:
    service, _ns_repo, chunk_repo, embedding_repo, embedder = _make_service(
        namespace=None, namespace_not_found=True
    )

    with pytest.raises(NotFoundError):
        await service.create_chunk(namespace_name="ghost", content="hello", ttl_seconds=None)

    chunk_repo.create.assert_not_awaited()
    embedding_repo.upsert.assert_not_awaited()
    embedder.embed.assert_not_awaited()


# ─── search ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_maps_repo_rows_to_dto() -> None:
    namespace = _make_namespace()
    chunk_a = _make_chunk(content="alpha")
    chunk_b = _make_chunk(content="beta")
    service, _ns_repo, _chunk_repo, embedding_repo, embedder = _make_service(
        namespace=namespace,
        search_rows=[(chunk_a, 0.9), (chunk_b, 0.5)],
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert [r.content for r in results] == ["alpha", "beta"]
    assert [r.score for r in results] == [0.9, 0.5]
    assert all(r.namespace == namespace.name for r in results)
    embedder.embed.assert_awaited_once_with(["q"], model=EMBEDDING_MODEL)
    search_kwargs = embedding_repo.search_ann.await_args.kwargs
    assert search_kwargs["model"] == EMBEDDING_MODEL
    assert search_kwargs["namespace_id"] == namespace.id
    assert search_kwargs["top_k"] == 5


@pytest.mark.asyncio
async def test_search_unknown_namespace_raises_not_found() -> None:
    service, _ns_repo, _chunk_repo, embedding_repo, embedder = _make_service(
        namespace=None, namespace_not_found=True
    )

    with pytest.raises(NotFoundError):
        await service.search(namespace_name="ghost", query="q", top_k=5)

    embedding_repo.search_ann.assert_not_awaited()
    embedder.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_no_results_returns_empty_list() -> None:
    namespace = _make_namespace()
    service, *_rest = _make_service(namespace=namespace, search_rows=[])

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert results == []


# ─── Code review Story 3.1 — guards added by the patch batch ─────


@pytest.mark.parametrize(
    "bad_policy",
    [
        pytest.param({"default_ttl_seconds": "3600"}, id="ttl-as-json-string"),
        pytest.param({"default_ttl_seconds": -1}, id="negative-ttl"),
        pytest.param([1, 2], id="policy-is-not-an-object"),
        pytest.param("none", id="policy-is-a-scalar"),
        pytest.param({"default_ttl_seconds": 10**18}, id="ttl-overflows-datetime"),
    ],
)
@pytest.mark.asyncio
async def test_create_chunk_malformed_retention_policy_raises_rfc7807(bad_policy: object) -> None:
    """P4: `namespaces.retention_policy` is free-shape JSONB with no CHECK,
    so each of these used to escape the service as a raw 500 traceback."""
    namespace = _make_namespace(retention_policy=bad_policy)  # type: ignore[arg-type]
    service, _ns, chunk_repo, _emb, _embedder = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )

    with pytest.raises(InternalError) as exc:
        await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    assert exc.value.status == 500
    assert "retention_policy" in (exc.value.detail or "")
    # The guard fires BEFORE the insert — no orphan row is left behind.
    chunk_repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_chunk_empty_embedding_result_raises_503() -> None:
    """P5: the `Embedder` protocol pins ordering but never cardinality."""
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, embedder = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )
    embedder.embed = AsyncMock(return_value=[])

    with pytest.raises(DependencyError) as exc:
        await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    assert exc.value.status == 503
    embedding_repo.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_empty_embedding_result_raises_503() -> None:
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, embedder = _make_service(namespace=namespace)
    embedder.embed = AsyncMock(return_value=[])

    with pytest.raises(DependencyError):
        await service.search(namespace_name=namespace.name, query="hello", top_k=5)

    embedding_repo.search_ann.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_warns_when_namespace_backend_is_not_cloud() -> None:
    """P10: the warning used to fire only on the write path, while search
    forces the very same cloud model."""
    namespace = _make_namespace(embedding_backend="local")
    service, _ns, _chunk_repo, _emb, _embedder = _make_service(namespace=namespace)

    with patch.object(service_module._log, "warning") as warn:
        await service.search(namespace_name=namespace.name, query="hello", top_k=5)

    warn.assert_called_once()
    assert warn.call_args.args[0] == "memory_manager.embedding_backend_not_cloud"
    assert warn.call_args.kwargs["embedding_backend"] == "local"


# ─── Code review Story 3.1 — intent gaps ──────────────────────


@pytest.mark.asyncio
async def test_create_chunk_embeds_before_writing_the_chunk_row() -> None:
    """IG1 — the embedder must run before any DB write. A provider failure
    (429/timeout/5xx) must leave nothing behind, not a chunk with no
    embedding."""
    namespace = _make_namespace()
    service, _ns, chunk_repo, embedding_repo, embedder = _make_service(namespace=namespace)
    embedder.embed = AsyncMock(side_effect=RuntimeError("provider rate-limited"))

    with pytest.raises(RuntimeError):
        await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    chunk_repo.create.assert_not_awaited()
    embedding_repo.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_chunk_compensates_orphan_chunk_when_upsert_fails() -> None:
    """IG1 — if the chunk row DID get written but the embedding upsert then
    fails, the chunk must not be left behind as an orphan invisible to
    search_ann's INNER JOIN."""
    namespace = _make_namespace()
    chunk = _make_chunk()
    service, _ns, chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, created_chunk=chunk
    )
    embedding_repo.upsert = AsyncMock(side_effect=RuntimeError("db connection lost"))
    chunk_repo.delete_by_id = AsyncMock()

    with pytest.raises(RuntimeError):
        await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    chunk_repo.delete_by_id.assert_awaited_once_with(chunk.id, tenant_id=None)


@pytest.mark.asyncio
async def test_create_chunk_compensation_failure_does_not_mask_the_original_error() -> None:
    """IG1 — if even the compensating delete fails, the original upsert
    error is still what the caller sees (best-effort cleanup, not a new
    failure mode)."""
    namespace = _make_namespace()
    service, _ns, chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )
    embedding_repo.upsert = AsyncMock(side_effect=RuntimeError("original failure"))
    chunk_repo.delete_by_id = AsyncMock(side_effect=RuntimeError("compensation also failed"))

    with pytest.raises(RuntimeError, match="original failure"):
        await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)


@pytest.mark.asyncio
async def test_create_chunk_passes_embedder_provider_name_as_source() -> None:
    """IG3 — the embedding row must record which embedder produced it, so a
    mock-derived vector is distinguishable from a real one after the fact."""
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, embedder = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )
    embedder.provider_name = "mock"

    await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    assert embedding_repo.upsert.await_args.kwargs["source"] == "mock"


@pytest.mark.asyncio
async def test_create_chunk_source_is_none_when_embedder_has_no_provider_name() -> None:
    """IG3 — a bare-minimum `Embedder` conformer (the protocol does not
    require `provider_name`) must not crash the write path."""
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, embedder = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )
    del embedder.provider_name

    await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    assert embedding_repo.upsert.await_args.kwargs["source"] is None


@pytest.mark.asyncio
async def test_search_passes_now_through_to_search_ann() -> None:
    """IG2 — `search()` must forward a `now` so `search_ann` can exclude
    expired chunks; a missing/None value would silently disable the
    filter's default only inside the repo, not make it inconsistent across
    calls within the same request."""
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(namespace=namespace)

    await service.search(namespace_name=namespace.name, query="hello", top_k=5)

    assert isinstance(embedding_repo.search_ann.await_args.kwargs["now"], datetime)
