"""Unit tests for ``MemoryManagerService`` — Story 3.1 T6.1.

Pattern: AsyncMock repos + a fake :class:`Embedder` (deterministic vectors,
no network) — mirrors ``tests/unit/playground/test_service.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from agentive_backend.features.memory_manager import service as service_module
from agentive_backend.features.memory_manager.domain.value_objects import (
    DecayFunction,
    DecayPolicy,
    EmbeddingBackend,
)
from agentive_backend.features.memory_manager.service import MemoryManagerService
from agentive_backend.shared.exceptions import (
    DependencyError,
    ForbiddenError,
    InternalError,
    NotFoundError,
)

# Story 3.6 — `EMBEDDING_MODEL` no longer lives on the service (T7.4); the
# model name is now whatever `EmbeddingRouter.embed()` resolves to. Tests
# use this local constant purely for readability/fake-router return values.
_CLOUD_MODEL = "text-embedding-3-small"


def _make_namespace(
    *,
    name: str = "ns-test",
    retention_policy: dict | None = None,
    decay_policy: dict | None = None,
    embedding_backend: str = "cloud",
    department: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        retention_policy=retention_policy or {},
        # Story 3.4 — `{}` is the column default and means "no decay", so
        # every pre-3.4 assertion in this module keeps its exact meaning.
        decay_policy=decay_policy or {},
        embedding_backend=embedding_backend,
        department=department,
        project=None,
        type="metier",
        created_at=datetime.now(UTC),
    )


def _configure_with_tenant(repo: MagicMock, session: MagicMock) -> None:
    """Wire ``repo.with_tenant(...)`` as an async context manager yielding
    ``session`` — mirror of the pattern already used by
    ``tests/unit/tool_hub/test_service.py``."""
    repo.with_tenant = MagicMock()
    repo.with_tenant.return_value.__aenter__ = AsyncMock(return_value=session)
    repo.with_tenant.return_value.__aexit__ = AsyncMock(return_value=False)


def _make_chunk(
    *,
    content: str = "hello",
    ttl_seconds: int | None = None,
    expires_at: datetime | None = None,
    archived_at: datetime | None = None,
    created_at: datetime | None = None,
    namespace_id: object = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        namespace_id=namespace_id or uuid4(),
        content=content,
        ttl_seconds=ttl_seconds,
        expires_at=expires_at,
        archived_at=archived_at,
        # Story 3.4 — the decay reranker reads `created_at`, so it has to be
        # steerable to assert an ordering at an exact age.
        created_at=created_at or datetime.now(UTC),
    )


def _make_service(
    *,
    namespace: SimpleNamespace | None,
    namespace_not_found: bool = False,
    created_chunk: SimpleNamespace | None = None,
    search_rows: list[tuple[SimpleNamespace, float]] | None = None,
) -> tuple[MemoryManagerService, MagicMock, AsyncMock, AsyncMock, AsyncMock]:
    namespace_repo = MagicMock()
    if namespace_not_found:
        namespace_repo.require_by_name = AsyncMock(
            side_effect=NotFoundError(detail="Namespace 'ghost' not found", context={})
        )
    else:
        namespace_repo.require_by_name = AsyncMock(return_value=namespace)
    _configure_with_tenant(namespace_repo, MagicMock())

    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.create = AsyncMock(return_value=created_chunk)

    chunk_embedding_repo = AsyncMock()
    chunk_embedding_repo.upsert = AsyncMock(return_value=None)
    chunk_embedding_repo.search_ann = AsyncMock(return_value=search_rows or [])

    # Fake `EmbeddingRouter` — `embed()` returns the same 3-tuple contract
    # (vectors, model_name, provider_name) the real router does (T4.3).
    # Deterministic `"fake"` provider name so tests that don't care about
    # `source` get a real string rather than an auto-vivified child Mock.
    embedding_router = AsyncMock()
    embedding_router.embed = AsyncMock(return_value=([[0.1, 0.2, 0.3]], _CLOUD_MODEL, "fake"))

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=chunk_embedding_repo,
        namespace_repo=namespace_repo,
        embedding_router=embedding_router,
    )
    return service, namespace_repo, memory_chunk_repo, chunk_embedding_repo, embedding_router


# ─── create_chunk ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_chunk_no_ttl_no_policy_never_expires() -> None:
    namespace = _make_namespace(retention_policy={})
    chunk = _make_chunk(ttl_seconds=None, expires_at=None)
    service, _ns_repo, chunk_repo, embedding_repo, embedding_router = _make_service(
        namespace=namespace, created_chunk=chunk
    )

    result = await service.create_chunk(
        namespace_name=namespace.name, content="hello", ttl_seconds=None
    )

    assert result.expires_at is None
    create_kwargs = chunk_repo.create.await_args.kwargs
    assert create_kwargs["expires_at"] is None
    embedding_router.embed.assert_awaited_once_with(["hello"], backend="cloud")
    embedding_repo.upsert.assert_awaited_once()
    assert result.embedding_model == _CLOUD_MODEL
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
    service, _ns_repo, chunk_repo, embedding_repo, embedding_router = _make_service(
        namespace=None, namespace_not_found=True
    )

    with pytest.raises(NotFoundError):
        await service.create_chunk(namespace_name="ghost", content="hello", ttl_seconds=None)

    chunk_repo.create.assert_not_awaited()
    embedding_repo.upsert.assert_not_awaited()
    embedding_router.embed.assert_not_awaited()


# ─── search ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_maps_repo_rows_to_dto() -> None:
    namespace = _make_namespace()
    chunk_a = _make_chunk(content="alpha")
    chunk_b = _make_chunk(content="beta")
    service, _ns_repo, _chunk_repo, embedding_repo, embedding_router = _make_service(
        namespace=namespace,
        search_rows=[(chunk_a, 0.9), (chunk_b, 0.5)],
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert [r.content for r in results] == ["alpha", "beta"]
    assert [r.score for r in results] == [0.9, 0.5]
    assert all(r.namespace == namespace.name for r in results)
    embedding_router.embed.assert_awaited_once_with(["q"], backend="cloud")
    search_kwargs = embedding_repo.search_ann.await_args.kwargs
    assert search_kwargs["model"] == _CLOUD_MODEL
    assert search_kwargs["namespace_id"] == namespace.id
    assert search_kwargs["top_k"] == 5
    assert search_kwargs["include_archived"] is False


@pytest.mark.asyncio
async def test_search_passes_include_archived_through_to_search_ann() -> None:
    """Story 3.3 AC2."""
    namespace = _make_namespace()
    service, _ns_repo, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(namespace_name=namespace.name, query="q", top_k=5, include_archived=True)

    assert embedding_repo.search_ann.await_args.kwargs["include_archived"] is True


@pytest.mark.asyncio
async def test_search_result_exposes_archived_at() -> None:
    """Story 3.3 AC2 — lets a caller distinguish a live hit from one only
    surfaced because `include_archived=true` was set."""
    namespace = _make_namespace()
    archived_at = datetime.now(UTC)
    chunk = _make_chunk(content="stale", archived_at=archived_at)
    service, _ns_repo, _chunk_repo, _embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[(chunk, 0.4)]
    )

    results = await service.search(
        namespace_name=namespace.name, query="q", top_k=5, include_archived=True
    )

    assert results[0].archived_at == archived_at


@pytest.mark.asyncio
async def test_search_unknown_namespace_raises_not_found() -> None:
    service, _ns_repo, _chunk_repo, embedding_repo, embedding_router = _make_service(
        namespace=None, namespace_not_found=True
    )

    with pytest.raises(NotFoundError):
        await service.search(namespace_name="ghost", query="q", top_k=5)

    embedding_repo.search_ann.assert_not_awaited()
    embedding_router.embed.assert_not_awaited()


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
    service, _ns, _chunk_repo, embedding_repo, embedding_router = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )
    embedding_router.embed = AsyncMock(return_value=([], _CLOUD_MODEL, "fake"))

    with pytest.raises(DependencyError) as exc:
        await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    assert exc.value.status == 503
    embedding_repo.upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_empty_embedding_result_raises_503() -> None:
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, embedding_router = _make_service(namespace=namespace)
    embedding_router.embed = AsyncMock(return_value=([], _CLOUD_MODEL, "fake"))

    with pytest.raises(DependencyError):
        await service.search(namespace_name=namespace.name, query="hello", top_k=5)

    embedding_repo.search_ann.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_resolves_the_backend_from_the_namespace() -> None:
    """Story 3.6 T7.3 — `search()` derives its `EmbeddingRouter.embed(backend=...)`
    argument from `namespace.embedding_backend`, not a hardcoded `"cloud"`
    (supersedes the removed `_warn_if_backend_not_cloud`, T7.5: this is now
    real routing, not a warning)."""
    namespace = _make_namespace(embedding_backend="local")
    service, _ns, _chunk_repo, _emb, embedding_router = _make_service(namespace=namespace)

    await service.search(namespace_name=namespace.name, query="hello", top_k=5)

    embedding_router.embed.assert_awaited_once_with(["hello"], backend="local")


# ─── Code review Story 3.1 — intent gaps ──────────────────────


@pytest.mark.asyncio
async def test_create_chunk_embeds_before_writing_the_chunk_row() -> None:
    """IG1 — the embedder must run before any DB write. A provider failure
    (429/timeout/5xx) must leave nothing behind, not a chunk with no
    embedding."""
    namespace = _make_namespace()
    service, _ns, chunk_repo, embedding_repo, embedding_router = _make_service(namespace=namespace)
    embedding_router.embed = AsyncMock(side_effect=RuntimeError("provider rate-limited"))

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
async def test_create_chunk_passes_the_routers_provider_name_as_source() -> None:
    """IG3 — the embedding row must record which provider produced it, so a
    mock-derived vector is distinguishable from a real one after the fact.
    Story 3.6 T4.3 moved this from `getattr(embedder, "provider_name")` to
    `EmbeddingRouter.embed()`'s own 3rd return value — the router is what
    resolved which underlying provider served the call."""
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, embedding_router = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )
    embedding_router.embed = AsyncMock(return_value=([[0.1, 0.2, 0.3]], _CLOUD_MODEL, "mock"))

    await service.create_chunk(namespace_name=namespace.name, content="hello", ttl_seconds=None)

    assert embedding_repo.upsert.await_args.kwargs["source"] == "mock"


@pytest.mark.asyncio
async def test_create_chunk_source_is_none_when_the_router_reports_none() -> None:
    """IG3 — a bare-minimum `Embedder` conformer (the protocol does not
    require `provider_name`) must not crash the write path;
    `EmbeddingRouter.embed()` itself falls back to `None` via `getattr`."""
    namespace = _make_namespace()
    service, _ns, _chunk_repo, embedding_repo, embedding_router = _make_service(
        namespace=namespace, created_chunk=_make_chunk()
    )
    embedding_router.embed = AsyncMock(return_value=([[0.1, 0.2, 0.3]], _CLOUD_MODEL, None))

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


# ─── Story 3.2 AC2 — department isolation ─────────────────────────


def _patch_event_bus(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def _fake_publish(event_type: str, event: object, **_kw: object) -> object:
        captured["event_type"] = event_type
        captured["event"] = event
        return uuid4()

    monkeypatch.setattr(service_module, "publish", _fake_publish)
    monkeypatch.setattr(service_module, "notify_best_effort", AsyncMock())
    return captured


@pytest.mark.asyncio
async def test_search_denies_cross_department_access(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_event_bus(monkeypatch)
    namespace = _make_namespace(department="Dev")
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(namespace=namespace)

    with pytest.raises(ForbiddenError):
        await service.search(
            namespace_name=namespace.name,
            query="q",
            top_k=5,
            acting_department="Design-UX",
        )

    embedding_repo.search_ann.assert_not_awaited()
    assert captured["event_type"] == "memory_manager.namespace.access_denied"
    assert captured["event"].namespace_department == "Dev"  # type: ignore[attr-defined]
    assert captured["event"].acting_department == "Design-UX"  # type: ignore[attr-defined]
    assert captured["event"].operation == "search"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_create_chunk_denies_cross_department_access(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_event_bus(monkeypatch)
    namespace = _make_namespace(department="Dev")
    service, _ns, chunk_repo, _embedding_repo, embedding_router = _make_service(namespace=namespace)

    with pytest.raises(ForbiddenError):
        await service.create_chunk(
            namespace_name=namespace.name,
            content="hello",
            ttl_seconds=None,
            acting_department="Design-UX",
        )

    chunk_repo.create.assert_not_awaited()
    embedding_router.embed.assert_not_awaited()
    assert captured["event"].operation == "create_chunk"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_search_allows_same_department() -> None:
    namespace = _make_namespace(department="Dev")
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(namespace_name=namespace.name, query="q", top_k=5, acting_department="Dev")

    embedding_repo.search_ann.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_allows_same_department_case_insensitive() -> None:
    """Product decision (code review Story 3.2, IG1): this header is not a

    real security boundary before Growth RBAC, so a case mismatch must not
    turn into a spurious 403.
    """
    namespace = _make_namespace(department="Dev")
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(namespace_name=namespace.name, query="q", top_k=5, acting_department="dev")

    embedding_repo.search_ann.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_allows_same_department_with_stray_whitespace() -> None:
    """A trailing space in either value (typo, copy-paste) must not turn

    into a spurious 403 (code review Story 3.2, IG1).
    """
    namespace = _make_namespace(department="Dev")
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(
        namespace_name=namespace.name, query="q", top_k=5, acting_department=" Dev "
    )

    embedding_repo.search_ann.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_allows_cross_department_when_namespace_has_no_department() -> None:
    namespace = _make_namespace(department=None)
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(
        namespace_name=namespace.name, query="q", top_k=5, acting_department="Design-UX"
    )

    embedding_repo.search_ann.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_require_shared_namespace_refuses_a_department_scoped_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IG1 (revue Story 3.5) : a caller with NO department identity at all
    (Push Memory) may only read shared namespaces. Absent this flag, the
    ``acting_department is None`` early return let it read every department's
    namespaces, which is exactly what AC2 forbids over HTTP.

    No ``NamespaceAccessDeniedEvent`` here, deliberately: this path fires on
    every run of a misconfigured template, so it would emit an unbounded
    stream of outbox rows for one config mistake.
    """
    captured = _patch_event_bus(monkeypatch)
    namespace = _make_namespace(department="Dev")
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(namespace=namespace)

    with pytest.raises(ForbiddenError):
        await service.search(
            namespace_name=namespace.name,
            query="q",
            top_k=5,
            require_shared_namespace=True,
        )

    embedding_repo.search_ann.assert_not_awaited()
    assert captured == {}


