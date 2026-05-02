"""Unit tests — :class:`ChunkEmbeddingRepo` mock-driven."""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentive_backend.shared.repositories import ChunkEmbeddingRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_upsert_emits_insert_on_conflict_do_update() -> None:
    factory, session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    await repo.upsert(chunk_id=uuid4(), model="bge-m3", embedding=[0.1, 0.2, 0.3])
    sql_text = str(session.execute.await_args.args[0]).upper()
    assert "INSERT INTO" in sql_text
    assert "ON CONFLICT" in sql_text


@pytest.mark.asyncio
async def test_get_uses_composite_pk() -> None:
    factory, session = make_session_factory_mock()
    repo = ChunkEmbeddingRepo(session_factory=factory)
    chunk_id = uuid4()
    await repo.get(chunk_id, "bge-m3")
    session.get.assert_awaited_once()
    args = session.get.await_args.args
    assert args[1] == (chunk_id, "bge-m3")
