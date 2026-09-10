"""End-to-end integration tests — Memory Manager API (Story 3.1 T6.3).

Covers:
- AC1 — POST /memory/chunks happy path (201 + body shape) + 404 unknown
  namespace.
- AC2 : POST /memory/search happy path (200 + ranked results) + 404 unknown
  namespace + 422 top_k out of bounds.

Namespace is created directly via ``NamespaceRepo`` (not through an API —
namespace CRUD is Story 3.2, doesn't exist yet). ``MockEmbedder`` is wired
by default in ``make_e2e_app`` so no OPENAI_API_KEY is needed.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.repositories import NamespaceRepo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app

pytestmark = pytest.mark.integration


async def _create_namespace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    name: str,
    retention_policy: dict[str, Any] | None = None,
    decay_policy: dict[str, Any] | None = None,
    embedding_backend: str = "cloud",
) -> Any:
    repo = NamespaceRepo(session_factory=session_factory)
    return await repo.create(
        name=name,
        ns_type="metier",
        retention_policy=retention_policy,
        decay_policy=decay_policy,
        embedding_backend=embedding_backend,
    )


def _deterministic_vector(text_: str, *, dims: int) -> list[float]:
    """Same hash-derived, unit-norm approach as
    ``agentive_backend.shared.llm.testing.MockEmbedder.vector_for``,
    parameterized by dimension — Story 3.6 T13.6 needs a SECOND
    deterministic embedder (384-dim, ``local``) distinct from
    ``MockEmbedder``'s hardcoded 1536-dim ``cloud`` vectors, without
    downloading the real ``fastembed`` ONNX model in CI."""
    digest = hashlib.sha256(text_.encode("utf-8")).digest()
    raw: list[float] = []
    counter = 0
    while len(raw) < dims:
        block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
        raw.extend(b / 255.0 - 0.5 for b in block)
        counter += 1
    raw = raw[:dims]
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / norm for v in raw]


class _FakeLocalEmbedder:
    """Test-only ``local`` backend conformer — 384-dim, mirrors
    ``bge-small-en-v1.5``'s partial HNSW index dimension without the real
    model (T13.6: "ne pas télécharger le vrai FastEmbed en CI d'intégration
    si le modèle doit être téléchargé")."""

    provider_name: ClassVar[str] = "fastembed"

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        timeout_s: float = 30.0,
        purpose: str | None = None,
    ) -> list[list[float]]:
        return [_deterministic_vector(t, dims=384) for t in texts]


class _FakeVoyageEmbedder:
    """Test-only Voyage backend with the production model's 512 dimensions."""

    provider_name: ClassVar[str] = "voyage"

    async def embed(
        self,
        texts: list[str],
        *,
        model: str,
        timeout_s: float = 30.0,
        purpose: str | None = None,
    ) -> list[list[float]]:
        return [_deterministic_vector(t, dims=512) for t in texts]


@pytest.mark.asyncio
async def test_create_chunk_happy_path_returns_201(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="chunks-happy")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "hello world", "namespace": "chunks-happy", "ttl": 3600},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["namespace"] == "chunks-happy"
    assert body["content"] == "hello world"
    assert body["ttl_seconds"] == 3600
    assert body["expires_at"] is not None
    assert body["embedding_model"] == "text-embedding-3-small"
    assert "chunk_id" in body
    assert "created_at" in body


@pytest.mark.asyncio
async def test_create_chunk_no_ttl_never_expires(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="chunks-no-ttl")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "no ttl here", "namespace": "chunks-no-ttl"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["expires_at"] is None
    assert resp.json()["ttl_seconds"] is None


@pytest.mark.asyncio
async def test_create_chunk_no_ttl_returns_effective_ttl_from_namespace_default(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """BS3 : `ttl_seconds` must reflect the TTL actually applied (namespace
    default), not the raw client override alone, which would read back
    `null` here even though `expires_at` is set."""
    await _create_namespace(
        app_session_factory,
        name="chunks-default-ttl",
        retention_policy={"default_ttl_seconds": 3600},
    )
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "no explicit ttl", "namespace": "chunks-default-ttl"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ttl_seconds"] == 3600
    assert body["expires_at"] is not None


@pytest.mark.asyncio
async def test_create_chunk_unknown_namespace_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "hello", "namespace": "ghost-namespace"},
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_search_happy_path_ranks_exact_match_first(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="search-happy")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for content in ("apples and oranges", "a distant unrelated topic", "more unrelated text"):
            resp = await client.post(
                "/api/v1/memory/chunks",
                headers=_auth_headers(),
                json={"content": content, "namespace": "search-happy"},
            )
            assert resp.status_code == 201, resp.text

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "apples and oranges", "namespace": "search-happy", "top_k": 2},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 2
    # Identical text embeds to an identical vector under the deterministic
    # MockEmbedder → the exact-match chunk ranks first with score ~1.0.
    assert body[0]["content"] == "apples and oranges"
    assert body[0]["score"] > 0.999
    assert all(r["namespace"] == "search-happy" for r in body)


@pytest.mark.asyncio
async def test_search_unknown_namespace_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "hello", "namespace": "ghost-namespace"},
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_search_top_k_out_of_bounds_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="search-bounds")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_too_big = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "hello", "namespace": "search-bounds", "top_k": 51},
        )
        resp_too_small = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "hello", "namespace": "search-bounds", "top_k": 0},
        )
    assert resp_too_big.status_code == 422, resp_too_big.text
    assert resp_too_small.status_code == 422, resp_too_small.text