@pytest.mark.asyncio
async def test_search_require_shared_namespace_allows_a_shared_one() -> None:
    """IG1 : the flag is fail-closed on department-scoped namespaces only.
    A namespace with no department stays readable, which is the nominal
    Push Memory case."""
    namespace = _make_namespace(department=None)
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(
        namespace_name=namespace.name, query="q", top_k=5, require_shared_namespace=True
    )

    embedding_repo.search_ann.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_allows_when_no_acting_department_declared() -> None:
    """Backward compatibility — every existing Story 3.1 call site (no
    ``X-Acting-Department`` header) must keep working unrestricted."""
    namespace = _make_namespace(department="Dev")
    service, _ns, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(namespace_name=namespace.name, query="q", top_k=5)

    embedding_repo.search_ann.assert_awaited_once()


@pytest.mark.asyncio
async def test_denied_access_commits_the_audit_event_before_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5.3 — the outbox INSERT must be committed (the `with_tenant` block
    exited) before `ForbiddenError` is raised, or the audit trail row would
    roll back along with an exception raised inside the block."""
    events: list[str] = []

    async def _fake_publish(event_type: str, event: object, **_kw: object) -> object:
        events.append("published")
        return uuid4()

    monkeypatch.setattr(service_module, "publish", _fake_publish)
    monkeypatch.setattr(service_module, "notify_best_effort", AsyncMock())

    namespace = _make_namespace(department="Dev")
    service, ns_repo, _chunk_repo, _embedding_repo, _embedder = _make_service(namespace=namespace)

    aexit_calls: list[str] = []
    original_aexit = ns_repo.with_tenant.return_value.__aexit__

    async def _tracking_aexit(*args: object) -> bool | None:
        aexit_calls.append("committed")
        return await original_aexit(*args)

    ns_repo.with_tenant.return_value.__aexit__ = _tracking_aexit

    with pytest.raises(ForbiddenError):
        await service.search(
            namespace_name=namespace.name, query="q", top_k=5, acting_department="Design-UX"
        )

    assert events == ["published"]
    assert aexit_calls == ["committed"]


# ─── Story 3.2 AC1 — create_namespace ─────────────────────────────


@pytest.mark.asyncio
async def test_create_namespace_uses_type_default_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_event_bus(monkeypatch)
    namespace_repo = MagicMock()
    session = MagicMock()
    _configure_with_tenant(namespace_repo, session)
    created = _make_namespace(name="dev-notes", department="Dev", retention_policy={})
    created.type = "metier"
    namespace_repo.create_in_session = AsyncMock(return_value=created)

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.create_namespace(name="dev-notes", ns_type="metier", department="Dev")

    create_kwargs = namespace_repo.create_in_session.await_args.kwargs
    assert create_kwargs["retention_policy"] == {"default_ttl_seconds": 31_536_000}
    assert result.retention_policy == {
        "default_ttl_seconds": 31_536_000,
        "archive_after_seconds": None,
    }
    assert result.name == "dev-notes"


@pytest.mark.asyncio
async def test_create_namespace_override_replaces_type_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentive_backend.features.memory_manager.domain.value_objects import RetentionPolicy

    _patch_event_bus(monkeypatch)
    namespace_repo = MagicMock()
    _configure_with_tenant(namespace_repo, MagicMock())
    created = _make_namespace(name="client-acme")
    created.type = "client"
    namespace_repo.create_in_session = AsyncMock(return_value=created)

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    override = RetentionPolicy(default_ttl_seconds=3600)
    await service.create_namespace(
        name="client-acme", ns_type="client", retention_policy_override=override
    )

    create_kwargs = namespace_repo.create_in_session.await_args.kwargs
    assert create_kwargs["retention_policy"] == {"default_ttl_seconds": 3600}


@pytest.mark.asyncio
async def test_create_namespace_propagates_conflict_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentive_backend.shared.exceptions import ConflictError

    _patch_event_bus(monkeypatch)
    namespace_repo = MagicMock()
    _configure_with_tenant(namespace_repo, MagicMock())
    namespace_repo.create_in_session = AsyncMock(
        side_effect=ConflictError(detail="Namespace 'dup' already exists", context={})
    )

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    with pytest.raises(ConflictError):
        await service.create_namespace(name="dup", ns_type="client")


@pytest.mark.asyncio
async def test_create_namespace_unknown_type_raises_validation_error() -> None:
    """The HTTP router already restricts `type` via Pydantic — this covers

    a non-HTTP caller passing a raw string outside the 4 known types,
    which used to surface as an opaque `ValueError`/500 instead of a typed
    domain error (code review Story 3.2, P7).
    """
    from agentive_backend.shared.exceptions import ValidationError

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=AsyncMock(),
        embedding_router=AsyncMock(),
    )

    with pytest.raises(ValidationError):
        await service.create_namespace(name="ns", ns_type="bogus")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_namespace_defaults_embedding_backend_to_cloud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 3.6 T7.6 — omitting `embedding_backend` keeps pre-3.6 behaviour."""
    _patch_event_bus(monkeypatch)
    namespace_repo = MagicMock()
    _configure_with_tenant(namespace_repo, MagicMock())
    namespace_repo.create_in_session = AsyncMock(return_value=_make_namespace(name="ns-default"))

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    await service.create_namespace(name="ns-default", ns_type="metier")

    assert namespace_repo.create_in_session.await_args.kwargs["embedding_backend"] == "cloud"


