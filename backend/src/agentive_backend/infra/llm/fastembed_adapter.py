"""FastEmbedProvider — concrete :class:`Embedder` wrapping ``fastembed``
(Story 3.6 T2, AC2 local backend).

``fastembed`` runs a quantized ONNX model fully in-process — no network
call per ``embed()``, no API key. The tradeoff this adapter makes explicit:

* **Eager load at construction, not at first call.** A model that fails to
  download (~33MB from the HuggingFace Hub on first run, then cached) fails
  the process *boot*, mirroring the "refuse to start without a working
  dependency" posture of :func:`agentive_backend.app.lifespan._build_llm_router`
  / ``_build_embedder``. Deferring the load to the first ``POST
  /memory/chunks`` would instead surprise an operator in production with a
  503 on an otherwise-healthy-looking deployment.
* **``embed()`` wraps a synchronous, CPU-bound call in
  :func:`asyncio.to_thread`.** ``TextEmbedding.embed()`` has no I/O — the
  model is already resident in memory — so calling it directly inside a
  coroutine would block the FastAPI event loop for the full inference
  duration (a handful to a few tens of milliseconds per text, additive
  under load). This is the first use of ``asyncio.to_thread`` in this
  codebase for that reason; every other adapter (OpenAI, Anthropic, Voyage)
  wraps a genuine network round-trip instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import ClassVar, Final

from fastembed import TextEmbedding

from agentive_backend.shared.llm.embedder import EmbeddingPurpose

# Canonical name stored in `chunk_embeddings.model` — matches the partial
# HNSW index already migrated by Story 3.1 (`architecture.md:603`). NOT the
# same string as the HuggingFace repo id below: Story 3.1 picked the short
# form for the column before this adapter existed.
EMBEDDING_MODEL_NAME: Final[str] = "bge-small-en-v1.5"
EMBEDDING_DIMENSIONS: Final[int] = 384

# The HuggingFace Hub repo id `fastembed.TextEmbedding` actually loads.
_HF_MODEL_NAME: Final[str] = "BAAI/bge-small-en-v1.5"


class FastEmbedProvider:
    """:class:`Embedder` adapter for the local ``bge-small-en-v1.5`` ONNX model."""

    provider_name: ClassVar[str] = "fastembed"

    def __init__(self) -> None:
        # `lazy_load` defaults to `False` in fastembed — the download +
        # ONNX session init happens right here, synchronously, at
        # construction time (T2.1). No `asyncio.to_thread` needed for this
        # one-time call: it only ever runs during app startup, never inside
        # a request.
        self._model = TextEmbedding(model_name=_HF_MODEL_NAME)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,  # noqa: ARG002 — required by the Embedder protocol contract
        timeout_s: float = 30.0,  # noqa: ARG002 — required by the Embedder protocol contract
        purpose: EmbeddingPurpose | None = None,  # noqa: ARG002 — local model has no retrieval mode
    ) -> list[list[float]]:
        """Embed ``texts`` via the local ONNX model (Story 3.6 T2.2).

        ``model`` is accepted for :class:`~agentive_backend.shared.llm.embedder.Embedder`
        protocol parity but otherwise unused: this provider always serves
        :data:`EMBEDDING_MODEL_NAME` from the single model loaded at
        construction — ``EmbeddingRouter`` is what maps a namespace's
        ``embedding_backend`` to the right provider *and* the right model
        name (T4.2), so by the time this method runs the caller has already
        agreed which model it wants.

        ``timeout_s`` has no network round-trip to bound here (no HTTP
        call — the model is already in process memory), unlike its
        namesake on :class:`~agentive_backend.infra.llm.openai_adapter.OpenAIProvider`
        / :class:`~agentive_backend.infra.llm.voyage_adapter.VoyageProvider`.
        Kept in the signature for protocol conformance and documented here
        rather than silently ignored.
        """

        def _run() -> list[list[float]]:
            return [vector.tolist() for vector in self._model.embed(list(texts))]

        return await asyncio.to_thread(_run)


__all__ = ["EMBEDDING_DIMENSIONS", "EMBEDDING_MODEL_NAME", "FastEmbedProvider"]
