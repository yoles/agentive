"""``EmbeddingRouter`` — per-namespace backend selection for embeddings
(Story 3.6 T4, AC2/AC3).

Conceptual mirror of :class:`~agentive_backend.shared.llm.router.LLMRouter`
— both pick a provider and record cost — but deliberately NOT a fallback
chain: :class:`~agentive_backend.shared.llm.embedder.Embedder`'s own
docstring anticipated this file since Story 3.1, and its rationale still
holds. ``LLMRouter`` retries a single request across providers when one
fails; the backend an embedding call uses is instead a property of the
*namespace* (``namespaces.embedding_backend``), decided once at creation
and never retried mid-request — retrying a write against a different
backend than the one the namespace is configured for would silently break
the write/read symmetry this story's Dev Notes call out as its most
dangerous pitfall (a namespace's chunks must all be embedded — and
searched — by the same backend, or ``ChunkEmbeddingRepo.search_ann``'s
``WHERE model = ...`` returns nothing, indistinguishable from "no match").

``providers`` is keyed on the raw string values of
:class:`~agentive_backend.features.memory_manager.domain.value_objects.EmbeddingBackend`
(``"local"``/``"cloud"``/``"voyage"``), not the enum itself: this module
lives in ``shared.llm``, which the layered-architecture import-linter
contract (Contract 2) forbids from importing a `features.*` domain module,
and it does not need to — ``namespace.embedding_backend`` is already a
plain ``str`` column by the time it reaches here.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentive_backend.shared.llm.embedder import Embedder, EmbeddingPurpose
from agentive_backend.shared.llm.exceptions import LLMProviderUnavailableError
from agentive_backend.shared.llm.metrics import EMBEDDING_COST_USD_TOTAL, EMBEDDING_MODEL_PRICING
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)


def _estimate_tokens(text: str) -> int:
    """Characters/4 heuristic — same approximation as Story 3.5
    (``features/playground/service.py::_estimate_tokens``); no real
    tokenizer dependency exists in this repo. Documented at both call
    sites rather than shared as a helper across a feature/shared boundary
    (Contract 1/2) that would not otherwise exist for one three-line
    function."""
    return max(1, len(text) // 4)


class EmbeddingRouter:
    """Resolve a namespace's ``embedding_backend`` to an :class:`Embedder`
    + model name, and record the resulting cost.

    Not a :class:`~agentive_backend.shared.llm.router.LLMRouter` — see the
    module docstring for why. There is no retry/fallback chain: a namespace's
    configured backend must be available, otherwise :meth:`resolve` raises a
    503-ready error before any vector can be written under another model.
    """

    def __init__(
        self,
        providers: dict[str, Embedder],
        *,
        model_by_backend: dict[str, str],
        dimensions_by_backend: dict[str, int],
    ) -> None:
        self._providers = providers
        self._model_by_backend = model_by_backend
        self._dimensions_by_backend = dimensions_by_backend

    def resolve(self, backend: str) -> tuple[Embedder, str]:
        """Return ``(embedder, model_name)`` for ``backend``.

        Missing optional backends and corrupt/unknown DB values raise a
        503-ready error. Falling back to cloud could persist a cloud vector
        under a namespace configured for another model, making that row
        invisible when the configured backend becomes available again.
        """
        embedder = self._providers.get(backend)
        model = self._model_by_backend.get(backend)
        dimensions = self._dimensions_by_backend.get(backend)
        if embedder is None or model is None or dimensions is None:
            _log.warning(
                "embedding_router.backend_unavailable",
                backend=backend,
            )
            raise LLMProviderUnavailableError(
                detail=f"Embedding backend {backend!r} is not available in this deployment.",
                context={"backend": backend},
            )
        return embedder, model

    async def embed(
        self,
        texts: Sequence[str],
        *,
        backend: str,
        timeout_s: float = 30.0,
        purpose: EmbeddingPurpose | None = None,
        namespace: str | None = None,
    ) -> tuple[list[list[float]], str, str | None]:
        """Embed ``texts`` with the :class:`Embedder` ``backend`` resolves
        to. Returns ``(vectors, model_name, provider_name)`` so the caller
        (``MemoryManagerService``) can build its event/response without
        calling :meth:`resolve` a second time.

        Increments :data:`~agentive_backend.shared.llm.metrics.EMBEDDING_COST_USD_TOTAL`
        here, once, after a successful call — mirror of exactly where
        ``LLMRouter.complete`` increments ``LLM_COST_USD_TOTAL``
        (``shared/llm/router.py``), so a caller that only ever goes through
        this method can never double-count or under-count a cost sample.
        """
        embedder, model = self.resolve(backend)
        vectors = await embedder.embed(
            texts,
            model=model,
            timeout_s=timeout_s,
            purpose=purpose,
        )

        if len(vectors) != len(texts):
            _log.error(
                "embedding_router.invalid_vector_count",
                backend=backend,
                model=model,
                expected_count=len(texts),
                actual_count=len(vectors),
            )
            raise LLMProviderUnavailableError(
                detail=f"Embedding backend {backend!r} returned an invalid vector count.",
                context={
                    "backend": backend,
                    "model": model,
                    "expected_count": len(texts),
                    "actual_count": len(vectors),
                },
            )

        expected_dimensions = self._dimensions_by_backend[backend]
        invalid_vector = next(
            (vector for vector in vectors if len(vector) != expected_dimensions),
            None,
        )
        if invalid_vector is not None:
            actual_dimensions = len(invalid_vector)
            _log.error(
                "embedding_router.invalid_vector_dimensions",
                backend=backend,
                model=model,
                expected_dimensions=expected_dimensions,
                actual_dimensions=actual_dimensions,
            )
            raise LLMProviderUnavailableError(
                detail=f"Embedding backend {backend!r} returned an invalid vector dimension.",
                context={
                    "backend": backend,
                    "model": model,
                    "expected_dimensions": expected_dimensions,
                    "actual_dimensions": actual_dimensions,
                },
            )

        total_tokens = sum(_estimate_tokens(text) for text in texts)
        price_per_million = EMBEDDING_MODEL_PRICING.get(model)
        estimated_cost_usd: float | None = None
        if price_per_million is not None:
            cost = price_per_million * total_tokens / 1_000_000
            estimated_cost_usd = float(cost)
            EMBEDDING_COST_USD_TOTAL.labels(backend=backend, model=model).inc(estimated_cost_usd)

        _log.info(
            "embedding_router.embedding_cost_recorded",
            namespace=namespace,
            backend=backend,
            model=model,
            purpose=purpose,
            estimated_tokens=total_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )

        provider_name = getattr(embedder, "provider_name", None)
        return vectors, model, provider_name


__all__ = ["EmbeddingRouter"]