@pytest.mark.asyncio
async def test_create_namespace_forwards_an_explicit_embedding_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 3.6 AC2 — `embedding_backend=local` is persisted as its raw
    string value (`NamespaceRepo.create_in_session` takes `str`, not the
    domain enum)."""
    _patch_event_bus(monkeypatch)
    namespace_repo = MagicMock()
    _configure_with_tenant(namespace_repo, MagicMock())
    namespace_repo.create_in_session = AsyncMock(
        return_value=_make_namespace(name="ns-local", embedding_backend="local")
    )

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.create_namespace(
        name="ns-local", ns_type="metier", embedding_backend=EmbeddingBackend.LOCAL
    )

    assert namespace_repo.create_in_session.await_args.kwargs["embedding_backend"] == "local"
    assert result.embedding_backend == "local"


# ─── Story 3.2 AC3 — list_namespaces ───────────────────────────────


@pytest.mark.asyncio
async def test_list_namespaces_zips_chunk_counts() -> None:
    ns_a = _make_namespace(name="a", retention_policy={"default_ttl_seconds": 60})
    ns_b = _make_namespace(name="b")
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=[ns_a, ns_b])
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={ns_a.id: 7})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.list_namespaces()

    by_name = {view.name: view for view in result}
    assert by_name["a"].chunk_count == 7
    assert by_name["a"].retention_policy["default_ttl_seconds"] == 60
    assert by_name["a"].retention_policy_valid is True
    assert by_name["b"].chunk_count == 0  # absent from the counts dict


@pytest.mark.asyncio
async def test_list_namespaces_tolerates_malformed_retention_policy() -> None:
    """A hand-seeded/legacy row with a malformed JSONB must not 500 the
    entire admin listing (same P4 class of defensive guard as create_chunk)."""
    ns_bad = _make_namespace(name="legacy", retention_policy="not-a-dict")  # type: ignore[arg-type]
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=[ns_bad])
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.list_namespaces()

    assert result[0].retention_policy == {
        "default_ttl_seconds": None,
        "archive_after_seconds": None,
    }
    assert result[0].retention_policy_valid is False


@pytest.mark.asyncio
async def test_list_namespaces_tolerates_non_int_ttl() -> None:
    """`from_mapping`'s own checks let a float TTL through (only the view's

    `int` validation catches it, since a fractional float fails coercion) —
    that failure must be caught at the same point as any other malformed
    policy (code review Story 3.2, P1), not 500 the whole listing. A bool
    TTL is not in this category: Pydantic's lax `int` coercion silently
    accepts it (`True` -> `1`), so it never raises here.
    """
    ns_bad = _make_namespace(
        name="legacy-float",
        retention_policy={"default_ttl_seconds": 1.5},  # type: ignore[arg-type]
    )
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=[ns_bad])
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.list_namespaces()

    assert result[0].retention_policy == {
        "default_ttl_seconds": None,
        "archive_after_seconds": None,
    }
    assert result[0].retention_policy_valid is False


@pytest.mark.asyncio
async def test_list_namespaces_warns_when_hitting_the_safety_cap() -> None:
    """AC3 promises "tous les namespaces" — hitting the repo's safety cap

    must never be silent (code review Story 3.2, BS3).
    """
    from agentive_backend.shared.repositories.namespace_repo import (
        NAMESPACE_LISTING_SAFETY_CAP,
    )

    namespaces = [_make_namespace(name=f"ns-{i}") for i in range(NAMESPACE_LISTING_SAFETY_CAP)]
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=namespaces)
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    with patch.object(service_module._log, "warning") as warn:
        await service.list_namespaces()

    warn.assert_called_once()
    assert warn.call_args.args[0] == "memory_manager.namespace_listing_hit_safety_cap"


@pytest.mark.asyncio
async def test_list_namespaces_does_not_warn_below_the_safety_cap() -> None:
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=[_make_namespace(name="ns")])
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    with patch.object(service_module._log, "warning") as warn:
        await service.list_namespaces()

    warn.assert_not_called()


# ─── search — décroissance temporelle (Story 3.4) ─────────────────

_EXPONENTIAL_1D = {"function": "exponential", "half_life_seconds": 86_400}


@pytest.mark.asyncio
async def test_search_without_decay_policy_does_not_oversample() -> None:
    """T9.7's guarantee at the service level: a namespace with no

    `decay_policy` pays exactly the ANN cost it paid before Story 3.4.
    """
    namespace = _make_namespace()
    service, _ns_repo, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert embedding_repo.search_ann.await_args.kwargs["top_k"] == 5


@pytest.mark.asyncio
async def test_search_oversamples_the_ann_query_when_decay_is_configured() -> None:
    """THE guard of this story. `search_ann` truncates in SQL, so without a

    bigger `top_k` the rerank could only permute an already-cut set and AC2
    would be false end to end while every unit test still passed. Do not
    delete this test (story Dev Notes § "le vrai piège").
    """
    namespace = _make_namespace(decay_policy=_EXPONENTIAL_1D)
    service, _ns_repo, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert embedding_repo.search_ann.await_args.kwargs["top_k"] == 50


@pytest.mark.asyncio
async def test_search_oversampling_is_capped() -> None:
    namespace = _make_namespace(decay_policy=_EXPONENTIAL_1D)
    service, _ns_repo, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[]
    )

    await service.search(namespace_name=namespace.name, query="q", top_k=50)

    assert embedding_repo.search_ann.await_args.kwargs["top_k"] == 200


@pytest.mark.asyncio
async def test_search_rerank_false_skips_oversampling_and_decay() -> None:
    namespace = _make_namespace(decay_policy=_EXPONENTIAL_1D)
    old = _make_chunk(content="old", created_at=datetime.now(UTC) - timedelta(days=30))
    service, _ns_repo, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[(old, 0.9)]
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5, rerank=False)

    assert embedding_repo.search_ann.await_args.kwargs["top_k"] == 5
    assert results[0].score == pytest.approx(0.9)
    assert results[0].similarity == pytest.approx(0.9)
    assert results[0].decay_factor == 1.0


@pytest.mark.asyncio
async def test_search_applies_decay_and_reorders_results() -> None:
    """AC2 — at close similarity the recent chunk comes out on top."""
    namespace = _make_namespace(decay_policy=_EXPONENTIAL_1D)
    now = datetime.now(UTC)
    old = _make_chunk(content="old", created_at=now - timedelta(days=3))
    recent = _make_chunk(content="recent", created_at=now)
    service, _ns_repo, _chunk_repo, _embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[(old, 0.90), (recent, 0.85)]
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert [r.content for r in results] == ["recent", "old"]
    assert results[0].score == pytest.approx(0.85, abs=1e-3)
    assert results[1].decay_factor == pytest.approx(0.125, abs=1e-3)


@pytest.mark.asyncio
async def test_search_truncates_to_top_k_after_reranking() -> None:
    namespace = _make_namespace(decay_policy=_EXPONENTIAL_1D)
    rows = [(_make_chunk(content=f"c{i}"), 0.9 - i / 100) for i in range(8)]
    service, _ns_repo, _chunk_repo, _embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=rows
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=3)

    assert len(results) == 3


@pytest.mark.asyncio
async def test_search_tolerates_a_malformed_decay_policy() -> None:
    """AC3 — a corrupt JSONB degrades the ranking, it never 500s a read.

    Deliberately the opposite posture from `create_chunk`'s guard on
    `retention_policy`, which raises: a write must fail loudly, a read must
    keep serving.
    """
    namespace = _make_namespace(decay_policy={"function": "sigmoid"})
    chunk = _make_chunk(content="x", created_at=datetime.now(UTC) - timedelta(days=900))
    service, _ns_repo, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[(chunk, 0.7)]
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert results[0].decay_factor == 1.0
    assert results[0].score == pytest.approx(0.7)
    assert embedding_repo.search_ann.await_args.kwargs["top_k"] == 5


@pytest.mark.asyncio
async def test_search_uses_one_clock_for_the_expiry_filter_and_the_scoring() -> None:
    """T4.2 — two `datetime.now()` calls would let `search_ann` keep a chunk

    against one instant while the reranker ages it against another.
    """
    namespace = _make_namespace(decay_policy=_EXPONENTIAL_1D)
    created_at = datetime.now(UTC) - timedelta(days=1)
    chunk = _make_chunk(content="x", created_at=created_at)
    service, _ns_repo, _chunk_repo, embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[(chunk, 1.0)]
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5)

    filter_now = embedding_repo.search_ann.await_args.kwargs["now"]
    # The factor is `0.5 ** (age / 86400)` evaluated at that very instant —
    # recomputing it from `filter_now` must reproduce the returned value bit
    # for bit, which is only true if a single `now` fed both.
    expected = 0.5 ** ((filter_now - created_at).total_seconds() / 86_400)
    assert results[0].decay_factor == expected


@pytest.mark.asyncio
async def test_search_clamps_a_negative_similarity_end_to_end() -> None:
    """Cosine distance ranges over [0, 2], so `1 - distance` can be negative;

    without the clamp the product would promote the least relevant chunk.
    """
    namespace = _make_namespace(decay_policy=_EXPONENTIAL_1D)
    chunk = _make_chunk(content="opposite", created_at=datetime.now(UTC) - timedelta(days=10))
    service, _ns_repo, _chunk_repo, _embedding_repo, _embedder = _make_service(
        namespace=namespace, search_rows=[(chunk, -0.4)]
    )

    results = await service.search(namespace_name=namespace.name, query="q", top_k=5)

    assert results[0].similarity == 0.0
    assert results[0].score == 0.0


# ─── create / list namespaces — decay policy (Story 3.4 T5) ───────


@pytest.mark.asyncio
async def test_create_namespace_defaults_to_no_decay() -> None:
    """No per-type default, on purpose: enabling decay by default would

    silently reorder every namespace created by Stories 3.1-3.3.
    """
    namespace_repo = MagicMock()
    session = MagicMock()
    _configure_with_tenant(namespace_repo, session)
    created = _make_namespace(name="ns-new")
    namespace_repo.create_in_session = AsyncMock(return_value=created)

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )
    with (
        patch.object(service_module, "publish", AsyncMock(return_value=uuid4())),
        patch.object(service_module, "notify_best_effort", AsyncMock()),
    ):
        view = await service.create_namespace(name="ns-new", ns_type="metier")

    assert namespace_repo.create_in_session.await_args.kwargs["decay_policy"] == {}
    assert view.decay_policy == {}


@pytest.mark.asyncio
async def test_create_namespace_persists_an_explicit_decay_override() -> None:
    namespace_repo = MagicMock()
    session = MagicMock()
    _configure_with_tenant(namespace_repo, session)
    created = _make_namespace(name="ns-decay")
    namespace_repo.create_in_session = AsyncMock(return_value=created)

    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )
    override = DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=86_400)
    with (
        patch.object(service_module, "publish", AsyncMock(return_value=uuid4())),
        patch.object(service_module, "notify_best_effort", AsyncMock()),
    ):
        view = await service.create_namespace(
            name="ns-decay", ns_type="metier", decay_policy_override=override
        )

    expected = {"function": "linear", "horizon_seconds": 86_400}
    assert namespace_repo.create_in_session.await_args.kwargs["decay_policy"] == expected
    assert view.decay_policy == expected


@pytest.mark.asyncio
async def test_list_namespaces_exposes_decay_policy() -> None:
    ns = _make_namespace(name="a", decay_policy=_EXPONENTIAL_1D)
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=[ns])
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.list_namespaces()

    assert result[0].decay_policy == _EXPONENTIAL_1D
    assert result[0].decay_policy_valid is True


@pytest.mark.asyncio
async def test_list_namespaces_flags_a_malformed_decay_policy() -> None:
    ns = _make_namespace(name="a", decay_policy={"function": "sigmoid"})
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=[ns])
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.list_namespaces()

    assert result[0].decay_policy == {}
    assert result[0].decay_policy_valid is False
    assert result[0].retention_policy_valid is True


@pytest.mark.asyncio
async def test_list_namespaces_keeps_the_two_policy_flags_independent() -> None:
    """T5.2 — a corrupt `retention_policy` must not brand a healthy

    `decay_policy` invalid, nor the reverse. Two columns, two guards.
    """
    ns = _make_namespace(
        name="half-corrupt",
        retention_policy="not-a-dict",  # type: ignore[arg-type]
        decay_policy=_EXPONENTIAL_1D,
    )
    namespace_repo = AsyncMock()
    namespace_repo.list_all = AsyncMock(return_value=[ns])
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.count_by_namespace_ids = AsyncMock(return_value={})

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.list_namespaces()

    assert result[0].retention_policy_valid is False
    assert result[0].decay_policy_valid is True
    assert result[0].decay_policy == _EXPONENTIAL_1D


# ─── update_namespace_decay_policy — Story 3.4, code review BS1 ───


def _decay_update_service() -> tuple[MemoryManagerService, MagicMock]:
    namespace_repo = MagicMock()
    _configure_with_tenant(namespace_repo, MagicMock())
    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )
    return service, namespace_repo


@pytest.mark.asyncio
async def test_update_namespace_decay_policy_persists_the_override() -> None:
    """The whole point of the endpoint: a namespace created before Story 3.4

    (so with `decay_policy == {}`) can be switched on after the fact.
    """
    service, namespace_repo = _decay_update_service()
    existing = _make_namespace(name="ns-legacy", decay_policy={})
    namespace_repo.set_decay_policy_in_session = AsyncMock(return_value=existing)

    with (
        patch.object(service_module, "publish", AsyncMock(return_value=uuid4())),
        patch.object(service_module, "notify_best_effort", AsyncMock()),
    ):
        view = await service.update_namespace_decay_policy(
            name="ns-legacy",
            decay_policy_override=DecayPolicy(
                function=DecayFunction.EXPONENTIAL, half_life_seconds=86_400
            ),
        )

    expected = {"function": "exponential", "half_life_seconds": 86_400}
    assert namespace_repo.set_decay_policy_in_session.await_args.kwargs["decay_policy"] == expected
    assert view.decay_policy == expected


@pytest.mark.asyncio
async def test_update_namespace_decay_policy_clears_with_none() -> None:
    """`None` writes `{}`, which is byte-for-byte the column default: turning

    decay off leaves no trace distinguishing it from never having opted in.
    """
    service, namespace_repo = _decay_update_service()
    namespace_repo.set_decay_policy_in_session = AsyncMock(
        return_value=_make_namespace(name="ns-on", decay_policy={"function": "linear"})
    )

    with (
        patch.object(service_module, "publish", AsyncMock(return_value=uuid4())),
        patch.object(service_module, "notify_best_effort", AsyncMock()),
    ):
        view = await service.update_namespace_decay_policy(name="ns-on", decay_policy_override=None)

    assert namespace_repo.set_decay_policy_in_session.await_args.kwargs["decay_policy"] == {}
    assert view.decay_policy == {}


@pytest.mark.asyncio
async def test_update_namespace_decay_policy_raises_not_found() -> None:
    service, namespace_repo = _decay_update_service()
    namespace_repo.set_decay_policy_in_session = AsyncMock(return_value=None)

    with (
        patch.object(service_module, "publish", AsyncMock(return_value=uuid4())),
        patch.object(service_module, "notify_best_effort", AsyncMock()),
        pytest.raises(NotFoundError),
    ):
        await service.update_namespace_decay_policy(name="ghost", decay_policy_override=None)


@pytest.mark.asyncio
async def test_update_namespace_decay_policy_publishes_in_the_write_transaction() -> None:
    """Same atomicity posture as `create_namespace`: the publish takes the

    session, so a crash cannot leave a namespace reordering its results with
    nothing in the outbox saying when that started.
    """
    service, namespace_repo = _decay_update_service()
    namespace_repo.set_decay_policy_in_session = AsyncMock(
        return_value=_make_namespace(name="ns-audit")
    )
    publish = AsyncMock(return_value=uuid4())

    with (
        patch.object(service_module, "publish", publish),
        patch.object(service_module, "notify_best_effort", AsyncMock()),
    ):
        await service.update_namespace_decay_policy(
            name="ns-audit",
            decay_policy_override=DecayPolicy(
                function=DecayFunction.STEP, threshold_seconds=60, factor=0.25
            ),
        )

    assert publish.await_args.args[0] == "memory_manager.namespace.decay_policy_updated"
    assert publish.await_args.kwargs["session"] is not None
    event = publish.await_args.args[1]
    # The policy travels WITH the event: an audit trail saying only that
    # "something changed" would leave nothing to reconstruct.
    assert event.decay_policy == {
        "function": "step",
        "factor": 0.25,
        "threshold_seconds": 60,
    }


# ─── purge_chunk — Story 3.6 AC1 ───────────────────────────────────


def _purge_service(
    *,
    chunk: SimpleNamespace | None,
    namespace: SimpleNamespace | None = None,
    mark_archived_rowcount: int = 1,
) -> tuple[MemoryManagerService, AsyncMock, AsyncMock]:
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.get_by_id = AsyncMock(return_value=chunk)
    memory_chunk_repo.mark_archived_in_session = AsyncMock(return_value=mark_archived_rowcount)
    _configure_with_tenant(memory_chunk_repo, MagicMock())

    namespace_repo = AsyncMock()
    namespace_repo.get_by_id = AsyncMock(return_value=namespace)

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )
    return service, memory_chunk_repo, namespace_repo


@pytest.mark.asyncio
async def test_purge_chunk_soft_deletes_and_publishes_manual_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_event_bus(monkeypatch)
    chunk = _make_chunk()
    namespace = _make_namespace(name="ns-purge")
    service, chunk_repo, _ns_repo = _purge_service(chunk=chunk, namespace=namespace)

    await service.purge_chunk(chunk.id)

    chunk_repo.mark_archived_in_session.assert_awaited_once()
    assert chunk_repo.mark_archived_in_session.await_args.args[1] == [chunk.id]
    assert captured["event_type"] == "memory_manager.chunk.archived"
    event = captured["event"]
    assert event.reason == "manual_purge"  # type: ignore[attr-defined]
    assert event.chunk_id == chunk.id  # type: ignore[attr-defined]
    assert event.namespace == "ns-purge"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_purge_chunk_unknown_chunk_raises_not_found() -> None:
    service, chunk_repo, _ns_repo = _purge_service(chunk=None)

    with pytest.raises(NotFoundError):
        await service.purge_chunk(uuid4())

    chunk_repo.mark_archived_in_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_chunk_already_archived_raises_not_found() -> None:
    """AC1 — idempotence stricte: a second purge/DELETE is 404, never 204."""
    chunk = _make_chunk(archived_at=datetime.now(UTC))
    service, chunk_repo, _ns_repo = _purge_service(chunk=chunk)

    with pytest.raises(NotFoundError):
        await service.purge_chunk(chunk.id)

    chunk_repo.mark_archived_in_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_purge_chunk_race_with_concurrent_archival_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent purge/archival between the read and the UPDATE surfaces
    as `updated == 0` — the same idempotence contract, discovered a few
    milliseconds later."""
    _patch_event_bus(monkeypatch)
    chunk = _make_chunk()
    service, _chunk_repo, _ns_repo = _purge_service(chunk=chunk, mark_archived_rowcount=0)

    with pytest.raises(NotFoundError):
        await service.purge_chunk(chunk.id)