@pytest.mark.asyncio
async def test_search_no_chunks_returns_empty_list(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="search-empty")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "hello", "namespace": "search-empty"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.parametrize(
    "q",
    [
        pytest.param("   ", id="whitespace-only"),
        pytest.param("a\x00b", id="nul-byte"),
    ],
)
@pytest.mark.asyncio
async def test_search_rejects_unstorable_query_with_422(
    app_session_factory: async_sessionmaker[AsyncSession], q: str
) -> None:
    """P3/P8 — `Query(min_length=1)` does not strip, so a blank `q` used to
    reach the embedder (a billed call) and a NUL byte used to 500.

    No namespace is seeded on purpose: validation runs while parsing the
    request, so a 422 here also proves the guard fires BEFORE the namespace
    lookup (an unguarded `q` would surface as a 404 instead).
    """
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": q, "namespace": "some-namespace"},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_chunk_rejects_ttl_above_cap_with_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P1 — an unbounded `ttl` used to overflow the int32 column at insert
    and surface as a 500."""
    await _create_namespace(app_session_factory, name="ttl-cap-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "x", "namespace": "ttl-cap-ns", "ttl": 3_000_000_000},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_chunk_leaves_no_orphan_row_when_embedder_fails(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """IG1 end-to-end — a provider failure must not leave a chunk row with
    no embedding sitting invisibly in the table."""
    from unittest.mock import AsyncMock

    from agentive_backend.shared.repositories import MemoryChunkRepo

    namespace = await _create_namespace(app_session_factory, name="ig1-e2e-ns")
    app = _make_app(session_factory=app_session_factory)
    # Story 3.6 — `app.state.embedder` is now `app.state.embedding_router`;
    # patching its `embed()` directly (rather than reaching into the
    # `EmbeddingRouter`'s private `_providers["cloud"]`) exercises the same
    # "the embedding call fails" scenario at the boundary the service
    # actually calls.
    app.state.embedding_router.embed = AsyncMock(side_effect=RuntimeError("provider down"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="provider down"):
            await client.post(
                "/api/v1/memory/chunks",
                headers=_auth_headers(),
                json={"content": "hello", "namespace": "ig1-e2e-ns"},
            )

    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    remaining = await chunk_repo.list_by_namespace(namespace.id)
    assert remaining == []


@pytest.mark.asyncio
async def test_create_chunk_records_mock_as_source_in_e2e_fixture(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """IG3 end-to-end — `make_e2e_app` wires `MockEmbedder` by default, so
    every chunk written through this fixture must be traceable as such."""
    from uuid import UUID

    from agentive_backend.infra.db.models import ChunkEmbedding
    from agentive_backend.shared.repositories import ChunkEmbeddingRepo

    await _create_namespace(app_session_factory, name="ig3-e2e-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "hello", "namespace": "ig3-e2e-ns"},
        )
    assert resp.status_code == 201, resp.text
    chunk_id = UUID(resp.json()["chunk_id"])

    repo = ChunkEmbeddingRepo(session_factory=app_session_factory)
    row = await repo.get(chunk_id, "text-embedding-3-small")
    assert row is not None
    assert row.source == "mock"
    assert isinstance(row, ChunkEmbedding)


# ─── Story 3.2 AC1 — POST /memory/namespaces ──────────────────────


@pytest.mark.asyncio
async def test_create_namespace_happy_path_uses_type_default_retention(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "dev-notes", "type": "metier", "department": "Dev"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "dev-notes"
    assert body["department"] == "Dev"
    assert body["retention_policy"] == {
        "default_ttl_seconds": 31_536_000,
        "archive_after_seconds": None,
    }
    assert body["embedding_backend"] == "cloud"


@pytest.mark.asyncio
async def test_create_namespace_commits_created_event_to_outbox(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The INSERT + `NamespaceCreatedEvent` publish share one transaction

    (`create_in_session`) — the symmetric denial path already asserts its
    own event lands in `outbox_events`, this was the missing half for the
    creation path (code review Story 3.2, P10).
    """
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "outbox-atomicity", "type": "metier", "department": "Dev"},
        )
    assert resp.status_code == 201, resp.text
    namespace_id = resp.json()["namespace_id"]

    async with app_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = 'memory_manager.namespace.created' "
                "AND payload->>'namespace_id' = :namespace_id"
            ),
            {"namespace_id": namespace_id},
        )
        row = result.mappings().one_or_none()
    assert row is not None
    assert row["payload"]["name"] == "outbox-atomicity"
    assert row["payload"]["department"] == "Dev"


