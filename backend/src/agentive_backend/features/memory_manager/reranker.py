"""Temporal-decay reranking of ANN search results — Story 3.4 AC1/AC2.

Pure Python, deliberately: no DB access, no ``sqlalchemy`` import (not even
under ``TYPE_CHECKING`` — ``import-linter`` Contract 3 reads static AST
edges), no clock read. ``now`` is a parameter, exactly like in the domain
layer, so the whole module is testable at exact values.

Two responsibilities, both of which the service orchestrates:

1. :func:`fetch_k_for` — **how many candidates to ask Postgres for**. This
   is the non-obvious half of the story. ``ChunkEmbeddingRepo.search_ann``
   applies ``ORDER BY distance LIMIT top_k`` *in SQL*, so a rerank running
   on its output could only ever permute a set already truncated by cosine
   distance alone: a recent chunk ranked 6th by distance would never
   surface at ``top_k=5``, whatever its age. Oversampling the ANN pass and
   truncating *after* scoring is what makes AC2 true in practice rather
   than only in the unit tests.
2. :func:`rerank` — scoring, ordering and truncation of those candidates.

The formula is the epic's (epics.md line 1056), a **product**:
``final_score = clamp(similarity, 0, 1) x decay_factor(age)``. The weighted
sum in ``backend/scripts/_bench_common.py`` is a benchmark placeholder,
lives outside the runtime package, and is frozen by a signed gating ADR
(``docs/decisions/m4-bench-result.md``) — not imported, not modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agentive_backend.features.memory_manager.domain.value_objects import (
    DecayFunction,
    DecayPolicy,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

# How many ANN candidates to pull per requested result when reranking is
# active, and the absolute ceiling on that.
#
# Sizing comes from the Story 1.3 benchmark (`docs/decisions/m4-bench-result.md`),
# which measured the full path — ANN at `top_k_ann = 50`, marshalling, Python
# rerank — at p95 = 14.35 ms on 10k chunks, 14x under NFR5's 200 ms budget.
# `SearchMemoryRequest.top_k` is itself capped at 50, so the multiplier alone
# would allow 500 candidates; the cap bounds the worst case to 200, the same
# order of magnitude as what the benchmark actually measured. No new
# benchmark is required for this story.
RERANK_FETCH_MULTIPLIER = 10
RERANK_FETCH_CAP = 200


class RerankableChunk(Protocol):
    """The only two attributes reranking reads off a chunk.

    Declared structurally rather than imported: ``search_ann`` hands back
    ``infra.db.models.MemoryChunk``, and importing that here would drag
    ``sqlalchemy`` into ``features/*``, which ``import-linter`` Contract 3
    forbids on static AST edges. Typing the rows as ``Any`` was the previous
    way out, but it also switched off every attribute check in this module,
    so a typo on ``created_at`` would only fail at runtime (code review
    Story 3.4, P10).

    Read-only properties, not bare attributes, so the protocol is covariant
    and any richer chunk satisfies it.
    """

    @property
    def id(self) -> UUID: ...

    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class ScoredChunk[ChunkT: RerankableChunk]:
    """One reranked result: the chunk plus every term of its score.

    All three numbers are exposed rather than just ``final_score`` because
    a demoted score is otherwise indistinguishable from a poor similarity
    (AC2/AC3 — "scores documentés" has to be verifiable by the caller).

    Generic in the chunk type so the caller keeps the concrete one it passed
    in: the service reads ``content``, ``archived_at`` and ``expires_at`` off
    ``item.chunk`` to build its response, and those stay type-checked even
    though this module only ever constrains ``id`` and ``created_at``.
    """

    chunk: ChunkT
    similarity: float
    decay_factor: float
    final_score: float


def fetch_k_for(top_k: int, policy: DecayPolicy, *, rerank_enabled: bool) -> int:
    """How many rows to ask ``search_ann`` for, given the namespace's policy.

    Returns ``top_k`` unchanged when reranking cannot change the ordering
    — disabled by the caller, or a ``none`` policy whose factor is a
    constant ``1.0``. That keeps the cost of Stories 3.1-3.3's search path
    exactly where it is today: **oversampling is only paid when it buys
    something**.
    """
    if not rerank_enabled or policy.function is DecayFunction.NONE:
        return top_k
    # `max(top_k, ...)` is the invariant, not decoration: the cap bounds how
    # far we oversample, it must never make us ask Postgres for FEWER rows
    # than the caller wants. Without it, any `top_k` above the cap returns a
    # silently short result set. Unreachable over HTTP today
    # (`SearchMemoryRequest.top_k` is capped at 50), but
    # `MemoryManagerService.search` is a plain internal API and Story 3.5's
    # Push Memory calls it directly (code review Story 3.4, P3).
    return max(top_k, min(top_k * RERANK_FETCH_MULTIPLIER, RERANK_FETCH_CAP))


def rerank[ChunkT: RerankableChunk](
    rows: Sequence[tuple[ChunkT, float]],
    *,
    policy: DecayPolicy,
    now: datetime,
    top_k: int,
) -> list[ScoredChunk[ChunkT]]:
    """Score, order and truncate ``search_ann``'s output — AC1/AC2.

    ``rows`` is exactly what ``ChunkEmbeddingRepo.search_ann`` returns:
    ``(MemoryChunk, 1.0 - cosine_distance)`` pairs.

    The final ordering key mirrors ``search_ann``'s own SQL tie-break
    (``distance, created_at DESC, id`` — code review Story 3.1, P7) so two
    identical requests always return the same order, including when several
    chunks land on the same ``final_score``.
    """
    scored: list[ScoredChunk[ChunkT]] = []
    for chunk, raw_similarity in rows:
        # ⚠️ The clamp is not cosmetic. `search_ann` returns
        # `1.0 - cosine_distance`, and cosine distance ranges over [0, 2],
        # so a similarity can be NEGATIVE for a chunk pointing away from
        # the query vector. Multiplying a negative by a factor below 1
        # *increases* it (-0.5 x 0.5 = -0.25), which inverts the ranking
        # and promotes the least relevant, oldest chunks. Clamping into
        # [0, 1] before the product is what keeps the formula monotonic.
        similarity = min(1.0, max(0.0, raw_similarity))
        factor = policy.decay_factor(chunk.created_at, now)
        scored.append(
            ScoredChunk(
                chunk=chunk,
                similarity=similarity,
                decay_factor=factor,
                final_score=similarity * factor,
            )
        )

    scored.sort(key=lambda s: (-s.final_score, -s.chunk.created_at.timestamp(), str(s.chunk.id)))
    return scored[:top_k]


__all__ = [
    "RERANK_FETCH_CAP",
    "RERANK_FETCH_MULTIPLIER",
    "RerankableChunk",
    "ScoredChunk",
    "fetch_k_for",
    "rerank",
]
