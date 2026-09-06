"""Unit tests — temporal-decay reranker (Story 3.4 T9.2).

Pure Python: no DB, no clock, no app. ``now`` and ``created_at`` are both
injected, so every assertion below is on an exact value, not a tolerance
window around "roughly now".
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from agentive_backend.features.memory_manager.domain.value_objects import (
    DecayFunction,
    DecayPolicy,
)
from agentive_backend.features.memory_manager.reranker import (
    RERANK_FETCH_CAP,
    RERANK_FETCH_MULTIPLIER,
    fetch_k_for,
    rerank,
)

_NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)

_EXPONENTIAL = DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=86_400)


# Distinct per call. The id used to be derived from `age_days`, so two
# chunks of the same age collided on a single UUID — and since `str(chunk.id)`
# is the last sort key, any assertion that reached it was really testing
# `list.sort`'s stability rather than the tie-break rule (code review Story
# 3.4, P12). Tests that care about a specific id still pass `chunk_id`.
_CHUNK_SEQ = itertools.count(1)


def _chunk(*, age_days: float = 0.0, chunk_id: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID(chunk_id) if chunk_id else UUID(int=next(_CHUNK_SEQ)),
        content=f"chunk-{age_days}d",
        created_at=_NOW - timedelta(days=age_days),
    )


# ─── fetch_k_for (T3.2) ───────────────────────────────────────────


def test_fetch_k_equals_top_k_when_rerank_is_disabled() -> None:
    assert fetch_k_for(5, _EXPONENTIAL, rerank_enabled=False) == 5


def test_fetch_k_equals_top_k_when_policy_is_none() -> None:
    """No decay means no reordering, so oversampling would be pure cost on

    the exact path Stories 3.1-3.3 already ship.
    """
    assert fetch_k_for(5, DecayPolicy(), rerank_enabled=True) == 5


def test_fetch_k_oversamples_when_rerank_is_active() -> None:
    assert fetch_k_for(5, _EXPONENTIAL, rerank_enabled=True) == 5 * RERANK_FETCH_MULTIPLIER


def test_fetch_k_is_capped() -> None:
    """`top_k` tops out at 50 (`SearchMemoryRequest`), so the cap is the

    worst case the ANN query can ever be asked for.
    """
    assert fetch_k_for(50, _EXPONENTIAL, rerank_enabled=True) == RERANK_FETCH_CAP


def test_fetch_k_is_never_below_top_k() -> None:
    """Spans `top_k` values PAST the cap, which is the only place the

    invariant can break: `min(top_k * 10, 200)` on its own answers 200 for a
    `top_k` of 300, i.e. fewer candidates than the caller wants results. HTTP
    caps `top_k` at 50, but `MemoryManagerService.search` is a plain internal
    API that Story 3.5 calls directly (code review Story 3.4, P3).
    """
    for top_k in (1, 5, 20, 50, RERANK_FETCH_CAP - 1, RERANK_FETCH_CAP, 300):
        assert fetch_k_for(top_k, _EXPONENTIAL, rerank_enabled=True) >= top_k


def test_fetch_k_beyond_the_cap_falls_back_to_top_k() -> None:
    """The cap bounds oversampling; it never shrinks the request below what

    the caller asked for.
    """
    assert fetch_k_for(300, _EXPONENTIAL, rerank_enabled=True) == 300


# ─── rerank — AC2, the two halves of the ordering property ────────


def test_recent_chunk_wins_at_close_similarity() -> None:
    old = _chunk(age_days=30.0)
    recent = _chunk(age_days=0.0)

    scored = rerank([(old, 0.90), (recent, 0.88)], policy=_EXPONENTIAL, now=_NOW, top_k=2)

    assert [s.chunk for s in scored] == [recent, old]


def test_old_chunk_still_wins_at_significantly_higher_similarity() -> None:
    """The other half of AC2: decay demotes, it does not overrule. This is a

    property of the product, not a rule coded on top of it.
    """
    old = _chunk(age_days=1.0)  # exactly one half-life → factor 0.5
    recent = _chunk(age_days=0.0)

    scored = rerank([(old, 0.95), (recent, 0.40)], policy=_EXPONENTIAL, now=_NOW, top_k=2)

    assert [s.chunk for s in scored] == [old, recent]
    assert scored[0].final_score == pytest.approx(0.475)


def test_none_policy_leaves_score_equal_to_similarity() -> None:
    """T9.7's guarantee at the unit level: no `decay_policy` → no change."""
    a, b = _chunk(age_days=100.0), _chunk(age_days=0.0)

    scored = rerank([(a, 0.9), (b, 0.5)], policy=DecayPolicy(), now=_NOW, top_k=2)

    assert [s.final_score for s in scored] == [0.9, 0.5]
    assert [s.decay_factor for s in scored] == [1.0, 1.0]
    assert [s.similarity for s in scored] == [0.9, 0.5]


