"""EmbeddingRouter — Story 3.6 T13.2 (T4).

Pure unit tests: a dict of fake :class:`Embedder` implementations, no real
provider, no DB, no HTTP.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar
from unittest.mock import patch

import pytest
from prometheus_client import REGISTRY

from agentive_backend.shared.llm.embedding_router import EmbeddingRouter


class _FakeEmbedder:
    provider_name: ClassVar[str] = "fake"

    def __init__(self, name: str, dim: int) -> None:
        self.provider_name = name  # type: ignore[misc]
        self._dim = dim
        self.calls: list[dict[str, object]] = []

    async def embed(
        self, texts: Sequence[str], *, model: str, timeout_s: float = 30.0
    ) -> list[list[float]]:
        self.calls.append({"texts": list(texts), "model": model, "timeout_s": timeout_s})
        return [[0.1] * self._dim for _ in texts]


def _metric_value(*, backend: str, model: str) -> float:
    for metric in REGISTRY.collect():
        if metric.name != "agentive_embedding_cost_usd":
            continue
        for sample in metric.samples:
            if (
                sample.name == "agentive_embedding_cost_usd_total"
                and sample.labels.get("backend") == backend
                and sample.labels.get("model") == model
            ):
                return sample.value
    return 0.0


def _router() -> tuple[EmbeddingRouter, _FakeEmbedder, _FakeEmbedder, _FakeEmbedder]:
    cloud = _FakeEmbedder("openai", 1536)
    local = _FakeEmbedder("fastembed", 384)
    voyage = _FakeEmbedder("voyage", 1024)
    router = EmbeddingRouter(
        providers={"cloud": cloud, "local": local, "voyage": voyage},
        model_by_backend={
            "cloud": "text-embedding-3-small",
            "local": "bge-small-en-v1.5",
            "voyage": "voyage-3-lite",
        },
    )
    return router, cloud, local, voyage


def test_resolve_returns_the_configured_backend() -> None:
    router, _cloud, local, _voyage = _router()
    embedder, model = router.resolve("local")
    assert embedder is local
    assert model == "bge-small-en-v1.5"


def test_resolve_degrades_unknown_backend_to_cloud() -> None:
    router, cloud, _local, _voyage = _router()
    with patch("agentive_backend.shared.llm.embedding_router._log") as log:
        embedder, model = router.resolve("quantum")
    assert embedder is cloud
    assert model == "text-embedding-3-small"
    log.warning.assert_called_once_with(
        "embedding_router.unknown_backend_fallback_cloud", backend="quantum"
    )


def test_resolve_degrades_when_optional_backend_not_wired() -> None:
    """T4.2 — e.g. `voyage` configured on a namespace but `VOYAGE_API_KEY`
    absent at boot, so `providers` never got a `"voyage"` key."""
    router = EmbeddingRouter(
        providers={"cloud": _FakeEmbedder("openai", 1536)},
        model_by_backend={"cloud": "text-embedding-3-small"},
    )
    embedder, model = router.resolve("voyage")
    assert model == "text-embedding-3-small"
    assert embedder.provider_name == "openai"


@pytest.mark.asyncio
async def test_embed_delegates_to_the_resolved_provider() -> None:
    router, _cloud, local, _voyage = _router()
    vectors, model, provider_name = await router.embed(["a", "b"], backend="local")
    assert len(vectors) == 2
    assert model == "bge-small-en-v1.5"
    assert provider_name == "fastembed"
    assert local.calls == [{"texts": ["a", "b"], "model": "bge-small-en-v1.5", "timeout_s": 30.0}]


@pytest.mark.asyncio
async def test_embed_increments_cost_for_a_paid_model() -> None:
    router, _cloud, _local, voyage = _router()
    before = _metric_value(backend="voyage", model="voyage-3-lite")
    await router.embed(["some text long enough to have tokens"], backend="voyage")
    after = _metric_value(backend="voyage", model="voyage-3-lite")
    assert after > before
    assert voyage.calls  # sanity: the fake was actually called


@pytest.mark.asyncio
async def test_embed_does_not_increment_cost_for_the_free_local_model() -> None:
    router, _cloud, local, _voyage = _router()
    before = _metric_value(backend="local", model="bge-small-en-v1.5")
    await router.embed(["some text"], backend="local")
    after = _metric_value(backend="local", model="bge-small-en-v1.5")
    assert after == before
    assert local.calls
