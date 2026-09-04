"""Test helpers for the LLM layer — TEST-ONLY.

.. DANGER:: NEVER instantiate :class:`MockProvider` from production code.
   It is exported from ``agentive_backend.shared.llm.testing`` (a
   sub-module) so callers must opt in explicitly with
   ``from agentive_backend.shared.llm.testing import MockProvider``.
   The package-level ``__init__`` does NOT re-export it.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from typing import Any, ClassVar

from agentive_backend.shared.llm.types import ChatMessage, Completion


class MockProvider:
    """FIFO replay provider — drives router tests without network calls.

    Each ``complete`` (or ``raw_provider_call``) consumes the next entry
    of ``responses``. ``Completion`` entries are returned; ``Exception``
    entries are raised. Once the queue is exhausted, further calls raise
    :class:`ValueError` so test misconfigurations surface loudly.
    """

    provider_name: ClassVar[str] = "mock"  # overridden per-instance below

    def __init__(
        self,
        name: str,
        responses: Sequence[Completion | Exception],
        *,
        infinite_default: Completion | None = None,
    ) -> None:
        self.provider_name = name  # type: ignore[misc]  # shadow ClassVar with instance attr
        self._responses: list[Completion | Exception] = list(responses)
        self._calls: list[dict[str, Any]] = []
        # When set, ``_consume`` returns this value forever once the FIFO
        # queue is exhausted instead of raising ``ValueError``. Used by
        # the dev-mode synthesized mock in :mod:`agentive_backend.app.lifespan`
        # so the app keeps booting after 1024 dev-time calls (review fix-batch P20).
        self._infinite_default = infinite_default

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Return a snapshot of the kwargs each call received (for assertions)."""
        return list(self._calls)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float = 0.7,
        system: str | None = None,
        stop: Sequence[str] | None = None,
        timeout_s: float = 30.0,
    ) -> Completion:
        self._calls.append(
            {
                "method": "complete",
                "messages": list(messages),
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "stop": list(stop) if stop is not None else None,
                "timeout_s": timeout_s,
            }
        )
        return self._consume()

    async def raw_provider_call(self, **kwargs: Any) -> Any:
        self._calls.append({"method": "raw_provider_call", **kwargs})
        return self._consume()

    def _consume(self) -> Completion:
        if not self._responses:
            if self._infinite_default is not None:
                return self._infinite_default
            raise ValueError(
                f"MockProvider({self.provider_name!r}) exhausted — "
                "more calls were made than queued responses"
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# Dimension of `text-embedding-3-small` — matches the partial HNSW index
# `chunk_embeddings_openai_hnsw` (Story 3.1). Kept here rather than imported
# from the OpenAI adapter so this module has zero production dependencies.
_MOCK_EMBEDDING_DIMS = 1536


class MockEmbedder:
    """Deterministic hash-derived embedder — drives memory_manager tests
    without an OpenAI API call (Story 3.1 T1.4).

    .. DANGER:: NEVER instantiate :class:`MockEmbedder` from production code.
       Same opt-in-only convention as :class:`MockProvider` — imported
       explicitly from ``agentive_backend.shared.llm.testing``, never
       re-exported from the package ``__init__``.

    Each text is hashed (``sha256``) into a seed, then expanded into a
    unit-norm 1536-dim vector. This is deliberately NOT a constant vector:
    tests asserting that a search ranks distinct contents differently need
    the mock to actually vary with the input.
    """

    provider_name: ClassVar[str] = "mock"

    def __init__(self) -> None:
        self._calls: list[dict[str, Any]] = []

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Return a snapshot of the kwargs each call received (for assertions)."""
        return list(self._calls)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        timeout_s: float = 30.0,
    ) -> list[list[float]]:
        self._calls.append({"texts": list(texts), "model": model, "timeout_s": timeout_s})
        return [self.vector_for(text) for text in texts]

    @staticmethod
    def vector_for(text: str) -> list[float]:
        """Derive a deterministic unit-norm vector from ``text`` via a
        seeded PRNG-free expansion of its sha256 digest (no ``random``
        module — reproducible across processes/interpreters)."""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw: list[float] = []
        # Repeat/extend the 32-byte digest into 1536 floats by re-hashing
        # with an incrementing counter each time the digest is exhausted.
        counter = 0
        while len(raw) < _MOCK_EMBEDDING_DIMS:
            block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
            raw.extend(b / 255.0 - 0.5 for b in block)
            counter += 1
        raw = raw[:_MOCK_EMBEDDING_DIMS]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]
