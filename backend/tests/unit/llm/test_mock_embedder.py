"""MockEmbedder — deterministic hash-derived vectors (Story 3.1 T1.4)."""

from __future__ import annotations

import math

import pytest

from agentive_backend.shared.llm.testing import MockEmbedder


@pytest.mark.asyncio
async def test_embed_returns_one_vector_per_text_in_order() -> None:
    embedder = MockEmbedder()
    vectors = await embedder.embed(["a", "b", "c"], model="text-embedding-3-small")
    assert len(vectors) == 3
    assert all(len(v) == 1536 for v in vectors)


@pytest.mark.asyncio
async def test_embed_is_deterministic_for_same_text() -> None:
    embedder = MockEmbedder()
    [v1] = await embedder.embed(["hello"], model="m")
    [v2] = await embedder.embed(["hello"], model="m")
    assert v1 == v2


@pytest.mark.asyncio
async def test_embed_distinguishes_different_texts() -> None:
    """Not a constant vector — distinct content must embed to distinct vectors
    so search tests can assert a meaningful ranking."""
    embedder = MockEmbedder()
    [v1] = await embedder.embed(["alpha"], model="m")
    [v2] = await embedder.embed(["beta"], model="m")
    assert v1 != v2


@pytest.mark.asyncio
async def test_embed_returns_unit_norm_vectors() -> None:
    embedder = MockEmbedder()
    [vector] = await embedder.embed(["hello"], model="m")
    norm = math.sqrt(sum(x * x for x in vector))
    assert norm == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_embed_records_call_kwargs_for_assertions() -> None:
    embedder = MockEmbedder()
    await embedder.embed(["hi"], model="text-embedding-3-small", timeout_s=15.0)
    [call] = embedder.calls
    assert call["texts"] == ["hi"]
    assert call["model"] == "text-embedding-3-small"
    assert call["timeout_s"] == 15.0