@pytest.mark.asyncio
async def test_purge_chunk_uses_mark_archived_never_hard_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 — soft-delete via the same primitive Story 3.3's worker uses,
    never `delete_by_id` (reserved for a failed embedding write's
    compensating action)."""
    _patch_event_bus(monkeypatch)
    chunk = _make_chunk()
    service, chunk_repo, _ns_repo = _purge_service(chunk=chunk)

    await service.purge_chunk(chunk.id)

    chunk_repo.delete_by_id.assert_not_awaited()


# ─── list_chunks — Story 3.6 AC5 ───────────────────────────────────


@pytest.mark.asyncio
async def test_list_chunks_maps_repo_rows_to_view() -> None:
    namespace = _make_namespace(name="ns-list")
    chunk = _make_chunk(content="alpha")
    namespace_repo = AsyncMock()
    namespace_repo.require_by_name = AsyncMock(return_value=namespace)
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.list_by_namespace = AsyncMock(return_value=[chunk])

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    result = await service.list_chunks(namespace_name="ns-list")

    assert len(result) == 1
    assert result[0].chunk_id == chunk.id
    assert result[0].namespace == "ns-list"
    assert result[0].content == "alpha"
    assert memory_chunk_repo.list_by_namespace.await_args.kwargs["include_archived"] is False


@pytest.mark.asyncio
async def test_list_chunks_forwards_filters_to_the_repo() -> None:
    namespace = _make_namespace(name="ns-list")
    namespace_repo = AsyncMock()
    namespace_repo.require_by_name = AsyncMock(return_value=namespace)
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.list_by_namespace = AsyncMock(return_value=[])

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    after = datetime(2026, 1, 1, tzinfo=UTC)
    before = datetime(2026, 6, 1, tzinfo=UTC)
    await service.list_chunks(
        namespace_name="ns-list",
        include_archived=True,
        content_contains="invoice",
        created_after=after,
        created_before=before,
        limit=10,
        offset=20,
    )

    call = memory_chunk_repo.list_by_namespace.await_args
    assert call.args[0] == namespace.id
    assert call.kwargs["include_archived"] is True
    assert call.kwargs["content_contains"] == "invoice"
    assert call.kwargs["created_after"] == after
    assert call.kwargs["created_before"] == before
    assert call.kwargs["limit"] == 10
    assert call.kwargs["offset"] == 20


@pytest.mark.asyncio
async def test_list_chunks_unknown_namespace_raises_not_found() -> None:
    namespace_repo = AsyncMock()
    namespace_repo.require_by_name = AsyncMock(
        side_effect=NotFoundError(detail="Namespace 'ghost' not found", context={})
    )
    service = MemoryManagerService(
        memory_chunk_repo=AsyncMock(),
        chunk_embedding_repo=AsyncMock(),
        namespace_repo=namespace_repo,
        embedding_router=AsyncMock(),
    )

    with pytest.raises(NotFoundError):
        await service.list_chunks(namespace_name="ghost")


# ─── T13.5 — write/read backend symmetry (THE trap of this story) ─


@pytest.mark.asyncio
async def test_write_read_symmetry_local_namespace_finds_its_own_chunks() -> None:
    """Story 3.6 T13.5 — the nominal case: a namespace embeds and searches
    with the SAME backend. The fake `search_ann` below actually enforces
    `WHERE model = :model` (mirroring the real SQL predicate) — an
    AsyncMock that ignores its `model` kwarg would pass even a broken
    `search()`, which is exactly what makes this regression test worth
    having."""
    namespace = _make_namespace(name="ns-local", embedding_backend="local")
    written: dict[str, list[float]] = {}

    async def _embed(
        texts: list[str], *, backend: str, timeout_s: float = 30.0
    ) -> tuple[list[list[float]], str, str]:
        assert backend == "local"
        return [[0.42]], "bge-small-en-v1.5", "fastembed"

    async def _upsert(
        *, chunk_id: object, model: str, embedding: list[float], **_kw: object
    ) -> None:
        written[model] = embedding

    async def _search_ann(
        query_vector: list[float], *, model: str, **_kw: object
    ) -> list[tuple[SimpleNamespace, float]]:
        if written.get(model) != query_vector:
            return []
        return [(_make_chunk(content="found"), 1.0)]

    namespace_repo = AsyncMock()
    namespace_repo.require_by_name = AsyncMock(return_value=namespace)
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.create = AsyncMock(return_value=_make_chunk())
    chunk_embedding_repo = AsyncMock()
    chunk_embedding_repo.upsert = AsyncMock(side_effect=_upsert)
    chunk_embedding_repo.search_ann = AsyncMock(side_effect=_search_ann)
    embedding_router = AsyncMock()
    embedding_router.embed = AsyncMock(side_effect=_embed)

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=chunk_embedding_repo,
        namespace_repo=namespace_repo,
        embedding_router=embedding_router,
    )

    await service.create_chunk(namespace_name="ns-local", content="hello", ttl_seconds=None)
    results = await service.search(namespace_name="ns-local", query="hello", top_k=5)

    assert [r.content for r in results] == ["found"]


@pytest.mark.asyncio
async def test_write_read_symmetry_mismatched_backend_returns_nothing_explicitly() -> None:
    """The dangerous half of T13.5: a chunk written under one backend and
    searched as though the namespace were configured for a DIFFERENT one
    (simulated here by swapping `namespace_repo.require_by_name`'s return
    value between the write and the read, mirroring the story's own
    prescription) comes back as an EMPTY list, not an exception —
    indistinguishable from "no relevant match" unless this test documents
    the trap explicitly (Dev Notes § Symétrie write/read)."""
    # What was actually PERSISTED (via `chunk_embedding_repo.upsert`), keyed
    # by model — distinct from `_embed`'s own return value so a query
    # embedding computed under the "wrong" model is never mistaken for a
    # write. Both models happen to produce the numerically identical
    # `[0.42]` vector here on purpose: the only thing that must matter is
    # the model NAME (`search_ann`'s `WHERE model = :model`), not the
    # vector's value.
    written: dict[str, list[float]] = {}

    async def _embed(
        texts: list[str], *, backend: str, timeout_s: float = 30.0
    ) -> tuple[list[list[float]], str, str]:
        model = {"local": "bge-small-en-v1.5", "cloud": "text-embedding-3-small"}[backend]
        return [[0.42]], model, "fake"

    async def _upsert(
        *, chunk_id: object, model: str, embedding: list[float], **_kw: object
    ) -> None:
        written[model] = embedding

    async def _search_ann(
        query_vector: list[float], *, model: str, **_kw: object
    ) -> list[tuple[SimpleNamespace, float]]:
        if written.get(model) != query_vector:
            return []
        return [(_make_chunk(content="found"), 1.0)]

    write_namespace = _make_namespace(name="ns-mixed", embedding_backend="local")
    read_namespace = _make_namespace(name="ns-mixed", embedding_backend="cloud")

    namespace_repo = AsyncMock()
    memory_chunk_repo = AsyncMock()
    memory_chunk_repo.create = AsyncMock(return_value=_make_chunk())
    chunk_embedding_repo = AsyncMock()
    chunk_embedding_repo.upsert = AsyncMock(side_effect=_upsert)
    chunk_embedding_repo.search_ann = AsyncMock(side_effect=_search_ann)
    embedding_router = AsyncMock()
    embedding_router.embed = AsyncMock(side_effect=_embed)

    service = MemoryManagerService(
        memory_chunk_repo=memory_chunk_repo,
        chunk_embedding_repo=chunk_embedding_repo,
        namespace_repo=namespace_repo,
        embedding_router=embedding_router,
    )

    namespace_repo.require_by_name = AsyncMock(return_value=write_namespace)
    await service.create_chunk(namespace_name="ns-mixed", content="hello", ttl_seconds=None)

    namespace_repo.require_by_name = AsyncMock(return_value=read_namespace)
    results = await service.search(namespace_name="ns-mixed", query="hello", top_k=5)

    assert results == []