@pytest.mark.asyncio
async def test_create_namespace_client_type_defaults_to_unlimited(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "client-acme", "type": "client"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["retention_policy"]["default_ttl_seconds"] is None


@pytest.mark.asyncio
async def test_create_namespace_explicit_override_replaces_type_default(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={
                "name": "client-custom-ttl",
                "type": "client",
                "retention_policy": {"default_ttl_seconds": 3600},
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["retention_policy"] == {
        "default_ttl_seconds": 3600,
        "archive_after_seconds": None,
    }


@pytest.mark.asyncio
async def test_create_namespace_empty_retention_policy_object_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`{}` used to silently void the type default (code review Story 3.2, BS1)."""
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "client-empty-override", "type": "client", "retention_policy": {}},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_namespace_duplicate_name_returns_409(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="dup-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "dup-ns", "type": "client"},
        )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_create_namespace_invalid_type_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "bad-type-ns", "type": "banana"},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_namespace_ignores_acting_department_header(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Global administration action — not subject to department scoping

    (see the router's own docstring). A header naming a *different*
    department than the body must not affect the outcome (code review
    Story 3.2, P10 — this was asserted nowhere).
    """
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers={**_auth_headers(), "X-Acting-Department": "Design-UX"},
            json={"name": "header-ignored-ns", "type": "metier", "department": "Dev"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["department"] == "Dev"


@pytest.mark.asyncio
async def test_list_namespaces_ignores_acting_department_header(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Admin listing spans every department by design — a header naming

    one specific department must not filter the result (code review Story
    3.2, P10 — this was asserted nowhere).
    """
    await _create_namespace(app_session_factory, name="list-header-ignored")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/memory/namespaces",
            headers={**_auth_headers(), "X-Acting-Department": "Design-UX"},
        )
    assert resp.status_code == 200, resp.text
    assert any(ns["name"] == "list-header-ignored" for ns in resp.json())


# ─── Story 3.2 AC3 — GET /memory/namespaces ───────────────────────


@pytest.mark.asyncio
async def test_list_namespaces_includes_live_chunk_count(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="list-with-chunks")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for content in ("one", "two"):
            resp = await client.post(
                "/api/v1/memory/chunks",
                headers=_auth_headers(),
                json={"content": content, "namespace": "list-with-chunks"},
            )
            assert resp.status_code == 201, resp.text

        resp = await client.get("/api/v1/memory/namespaces", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    by_name = {ns["name"]: ns for ns in resp.json()}
    assert by_name["list-with-chunks"]["chunk_count"] == 2


@pytest.mark.asyncio
async def test_list_namespaces_zero_chunks_when_none_created(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="list-empty")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/memory/namespaces", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    by_name = {ns["name"]: ns for ns in resp.json()}
    assert by_name["list-empty"]["chunk_count"] == 0


@pytest.mark.asyncio
async def test_list_namespaces_flags_a_malformed_retention_policy(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A corrupted `retention_policy` used to render exactly like a

    legitimate unlimited `client` namespace, indistinguishable to an
    admin. `retention_policy_valid: false` is the explicit signal
    (product decision, code review Story 3.2, IG2).
    """
    await _create_namespace(
        app_session_factory,
        name="legacy-corrupt-policy",
        retention_policy={"default_ttl_seconds": "not-an-int"},
    )
    await _create_namespace(app_session_factory, name="legit-unlimited-client")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/memory/namespaces", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    by_name = {ns["name"]: ns for ns in resp.json()}
    assert by_name["legacy-corrupt-policy"]["retention_policy_valid"] is False
    assert by_name["legit-unlimited-client"]["retention_policy_valid"] is True


@pytest.mark.asyncio
async def test_list_namespaces_chunk_count_excludes_expired_chunks(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 states `chunk_count` is coherent with `search_ann`'s own filter,

    which excludes expired chunks in addition to archived ones. Before the
    BS2 fix, an expired-but-not-yet-archived chunk was still counted here,
    over-stating what a search would actually return (code review Story
    3.2, BS2).
    """
    from datetime import UTC, datetime, timedelta

    from agentive_backend.shared.repositories import MemoryChunkRepo

    namespace = await _create_namespace(app_session_factory, name="expired-chunk-ns")
    chunk_repo = MemoryChunkRepo(session_factory=app_session_factory)
    await chunk_repo.create(
        namespace_id=namespace.id,
        content="stale",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/memory/namespaces", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    by_name = {ns["name"]: ns for ns in resp.json()}
    assert by_name["expired-chunk-ns"]["chunk_count"] == 0


# ─── Story 3.2 AC2 — department isolation ─────────────────────────


@pytest.mark.asyncio
async def test_search_cross_department_returns_403_and_records_audit_event(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = NamespaceRepo(session_factory=app_session_factory)
    await repo.create(name="dept-dev-ns", ns_type="metier", department="Dev")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers={**_auth_headers(), "X-Acting-Department": "Design-UX"},
            json={"q": "hello", "namespace": "dept-dev-ns"},
        )
    assert resp.status_code == 403, resp.text

    async with app_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = 'memory_manager.namespace.access_denied' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        )
        row = result.mappings().one_or_none()
    assert row is not None
    assert row["payload"]["namespace"] == "dept-dev-ns"
    assert row["payload"]["namespace_department"] == "Dev"
    assert row["payload"]["acting_department"] == "Design-UX"
    assert row["payload"]["operation"] == "search"


@pytest.mark.asyncio
async def test_create_chunk_cross_department_returns_403(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = NamespaceRepo(session_factory=app_session_factory)
    await repo.create(name="dept-dev-write-ns", ns_type="metier", department="Dev")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers={**_auth_headers(), "X-Acting-Department": "Design-UX"},
            json={"content": "hello", "namespace": "dept-dev-write-ns"},
        )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_search_same_department_succeeds(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    repo = NamespaceRepo(session_factory=app_session_factory)
    await repo.create(name="dept-dev-same", ns_type="metier", department="Dev")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers={**_auth_headers(), "X-Acting-Department": "Dev"},
            json={"q": "hello", "namespace": "dept-dev-same"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_search_same_department_case_and_whitespace_insensitive(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Product decision (code review Story 3.2, IG1): this header is not a

    real security boundary before Growth RBAC, so a case/whitespace
    mismatch must not turn into a spurious 403.
    """
    repo = NamespaceRepo(session_factory=app_session_factory)
    await repo.create(name="dept-dev-fuzzy", ns_type="metier", department="Dev")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers={**_auth_headers(), "X-Acting-Department": " dev "},
            json={"q": "hello", "namespace": "dept-dev-fuzzy"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_search_empty_acting_department_header_is_unrestricted(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`X-Acting-Department:` (present, empty) used to fail the equality

    check and produce a spurious 403 + audit event, same as a real
    cross-department mismatch (code review Story 3.2, P2).
    """
    repo = NamespaceRepo(session_factory=app_session_factory)
    await repo.create(name="dept-dev-empty-header", ns_type="metier", department="Dev")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers={**_auth_headers(), "X-Acting-Department": ""},
            json={"q": "hello", "namespace": "dept-dev-empty-header"},
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_search_oversized_acting_department_header_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No cap on `X-Acting-Department` used to let an unbounded

    client-controlled value reach `outbox_events` and logs, unlike the
    body's `department` field (code review Story 3.2, P3).
    """
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers={**_auth_headers(), "X-Acting-Department": "x" * 101},
            json={"q": "hello", "namespace": "does-not-matter"},
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_search_no_acting_department_header_is_unrestricted(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Backward compatibility with every Story 3.1 call site."""
    repo = NamespaceRepo(session_factory=app_session_factory)
    await repo.create(name="dept-dev-no-header", ns_type="metier", department="Dev")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "hello", "namespace": "dept-dev-no-header"},
        )
    assert resp.status_code == 200, resp.text


# ─── Story 3.3 — MemoryArchivalWorker end-to-end ───────────────────


@pytest.mark.asyncio
async def test_archival_worker_archives_ttl_expired_chunk_end_to_end(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T10.7 — namespace with a short TTL, chunk created, worker run with a
    future ``now`` archives it: DB row + outbox event + search filtering."""
    from datetime import UTC, datetime, timedelta

    from agentive_backend.features.memory_manager.ttl import MemoryArchivalWorker

    await _create_namespace(app_session_factory, name="ttl-archival-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "will expire", "namespace": "ttl-archival-ns", "ttl": 1},
        )
        assert resp.status_code == 201, resp.text
        chunk_id = resp.json()["chunk_id"]

        worker = MemoryArchivalWorker(session_factory=app_session_factory)
        summary = await worker.run_once(now=datetime.now(UTC) + timedelta(days=1))
        assert summary.ttl_expired_count >= 1

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "will expire", "namespace": "ttl-archival-ns"},
        )
        assert resp.status_code == 200, resp.text
        # NOTE: this emptiness is owed to the `expires_at` filter inherited
        # from Story 3.1, not to archival — the chunk was already invisible
        # before the worker ran. The `archived_at` filter is proved on a
        # NON-expired chunk in the T10.8 test below (code review Story 3.3, P11).
        assert resp.json() == []

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "will expire", "namespace": "ttl-archival-ns", "include_archived": True},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["chunk_id"] == chunk_id
    assert body[0]["archived_at"] is not None
    # P10 — `archived_at` alone cannot tell a live hit from an expired one the
    # worker has not reached yet; `expires_at` is what makes that readable.
    assert body[0]["expires_at"] is not None

    async with app_session_factory() as session:
        result = await session.execute(
            text("SELECT archived_at FROM memory_chunks WHERE id = :id"),
            {"id": chunk_id},
        )
        row = result.mappings().one()
        assert row["archived_at"] is not None

        result = await session.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = 'memory_manager.chunk.archived' "
                "AND payload->>'chunk_id' = :chunk_id"
            ),
            {"chunk_id": chunk_id},
        )
        event_row = result.mappings().one_or_none()
    assert event_row is not None
    assert event_row["payload"]["reason"] == "ttl_expired"
    assert event_row["payload"]["namespace"] == "ttl-archival-ns"


@pytest.mark.asyncio
async def test_archival_worker_archives_chunk_by_archive_after_seconds(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T10.8 — a namespace with an explicit `archive_after_seconds` override
    archives a chunk that never expired, tagged `reason="archive_after_seconds"`.

    This is also where the `archived_at` search filter is actually proved: the
    chunk has NO TTL, so its disappearance from `/memory/search` after the run
    can only come from archival. The T10.7 test above cannot make that claim,
    its chunk was already hidden by `expires_at` (code review Story 3.3, P11).
    """
    from datetime import UTC, datetime, timedelta

    from agentive_backend.features.memory_manager.ttl import MemoryArchivalWorker

    repo = NamespaceRepo(session_factory=app_session_factory)
    await repo.create(
        name="age-archival-ns",
        ns_type="metier",
        retention_policy={"archive_after_seconds": 60},
    )
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "will age out", "namespace": "age-archival-ns"},
        )
        assert resp.status_code == 201, resp.text
        chunk_id = resp.json()["chunk_id"]
        assert resp.json()["expires_at"] is None  # no TTL — only the age path applies

        search_body = {"q": "will age out", "namespace": "age-archival-ns"}
        resp = await client.post("/api/v1/memory/search", headers=_auth_headers(), json=search_body)
        assert resp.status_code == 200, resp.text
        live = resp.json()
        assert [hit["chunk_id"] for hit in live] == [chunk_id]
        assert live[0]["archived_at"] is None
        assert live[0]["expires_at"] is None

        worker = MemoryArchivalWorker(session_factory=app_session_factory)
        summary = await worker.run_once(now=datetime.now(UTC) + timedelta(days=1))
        assert summary.archive_after_seconds_count >= 1

        # Same query, same chunk, no TTL involved: gone because it is archived.
        resp = await client.post("/api/v1/memory/search", headers=_auth_headers(), json=search_body)
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={**search_body, "include_archived": True},
        )
        assert resp.status_code == 200, resp.text
        recovered = resp.json()
        assert [hit["chunk_id"] for hit in recovered] == [chunk_id]
        assert recovered[0]["archived_at"] is not None

    async with app_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT payload FROM outbox_events "
                "WHERE event_type = 'memory_manager.chunk.archived' "
                "AND payload->>'chunk_id' = :chunk_id"
            ),
            {"chunk_id": chunk_id},
        )
        row = result.mappings().one_or_none()
    assert row is not None
    assert row["payload"]["reason"] == "archive_after_seconds"
    assert row["payload"]["namespace"] == "age-archival-ns"


# ─── Story 3.4 — scoring avec décroissance temporelle ─────────────

# One-day half-life: a chunk backdated 3 days keeps 1/8 of its similarity,
# which is enough to lose to a same-day chunk of similar (but lower) raw
# similarity, and not enough to lose to a genuinely unrelated one.
_DECAY_1D = {"function": "exponential", "half_life_seconds": 86_400}


async def _backdate_chunk(
    session_factory: async_sessionmaker[AsyncSession], *, chunk_id: str, days: int
) -> None:
    """Age a chunk by rewriting ``created_at`` directly.

    The column is ``server_default=func.now()`` and the API offers no way
    to write it, so a real end-to-end age can only be produced here.
    """
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE memory_chunks SET created_at = now() - make_interval(days => :d) "
                "WHERE id = :id"
            ),
            {"d": days, "id": chunk_id},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_namespaces_decay_policy_column_exists_after_migration(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T2 — the Alembic migration is exercised by the integration harness;

    this asserts the column it adds is actually there, with its `'{}'`
    default, rather than trusting the ORM declaration alone.
    """
    async with app_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'namespaces' AND column_name = 'decay_policy'"
            )
        )
        row = result.one_or_none()
    assert row is not None, "migration 20260906000000 did not add namespaces.decay_policy"
    is_nullable, column_default = row
    assert is_nullable == "NO"
    assert "'{}'" in (column_default or "")


@pytest.mark.asyncio
async def test_search_demotes_an_aged_chunk_below_a_recent_one(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 end to end — the aged chunk is the EXACT phrasing of the query, so

    it ranks first on raw cosine distance; only the decay can push it below
    a fresher, less similar one. The ordering flip between the two requests
    below is the whole point of oversampling the ANN pass.

    `linear` with a 1-day horizon rather than `exponential`: it reaches
    exactly ``0.0`` past its horizon, so this asserts an exact value on data
    that made a real round trip through Postgres.
    """
    await _create_namespace(
        app_session_factory,
        name="decay-ranks",
        decay_policy={"function": "linear", "horizon_seconds": 86_400},
    )
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "quarterly revenue report", "namespace": "decay-ranks"},
        )
        assert resp.status_code == 201, resp.text
        aged_id = resp.json()["chunk_id"]

        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "an unrelated onboarding checklist", "namespace": "decay-ranks"},
        )
        assert resp.status_code == 201, resp.text
        recent_id = resp.json()["chunk_id"]

        await _backdate_chunk(app_session_factory, chunk_id=aged_id, days=10)

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "quarterly revenue report", "namespace": "decay-ranks", "top_k": 2},
        )
        assert resp.status_code == 200, resp.text
        decayed = resp.json()

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={
                "q": "quarterly revenue report",
                "namespace": "decay-ranks",
                "top_k": 2,
                "rerank": False,
            },
        )
        assert resp.status_code == 200, resp.text
        raw = resp.json()

    aged = next(r for r in decayed if r["chunk_id"] == aged_id)
    assert aged["similarity"] > 0.999, "the aged chunk is still the best raw match"
    assert aged["decay_factor"] == 0.0
    assert aged["score"] == 0.0
    assert decayed[0]["chunk_id"] == recent_id
    # `approx`, not `== 1.0`: the "recent" chunk is already a little old by
    # the time the search runs, and `1 - age / 86400s` is not exactly 1. Only
    # the aged chunk's factor above is an exact value (past the horizon,
    # linear is flat at 0.0), which is why that one is asserted exactly.
    #
    # `rel=1e-3`, not `1e-6`: with a 86_400s horizon, `1e-6` only tolerated an
    # age of 86ms, and the elapsed budget here covers two chunk POSTs (each
    # with an embedding call), a backdating UPDATE, a commit and the search
    # itself. Green on a quiet machine, flaky on a loaded CI runner. `1e-3`
    # allows ~86s and still fails on any real decay (code review Story 3.4,
    # P7).
    assert decayed[0]["decay_factor"] == pytest.approx(1.0, rel=1e-3)

    # `rerank: false` gives back the pure-similarity ordering, which is the
    # exact opposite — proof the flip above came from the decay and not from
    # `search_ann`'s own `created_at DESC` tie-break.
    assert raw[0]["chunk_id"] == aged_id
    assert raw[0]["decay_factor"] == 1.0  # exact: `rerank: false` skips decay entirely
    assert raw[0]["score"] == pytest.approx(raw[0]["similarity"])
    assert raw[0]["score"] > 0.999


@pytest.mark.asyncio
async def test_search_score_is_similarity_times_decay_factor(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 end to end — two chunks with IDENTICAL content embed to the same

    vector, so their similarity is identical and the only thing separating
    their scores is their age. A 3-day-old chunk under a 1-day half-life is
    worth 1/8 of a same-day one.
    """
    await _create_namespace(app_session_factory, name="decay-formula", decay_policy=_DECAY_1D)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ids = []
        for _ in range(2):
            resp = await client.post(
                "/api/v1/memory/chunks",
                headers=_auth_headers(),
                json={"content": "identical content", "namespace": "decay-formula"},
            )
            assert resp.status_code == 201, resp.text
            ids.append(resp.json()["chunk_id"])
        aged_id, recent_id = ids

        await _backdate_chunk(app_session_factory, chunk_id=aged_id, days=3)

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "identical content", "namespace": "decay-formula", "top_k": 2},
        )
    assert resp.status_code == 200, resp.text
    by_id = {r["chunk_id"]: r for r in resp.json()}

    recent, aged = by_id[recent_id], by_id[aged_id]
    assert recent["similarity"] == pytest.approx(aged["similarity"])
    # `rel=1e-3` for the same reason as above: against an 86_400s half-life,
    # `1e-6` left only 125ms of wall clock for two POSTs, an UPDATE, a commit
    # and the search (code review Story 3.4, P7).
    assert recent["decay_factor"] == pytest.approx(1.0, rel=1e-3)
    assert recent["score"] == pytest.approx(recent["similarity"])
    # Three half-lives. `rel` rather than an exact 0.125 only because the few
    # ms between the backdating and the search shift the age very slightly.
    assert aged["decay_factor"] == pytest.approx(0.125, rel=1e-3)
    assert aged["score"] == pytest.approx(aged["similarity"] * aged["decay_factor"])
    assert aged["score"] < recent["score"]


@pytest.mark.asyncio
async def test_search_without_decay_policy_keeps_score_equal_to_similarity(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T9.7 end to end — a namespace created before 3.4 (empty

    `decay_policy`) ranks exactly as it did, whatever a chunk's age.
    """
    await _create_namespace(app_session_factory, name="no-decay-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "apples and oranges", "namespace": "no-decay-ns"},
        )
        assert resp.status_code == 201, resp.text
        await _backdate_chunk(app_session_factory, chunk_id=resp.json()["chunk_id"], days=400)

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "apples and oranges", "namespace": "no-decay-ns", "top_k": 5},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body[0]["decay_factor"] == 1.0
    assert body[0]["score"] == pytest.approx(body[0]["similarity"])
    assert body[0]["score"] > 0.999


@pytest.mark.asyncio
async def test_create_namespace_with_a_decay_policy_returns_201_and_persists_it(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={
                "name": "decay-created-ns",
                "type": "metier",
                "decay_policy": {"function": "step", "threshold_seconds": 3600, "factor": 0.2},
            },
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()

        resp = await client.get("/api/v1/memory/namespaces", headers=_auth_headers())
        assert resp.status_code == 200, resp.text
        listed = {ns["name"]: ns for ns in resp.json()}["decay-created-ns"]

    expected = {"function": "step", "threshold_seconds": 3600, "factor": 0.2}
    assert created["decay_policy"] == expected
    assert listed["decay_policy"] == expected
    assert listed["decay_policy_valid"] is True


@pytest.mark.asyncio
async def test_create_namespace_without_decay_policy_defaults_to_none(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "no-decay-created-ns", "type": "metier"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["decay_policy"] == {}


@pytest.mark.asyncio
async def test_create_namespace_incoherent_decay_policy_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A parameter that does not belong to the declared function is a 422 at

    the boundary, never a `DomainValidationError` escaping as a 500.
    """
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={
                "name": "bad-decay-ns",
                "type": "metier",
                "decay_policy": {"function": "exponential", "horizon_seconds": 60},
            },
        )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_list_namespaces_flags_a_malformed_decay_policy(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — a hand-seeded/legacy `decay_policy` must be signalled, and must

    not brand a healthy `retention_policy` invalid alongside it.
    """
    await _create_namespace(
        app_session_factory,
        name="corrupt-decay-ns",
        retention_policy={"default_ttl_seconds": 3600},
        decay_policy={"function": "sigmoid"},
    )
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/memory/namespaces", headers=_auth_headers())
    assert resp.status_code == 200, resp.text
    ns = {n["name"]: n for n in resp.json()}["corrupt-decay-ns"]
    assert ns["decay_policy_valid"] is False
    assert ns["decay_policy"] == {}
    assert ns["retention_policy_valid"] is True


@pytest.mark.asyncio
async def test_search_on_a_malformed_decay_policy_returns_200_not_500(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — a read degrades, it never fails, on a corrupt JSONB."""
    await _create_namespace(
        app_session_factory, name="corrupt-decay-search", decay_policy={"function": "sigmoid"}
    )
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "still searchable", "namespace": "corrupt-decay-search"},
        )
        assert resp.status_code == 201, resp.text

        resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "still searchable", "namespace": "corrupt-decay-search", "top_k": 5},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["decay_factor"] == 1.0


