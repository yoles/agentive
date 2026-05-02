"""Unit tests — :class:`MemoryChunkRepo` mock-driven."""

from __future__ import annotations

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
