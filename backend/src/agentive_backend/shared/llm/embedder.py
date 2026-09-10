"""``Embedder`` — the narrow port for text-to-vector embedding (Story 3.1 T1.1).

Deliberately separate from :class:`~agentive_backend.shared.llm.interface.Completer`
and NOT routed through :class:`~agentive_backend.shared.llm.router.LLMRouter`.
``LLMRouter`` is built around a multi-provider fallback chain for chat
completion; extending it for embedding now would be dead weight ahead of
Story 3.6 (Embedding Router hybride local/cloud), which replaces this
single-provider wiring with its own cost/quality routing. See Story 3.1
Dev Notes § Embedding for the full rationale.

Same ISP shape as :class:`Completer` (audit §2.4) — a Protocol, not an ABC,
so any adapter satisfies it structurally without inheriting from a base
class.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

EmbeddingPurpose = Literal["document", "query"]


@runtime_checkable
class Embedder(Protocol):
    """Narrow port — text-to-vector embedding only."""

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        timeout_s: float = 30.0,
        purpose: EmbeddingPurpose | None = None,
    ) -> list[list[float]]:
        """Return one embedding vector per input text, same order as ``texts``.

        Caller responsibility for NFR9 (no API keys in logs/traces)
        ----------------------------------------------------------
        Same redaction caveat as :meth:`Completer.complete` — arbitrary
        secrets embedded in ``texts`` are not redacted by this layer.
        """
        ...


__all__ = ["Embedder", "EmbeddingPurpose"]
