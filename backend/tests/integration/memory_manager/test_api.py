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

from typing import Any

import httpx
import pytest
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
) -> Any:
    repo = NamespaceRepo(session_factory=session_factory)
    return await repo.create(name=name, ns_type="metier", retention_policy=retention_policy)


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
    app.state.embedder.embed = AsyncMock(side_effect=RuntimeError("provider down"))

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
