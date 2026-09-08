"""``PushMemoryProvider`` — the narrow port for proactive memory injection
(Story 3.5 T1, FR20).

Why this Protocol exists
-------------------------
``.import-linter`` Contract 1 forbids ``features.playground`` from importing
anything out of ``features.memory_manager`` (strict feature isolation — only
``shared.event_bus`` and ``shared.contracts`` may cross that boundary).
Injecting memory chunks into a Playground run is by construction a
**synchronous** need (the search result must be ready before the LLM call),
so the asynchronous event bus does not fit here. This Protocol is the same
geste already used for :class:`~agentive_backend.shared.llm.embedder.Embedder`
/ :class:`~agentive_backend.shared.llm.interface.Completer` : a narrow port in
``shared/`` that a feature-level adapter implements structurally, wired once
in ``app/lifespan.py`` and read from ``app.state``.

Intended usage: Playground (Story 2.7/3.5) today, Workflow Engine and Chat
consume the exact same port once Epic 4/6 land — see Story 3.5 Dev Notes
§ Écarts avec la lettre de l'épic.

Same ISP shape as :class:`Embedder`/:class:`Completer` — a Protocol, not an
ABC, so :class:`~agentive_backend.features.memory_manager.push_memory.MemoryManagerPushMemoryProvider`
satisfies it structurally without inheriting from a base class.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from agentive_backend.shared.contracts.memory import MemorizedChunkView

# Product constant from the epic (FR20) — canonical value, imported by
# `features/playground/service.py` rather than duplicated as a literal.
DEFAULT_SIMILARITY_THRESHOLD: Final[float] = 0.85


@runtime_checkable
class PushMemoryProvider(Protocol):
    """Narrow port — relevant-chunk lookup for proactive memory injection only."""

    async def relevant_chunks(
        self,
        *,
        namespace: str,
        query: str,
        similarity_threshold: float,
        top_k: int,
    ) -> list[MemorizedChunkView]:
        """Return the chunks of ``namespace`` relevant to ``query``.

        Implementations MUST degrade to an empty list on any failure of the
        underlying memory subsystem (namespace not found, embedder down,
        etc.) — a Push Memory failure must never fail the caller's run.
        """
        ...


__all__ = ["DEFAULT_SIMILARITY_THRESHOLD", "PushMemoryProvider"]
