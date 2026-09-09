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

from agentive_backend.shared.llm.embedder import Embedder
from agentive_backend.shared.llm.metrics import EMBEDDING_COST_USD_TOTAL, EMBEDDING_MODEL_PRICING
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

# The one backend every deployment MUST wire (T6.1) — `resolve` degrades to
# it whenever the requested backend is absent from `providers`, so it must
# always be present or that fallback itself raises `KeyError`.
_FALLBACK_BACKEND = "cloud"


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
    module docstring for why. In particular there is no retry/fallback
    *chain*: :meth:`resolve` either returns the requested backend or
    degrades once, silently, to ``"cloud"`` — it never tries a third
    option.
    """

    def __init__(
        self,
        providers: dict[str, Embedder],
        *,
        model_by_backend: dict[str, str],
    ) -> None:
        self._providers = providers
        self._model_by_backend = model_by_backend

    def resolve(self, backend: str) -> tuple[Embedder, str]:
        """Return ``(embedder, model_name)`` for ``backend``.

        Degrades to ``"cloud"`` — never raises — when ``backend`` is not a
        key of ``providers`` (an optional backend like ``"voyage"`` whose
        API key was absent at boot, T6.1; or a corrupted/legacy DB value).
        Same "a read degrades, it never 500s" posture as
        ``RetentionPolicy``/``DecayPolicy`` parsing in
        ``MemoryManagerService`` (Story 3.4 code review, P5).

        ``"cloud"`` is assumed always present in ``providers`` — T6.1 makes
        it the one mandatory backend at boot — so the fallback branch below
        cannot itself raise ``KeyError``.
        """
        embedder = self._providers.get(backend)
        if embedder is None:
            _log.warning(
                "embedding_router.unknown_backend_fallback_cloud",
                backend=backend,
            )
            backend = _FALLBACK_BACKEND
            embedder = self._providers[_FALLBACK_BACKEND]
        return embedder, self._model_by_backend[backend]

    async def embed(
        self,
        texts: Sequence[str],
        *,
        backend: str,
        timeout_s: float = 30.0,
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
        vectors = await embedder.embed(texts, model=model, timeout_s=timeout_s)

        price_per_million = EMBEDDING_MODEL_PRICING.get(model)
        if price_per_million is not None:
            total_tokens = sum(_estimate_tokens(text) for text in texts)
            cost = price_per_million * total_tokens / 1_000_000
            EMBEDDING_COST_USD_TOTAL.labels(backend=backend, model=model).inc(float(cost))

        provider_name = getattr(embedder, "provider_name", None)
        return vectors, model, provider_name


__all__ = ["EmbeddingRouter"]