def test_negative_similarity_is_clamped_to_zero() -> None:
    """`search_ann` returns `1 - cosine_distance` and cosine distance goes up

    to 2, so a similarity can be NEGATIVE. Without the clamp, multiplying a
    negative by a factor below 1 INCREASES the score and inverts the order:
    `-0.5 x 0.5 = -0.25 > -0.5`. Drop the clamp in `rerank` and this test
    fails on the ordering assertion, not just the value one.
    """
    very_dissimilar = _chunk(age_days=365.0)
    mildly_dissimilar = _chunk(age_days=0.0)

    scored = rerank(
        [(very_dissimilar, -0.5), (mildly_dissimilar, -0.1)],
        policy=_EXPONENTIAL,
        now=_NOW,
        top_k=2,
    )

    assert all(s.similarity == 0.0 for s in scored)
    assert all(s.final_score == 0.0 for s in scored)
    # Both collapse to 0.0, so the tie-break decides — and the recent chunk
    # wins, which is the whole point of not letting a negative through.
    assert scored[0].chunk is mildly_dissimilar


def test_similarity_above_one_is_clamped() -> None:
    chunk = _chunk(age_days=0.0)

    scored = rerank([(chunk, 1.0000000002)], policy=_EXPONENTIAL, now=_NOW, top_k=1)

    assert scored[0].similarity == 1.0


def test_tie_break_is_deterministic_on_created_at_then_id() -> None:
    """Mirror of `search_ann`'s SQL tie-break (`distance, created_at DESC,

    id`, code review Story 3.1 P7): two identical requests must always
    return the same order.
    """
    older = _chunk(age_days=1.0, chunk_id="00000000-0000-0000-0000-0000000000ff")
    newer = _chunk(age_days=0.0, chunk_id="00000000-0000-0000-0000-000000000001")

    scored = rerank([(older, 0.7), (newer, 0.7)], policy=DecayPolicy(), now=_NOW, top_k=2)

    assert [s.chunk for s in scored] == [newer, older]


def test_tie_break_falls_through_to_chunk_id() -> None:
    created_at = _NOW - timedelta(days=1)
    low = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"), content="a", created_at=created_at
    )
    high = SimpleNamespace(
        id=UUID("ffffffff-ffff-ffff-ffff-ffffffffffff"), content="b", created_at=created_at
    )

    scored = rerank([(high, 0.7), (low, 0.7)], policy=DecayPolicy(), now=_NOW, top_k=2)

    assert [s.chunk for s in scored] == [low, high]


def test_truncates_to_top_k_after_scoring() -> None:
    rows = [(_chunk(age_days=float(i)), 0.9 - i / 100) for i in range(30)]

    scored = rerank(rows, policy=_EXPONENTIAL, now=_NOW, top_k=3)

    assert len(scored) == 3


def test_truncation_keeps_the_reranked_winners_not_the_ann_order() -> None:
    """The reason oversampling exists: the chunk the ANN pass ranked last

    must be able to reach the final top_k on recency alone.
    """
    ann_order = [
        (_chunk(age_days=10.0), 0.90),
        (_chunk(age_days=10.0), 0.89),
        (_chunk(age_days=0.0), 0.60),
    ]

    scored = rerank(ann_order, policy=_EXPONENTIAL, now=_NOW, top_k=1)

    assert scored[0].chunk is ann_order[2][0]


def test_empty_rows_returns_empty_list() -> None:
    assert rerank([], policy=_EXPONENTIAL, now=_NOW, top_k=5) == []


def test_future_created_at_never_scores_above_similarity() -> None:
    future = SimpleNamespace(id=UUID(int=1), content="drift", created_at=_NOW + timedelta(days=1))

    scored = rerank([(future, 0.8)], policy=_EXPONENTIAL, now=_NOW, top_k=1)

    assert scored[0].decay_factor == 1.0
    assert scored[0].final_score == pytest.approx(0.8)