# ─── PATCH /memory/namespaces/{name} — Story 3.4, code review BS1 ──


@pytest.mark.asyncio
async def test_patch_namespace_enables_decay_on_a_pre_existing_namespace(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The finding this endpoint closes: `decay_policy` used to be settable

    at creation only, so every namespace Stories 3.1-3.3 created — all of
    them — could never opt into decay. This walks the real path: a namespace
    born WITHOUT a policy, ranking on similarity alone, then switched on and
    reordering.
    """
    await _create_namespace(app_session_factory, name="decay-late")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "quarterly revenue report", "namespace": "decay-late"},
        )
        assert resp.status_code == 201, resp.text
        aged_id = resp.json()["chunk_id"]

        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "an unrelated onboarding checklist", "namespace": "decay-late"},
        )
        assert resp.status_code == 201, resp.text
        recent_id = resp.json()["chunk_id"]

        await _backdate_chunk(app_session_factory, chunk_id=aged_id, days=10)

        search = {"q": "quarterly revenue report", "namespace": "decay-late", "top_k": 2}

        # Before: no policy, so the exact phrasing wins on raw similarity.
        resp = await client.post("/api/v1/memory/search", headers=_auth_headers(), json=search)
        assert resp.status_code == 200, resp.text
        assert resp.json()[0]["chunk_id"] == aged_id
        assert resp.json()[0]["decay_factor"] == 1.0

        resp = await client.patch(
            "/api/v1/memory/namespaces/decay-late",
            headers=_auth_headers(),
            json={"decay_policy": {"function": "linear", "horizon_seconds": 86_400}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["decay_policy"] == {
            "function": "linear",
            "horizon_seconds": 86_400,
        }

        # After: the aged chunk is past the horizon, factor 0.0, demoted.
        resp = await client.post("/api/v1/memory/search", headers=_auth_headers(), json=search)
        assert resp.status_code == 200, resp.text
        after = resp.json()
        assert after[0]["chunk_id"] == recent_id
        assert next(r for r in after if r["chunk_id"] == aged_id)["decay_factor"] == 0.0


@pytest.mark.asyncio
async def test_patch_namespace_clears_the_policy_with_null(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Round trip: clearing writes `{}`, the column default, so the namespace

    is indistinguishable from one that never opted in.
    """
    await _create_namespace(app_session_factory, name="decay-clear", decay_policy=_DECAY_1D)
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/api/v1/memory/namespaces/decay-clear",
            headers=_auth_headers(),
            json={"decay_policy": None},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["decay_policy"] == {}

        resp = await client.get("/api/v1/memory/namespaces", headers=_auth_headers())
        assert resp.status_code == 200, resp.text
        listed = next(n for n in resp.json() if n["name"] == "decay-clear")
        assert listed["decay_policy"] == {}
        assert listed["decay_policy_valid"] is True


@pytest.mark.asyncio
async def test_patch_unknown_namespace_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/api/v1/memory/namespaces/does-not-exist",
            headers=_auth_headers(),
            json={"decay_policy": None},
        )

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_patch_namespace_rejects_an_incoherent_policy_with_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same 422-not-500 guarantee as the creation path — the parameter belongs

    to another function, so it is rejected rather than silently dropped.
    """
    await _create_namespace(app_session_factory, name="decay-422")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            "/api/v1/memory/namespaces/decay-422",
            headers=_auth_headers(),
            json={"decay_policy": {"function": "linear", "half_life_seconds": 60}},
        )

    assert resp.status_code == 422, resp.text


# ─── Story 3.6 AC1 — DELETE /memory/chunks/{chunk_id} ─────────────


@pytest.mark.asyncio
async def test_purge_chunk_returns_204_and_is_idempotent(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="purge-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "to purge", "namespace": "purge-ns"},
        )
        chunk_id = create_resp.json()["chunk_id"]

        delete_resp = await client.delete(
            f"/api/v1/memory/chunks/{chunk_id}", headers=_auth_headers()
        )
        assert delete_resp.status_code == 204, delete_resp.text
        assert delete_resp.content == b""

        # AC1 — a second DELETE on the same chunk is 404, never 204 again
        # (mirror `delete_template_tool`, Story 2.5 decision #10).
        second_delete_resp = await client.delete(
            f"/api/v1/memory/chunks/{chunk_id}", headers=_auth_headers()
        )
        assert second_delete_resp.status_code == 404, second_delete_resp.text


@pytest.mark.asyncio
async def test_purge_chunk_unknown_id_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/v1/memory/chunks/{uuid4()}", headers=_auth_headers())

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_purge_chunk_malformed_uuid_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete("/api/v1/memory/chunks/not-a-uuid", headers=_auth_headers())

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_purge_chunk_excludes_it_from_search_results(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="purge-search-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "purged content unique phrase", "namespace": "purge-search-ns"},
        )
        chunk_id = create_resp.json()["chunk_id"]
        await client.delete(f"/api/v1/memory/chunks/{chunk_id}", headers=_auth_headers())

        search_resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={
                "q": "purged content unique phrase",
                "namespace": "purge-search-ns",
                "top_k": 5,
            },
        )
    assert search_resp.status_code == 200, search_resp.text
    assert search_resp.json() == []


@pytest.mark.asyncio
async def test_purge_chunk_still_visible_with_include_archived(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Story 3.3 AC2's `include_archived` flag applies to a manual purge
    exactly as it does to automatic archival — same `archived_at` column,
    same filter."""
    await _create_namespace(app_session_factory, name="purge-archived-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "archived but findable", "namespace": "purge-archived-ns"},
        )
        chunk_id = create_resp.json()["chunk_id"]
        await client.delete(f"/api/v1/memory/chunks/{chunk_id}", headers=_auth_headers())

        search_resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={
                "q": "archived but findable",
                "namespace": "purge-archived-ns",
                "top_k": 5,
                "include_archived": True,
            },
        )
    assert search_resp.status_code == 200, search_resp.text
    results = search_resp.json()
    assert len(results) == 1
    assert results[0]["chunk_id"] == chunk_id
    assert results[0]["archived_at"] is not None


