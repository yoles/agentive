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

from agentive_backend.shared.llm.embedder import EmbeddingPurpose
from agentive_backend.shared.llm.embedding_router import EmbeddingRouter
from agentive_backend.shared.llm.exceptions import LLMProviderUnavailableError


class _FakeEmbedder:
    provider_name: ClassVar[str] = "fake"

    def __init__(self, name: str, dim: int) -> None:
        self.provider_name = name  # type: ignore[misc]
        self._dim = dim
        self.calls: list[dict[str, object]] = []

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        timeout_s: float = 30.0,
        purpose: EmbeddingPurpose | None = None,
    ) -> list[list[float]]:
        self.calls.append(
            {"texts": list(texts), "model": model, "timeout_s": timeout_s, "purpose": purpose}
        )
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
    voyage = _FakeEmbedder("voyage", 512)
    router = EmbeddingRouter(
        providers={"cloud": cloud, "local": local, "voyage": voyage},
        model_by_backend={
            "cloud": "text-embedding-3-small",
            "local": "bge-small-en-v1.5",
            "voyage": "voyage-3-lite",
        },
        dimensions_by_backend={
            "cloud": 1536,
            "local": 384,
            "voyage": 512,
        },
    )
    return router, cloud, local, voyage


def test_resolve_returns_the_configured_backend() -> None:
    router, _cloud, local, _voyage = _router()
    embedder, model = router.resolve("local")
    assert embedder is local
    assert model == "bge-small-en-v1.5"


def test_resolve_raises_for_an_unknown_backend() -> None:
    """Décision John 2026-09-09 (Review Findings) — aucun fallback cloud
    silencieux : un backend qui n'est pas dans `providers` doit lever, pas
    dégrader, pour préserver la symétrie write/read."""
    router, _cloud, _local, _voyage = _router()
    with (
        patch("agentive_backend.shared.llm.embedding_router._log") as log,
        pytest.raises(LLMProviderUnavailableError),
    ):
        router.resolve("quantum")
    log.warning.assert_called_once_with("embedding_router.backend_unavailable", backend="quantum")


def test_resolve_raises_when_optional_backend_not_wired() -> None:
    """T4.2 — e.g. `voyage` configured on a namespace but `VOYAGE_API_KEY`
    absent at boot, so `providers` never got a `"voyage"` key. Décision John
    2026-09-09 : refuser explicitement (503) plutôt que de dégrader vers
    cloud."""
    router = EmbeddingRouter(
        providers={"cloud": _FakeEmbedder("openai", 1536)},
        model_by_backend={"cloud": "text-embedding-3-small"},
        dimensions_by_backend={"cloud": 1536},
    )
    with pytest.raises(LLMProviderUnavailableError):
        router.resolve("voyage")


@pytest.mark.asyncio
async def test_embed_delegates_to_the_resolved_provider() -> None:
    router, _cloud, local, _voyage = _router()
    vectors, model, provider_name = await router.embed(
        ["a", "b"], backend="local", purpose="document"
    )
    assert len(vectors) == 2
    assert model == "bge-small-en-v1.5"
    assert provider_name == "fastembed"
    assert local.calls == [
        {
            "texts": ["a", "b"],
            "model": "bge-small-en-v1.5",
            "timeout_s": 30.0,
            "purpose": "document",
        }
    ]


@pytest.mark.asyncio
async def test_embed_logs_cost_with_namespace_context() -> None:
    """Décision John 2026-09-09 (Review Findings) — le coût de chaque
    embedding doit être journalisé avec son namespace, en plus du compteur
    Prometheus (qui reste sans label namespace pour préserver sa
    cardinalité bornée)."""
    router, _cloud, _local, _voyage = _router()
    with patch("agentive_backend.shared.llm.embedding_router._log") as log:
        await router.embed(["some text"], backend="voyage", purpose="query", namespace="ns-1")
    log.info.assert_called_once_with(
        "embedding_router.embedding_cost_recorded",
        namespace="ns-1",
        backend="voyage",
        model="voyage-3-lite",
        purpose="query",
        estimated_tokens=2,  # len("some text") // 4
        estimated_cost_usd=pytest.approx(4e-8),  # 0.02 USD/1M tokens * 2 tokens
    )


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


@pytest.mark.asyncio
async def test_embed_rejects_a_vector_count_different_from_the_input_count() -> None:
    router, _cloud, local, _voyage = _router()

    with (
        patch.object(local, "embed", return_value=[[0.1] * 384]),
        pytest.raises(LLMProviderUnavailableError) as exc_info,
    ):
        await router.embed(["first", "second"], backend="local")

    assert exc_info.value.context == {
        "backend": "local",
        "model": "bge-small-en-v1.5",
        "expected_count": 2,
        "actual_count": 1,
    }


@pytest.mark.asyncio
async def test_embed_rejects_an_unexpected_vector_dimension() -> None:
    router, _cloud, local, _voyage = _router()
    local._dim = 383

    with pytest.raises(LLMProviderUnavailableError) as exc_info:
        await router.embed(["some text"], backend="local")

    assert exc_info.value.context == {
        "backend": "local",
        "model": "bge-small-en-v1.5",
        "expected_dimensions": 384,
        "actual_dimensions": 383,
    }
