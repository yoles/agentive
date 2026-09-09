"""FastEmbedProvider — Story 3.6 T13.1.

Mocks ``fastembed.TextEmbedding`` throughout: never download the real ONNX
model in a unit test (mirrors ``test_openai_adapter.py``'s convention of
mocking the SDK boundary, not the network below it).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from agentive_backend.infra.llm.fastembed_adapter import (
    EMBEDDING_MODEL_NAME,
    FastEmbedProvider,
)


def test_embedding_model_name_is_the_short_column_form() -> None:
    # NOT "BAAI/bge-small-en-v1.5" — that's the HF repo id, distinct from
    # the value already migrated into `chunk_embeddings.model` (Story 3.1).
    assert EMBEDDING_MODEL_NAME == "bge-small-en-v1.5"


def test_constructor_loads_the_model_eagerly() -> None:
    """T2.1 — the model loads at construction, not at first ``embed()`` call."""
    with patch("agentive_backend.infra.llm.fastembed_adapter.TextEmbedding") as text_embedding_cls:
        FastEmbedProvider()

    text_embedding_cls.assert_called_once_with(model_name="BAAI/bge-small-en-v1.5")


def test_constructor_propagates_a_load_failure() -> None:
    """A model that fails to download must fail process boot (T2.1), not
    surprise a caller on the first ``POST /memory/chunks``."""
    with patch("agentive_backend.infra.llm.fastembed_adapter.TextEmbedding") as text_embedding_cls:
        text_embedding_cls.side_effect = RuntimeError("could not fetch model from hub")
        with pytest.raises(RuntimeError, match="could not fetch model"):
            FastEmbedProvider()


@pytest.mark.asyncio
async def test_embed_returns_vectors_in_input_order() -> None:
    fake_model = MagicMock()
    fake_model.embed.return_value = [
        np.array([0.1, 0.2, 0.3]),
        np.array([0.4, 0.5, 0.6]),
    ]
    with patch(
        "agentive_backend.infra.llm.fastembed_adapter.TextEmbedding", return_value=fake_model
    ):
        provider = FastEmbedProvider()
        vectors = await provider.embed(["a", "b"], model=EMBEDDING_MODEL_NAME)

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    fake_model.embed.assert_called_once_with(["a", "b"])


@pytest.mark.asyncio
async def test_embed_ignores_the_model_kwarg_but_accepts_it() -> None:
    """Protocol parity only (T2.2 docstring) — this provider always serves
    the single model loaded at construction."""
    fake_model = MagicMock()
    fake_model.embed.return_value = [np.array([1.0, 0.0])]
    with patch(
        "agentive_backend.infra.llm.fastembed_adapter.TextEmbedding", return_value=fake_model
    ):
        provider = FastEmbedProvider()
        vectors = await provider.embed(["x"], model="some-other-model-name")

    assert vectors == [[1.0, 0.0]]


def test_provider_name_is_fastembed() -> None:
    assert FastEmbedProvider.provider_name == "fastembed"