# ─── Story 3.6 AC2/AC3 — Embedding Router ──────────────────────────


@pytest.mark.asyncio
async def test_create_namespace_with_local_embedding_backend(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "local-backend-ns", "type": "metier", "embedding_backend": "local"},
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["embedding_backend"] == "local"


@pytest.mark.asyncio
async def test_create_namespace_defaults_embedding_backend_to_cloud(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/namespaces",
            headers=_auth_headers(),
            json={"name": "default-backend-ns", "type": "metier"},
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["embedding_backend"] == "cloud"


@pytest.mark.asyncio
async def test_local_backend_write_then_search_round_trip(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Story 3.6 T13.6 — the write/read symmetry (Dev Notes' central
    pitfall) exercised end to end through Postgres + the real
    `chunk_embeddings_bge_hnsw` partial index, with a deterministic FAKE
    `local` embedder (never the real ONNX model in CI, T13.6)."""
    from agentive_backend.shared.llm.embedding_router import EmbeddingRouter
    from agentive_backend.shared.llm.testing import MockEmbedder

    await _create_namespace(app_session_factory, name="local-e2e-ns", embedding_backend="local")
    app = _make_app(session_factory=app_session_factory)
    app.state.embedding_router = EmbeddingRouter(
        providers={"cloud": MockEmbedder(), "local": _FakeLocalEmbedder()},
        model_by_backend={"cloud": "text-embedding-3-small", "local": "bge-small-en-v1.5"},
        dimensions_by_backend={"cloud": 1536, "local": 384},
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "hello from the local backend", "namespace": "local-e2e-ns"},
        )
        assert create_resp.status_code == 201, create_resp.text
        assert create_resp.json()["embedding_model"] == "bge-small-en-v1.5"

        search_resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "hello from the local backend", "namespace": "local-e2e-ns", "top_k": 5},
        )

    assert search_resp.status_code == 200, search_resp.text
    results = search_resp.json()
    assert len(results) == 1
    assert results[0]["content"] == "hello from the local backend"


@pytest.mark.asyncio
async def test_voyage_backend_512_dimension_write_then_search_round_trip(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exercise the Voyage model width through PostgreSQL and its HNSW index."""
    from agentive_backend.shared.llm.embedding_router import EmbeddingRouter
    from agentive_backend.shared.llm.testing import MockEmbedder

    await _create_namespace(
        app_session_factory,
        name="voyage-e2e-ns",
        embedding_backend="voyage",
    )
    app = _make_app(session_factory=app_session_factory)
    app.state.embedding_router = EmbeddingRouter(
        providers={"cloud": MockEmbedder(), "voyage": _FakeVoyageEmbedder()},
        model_by_backend={
            "cloud": "text-embedding-3-small",
            "voyage": "voyage-3-lite",
        },
        dimensions_by_backend={"cloud": 1536, "voyage": 512},
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "hello from Voyage", "namespace": "voyage-e2e-ns"},
        )
        assert create_resp.status_code == 201, create_resp.text
        assert create_resp.json()["embedding_model"] == "voyage-3-lite"

        search_resp = await client.post(
            "/api/v1/memory/search",
            headers=_auth_headers(),
            json={"q": "hello from Voyage", "namespace": "voyage-e2e-ns", "top_k": 5},
        )

    assert search_resp.status_code == 200, search_resp.text
    assert [item["content"] for item in search_resp.json()] == ["hello from Voyage"]


@pytest.mark.asyncio
async def test_local_backend_unwired_refuses_with_503(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Story 3.6 code review, Décision John 2026-09-09 — a namespace
    configured for `local` when the app only wired `cloud` (e.g. FastEmbed
    failed to load at boot, `app/lifespan.py` T6.1) must refuse explicitly:
    `EmbeddingRouter.resolve` raises instead of silently degrading to
    cloud, to preserve write/read backend symmetry."""
    await _create_namespace(app_session_factory, name="degrade-ns", embedding_backend="local")
    app = _make_app(session_factory=app_session_factory)
    # `make_e2e_app` only wires `"cloud"` by default — no override needed to
    # exercise the "backend not wired" path.

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "should not silently use cloud", "namespace": "degrade-ns"},
        )

    assert resp.status_code == 503, resp.text


# ─── Story 3.6 AC5 — GET /memory/chunks ────────────────────────────


@pytest.mark.asyncio
async def test_list_chunks_returns_created_chunks(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="list-chunks-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "first chunk", "namespace": "list-chunks-ns"},
        )
        await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "second chunk", "namespace": "list-chunks-ns"},
        )

        resp = await client.get(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            params={"namespace": "list-chunks-ns"},
        )

    assert resp.status_code == 200, resp.text
    contents = {item["content"] for item in resp.json()}
    assert contents == {"first chunk", "second chunk"}


@pytest.mark.asyncio
async def test_list_chunks_excludes_purged_by_default(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="list-purged-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "will be purged", "namespace": "list-purged-ns"},
        )
        chunk_id = create_resp.json()["chunk_id"]
        await client.delete(f"/api/v1/memory/chunks/{chunk_id}", headers=_auth_headers())

        default_resp = await client.get(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            params={"namespace": "list-purged-ns"},
        )
        include_archived_resp = await client.get(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            params={"namespace": "list-purged-ns", "include_archived": "true"},
        )

    assert default_resp.json() == []
    assert len(include_archived_resp.json()) == 1
    assert include_archived_resp.json()[0]["chunk_id"] == chunk_id


@pytest.mark.asyncio
async def test_list_chunks_content_contains_filter(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="list-filter-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "INVOICE 100%_READY", "namespace": "list-filter-ns"},
        )
        await client.post(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            json={"content": "invoice 100XXready", "namespace": "list-filter-ns"},
        )

        resp = await client.get(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            params={"namespace": "list-filter-ns", "content_contains": "invoice 100%_ready"},
        )

    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["content"] == "INVOICE 100%_READY"


@pytest.mark.asyncio
async def test_list_chunks_date_window_and_pagination_are_deterministic(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_namespace(app_session_factory, name="list-window-ns")
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        chunk_ids: list[str] = []
        for index in range(4):
            created = await client.post(
                "/api/v1/memory/chunks",
                headers=_auth_headers(),
                json={"content": f"chunk-{index}", "namespace": "list-window-ns"},
            )
            assert created.status_code == 201, created.text
            chunk_ids.append(created.json()["chunk_id"])

        timestamps = (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            datetime(2026, 1, 3, tzinfo=UTC),
        )
        async with app_session_factory() as session:
            for chunk_id, created_at in zip(chunk_ids, timestamps, strict=True):
                await session.execute(
                    text("UPDATE memory_chunks SET created_at = :created_at WHERE id = :id"),
                    {"created_at": created_at, "id": chunk_id},
                )
            await session.commit()

        params = {
            "namespace": "list-window-ns",
            "created_after": timestamps[1].isoformat(),
            "created_before": timestamps[3].isoformat(),
            "limit": 2,
        }
        first_page = await client.get(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            params=params,
        )
        second_page = await client.get(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            params={**params, "offset": 2},
        )

    assert first_page.status_code == 200, first_page.text
    assert second_page.status_code == 200, second_page.text
    tie_ids = sorted(chunk_ids[1:3], reverse=True)
    expected_ids = [chunk_ids[3], *tie_ids]
    assert [item["chunk_id"] for item in first_page.json()] == expected_ids[:2]
    assert [item["chunk_id"] for item in second_page.json()] == expected_ids[2:]


@pytest.mark.asyncio
async def test_list_chunks_unknown_namespace_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/memory/chunks",
            headers=_auth_headers(),
            params={"namespace": "does-not-exist"},
        )

    assert resp.status_code == 404, resp.text
