"""Unit tests — :class:`MemoryChunkRepo` mock-driven."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import MemoryChunkRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_create_preserves_explicit_empty_metadata_dict() -> None:
    """Regression: ``metadata or {}`` collapsed user-supplied ``{}``."""
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    explicit_empty: dict = {}
    await repo.create(
        namespace_id=uuid4(),
        content="hello",
        metadata=explicit_empty,
    )
    chunk = session.add.call_args.args[0]
    # Should be the SAME object the caller passed (not a fresh dict).
    assert chunk.metadata_ is explicit_empty


@pytest.mark.asyncio
async def test_create_defaults_metadata_to_empty_dict_when_none() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    await repo.create(namespace_id=uuid4(), content="x", metadata=None)
    chunk = session.add.call_args.args[0]
    assert chunk.metadata_ == {}


@pytest.mark.asyncio
async def test_list_by_namespace_emits_select_with_limit() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    await repo.list_by_namespace(uuid4(), limit=42)
    sql_text = str(session.execute.await_args.args[0])
    assert "memory_chunks" in sql_text.lower()
    assert "limit" in sql_text.lower()


@pytest.mark.asyncio
async def test_create_passes_expires_at_through_to_the_row() -> None:
    """Story 3.1 T2.2 — `create()` used to accept `ttl_seconds` but never
    write `expires_at`; this is the gap-closing regression guard."""
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    expires_at = datetime(2030, 1, 1, tzinfo=UTC)
    await repo.create(
        namespace_id=uuid4(), content="hello", ttl_seconds=3600, expires_at=expires_at
    )
    chunk = session.add.call_args.args[0]
    assert chunk.expires_at == expires_at


@pytest.mark.asyncio
async def test_create_defaults_expires_at_to_none() -> None:
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    await repo.create(namespace_id=uuid4(), content="hello")
    chunk = session.add.call_args.args[0]
    assert chunk.expires_at is None


@pytest.mark.asyncio
async def test_delete_by_id_emits_delete_scoped_to_the_id() -> None:
    """IG1 — compensating cleanup for a chunk whose embedding write failed."""
    factory, session = make_session_factory_mock()
    repo = MemoryChunkRepo(session_factory=factory)
    chunk_id = uuid4()

    await repo.delete_by_id(chunk_id)

    sql_text = str(session.execute.await_args.args[0]).lower()
    assert "delete from memory_chunks" in sql_text
    assert "memory_chunks.id = " in sql_text
