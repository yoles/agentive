"""Unit tests pour les utilitaires bench M4 (Story 1.3).

Couvre :
- `make_unit_norm_embeddings` (déterminisme, norme L2, dims).
- `format_vector` (format pgvector littéral).
- `recall_at_k` (5 cas : intersection vide, partielle, totale, k=0, k > population).
- `recency_decay` (3 points : age=0, halflife, future-clamped).
- `rerank_topk` (ordre cosine + recency, top_k troncature).
- `percentiles_ms` (sanity sur série connue).
- `cache_path_ground_truth` (P4 : la clé inclut model + n_validation_queries + top_k).
- `_validate_maintenance_work_mem` (M2 : whitelist du format Postgres).

Pas de DB requise — pure logique CPU.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from scripts._bench_common import (
    BenchConfig,
    _validate_maintenance_work_mem,
    cache_path_ground_truth,
    format_vector,
    make_unit_norm_embeddings,
    percentiles_ms,
    recall_at_k,
    recency_decay,
    rerank_topk,
)

# ━━━ make_unit_norm_embeddings ━━━


def test_make_unit_norm_embeddings_shape_and_norm() -> None:
    arr = make_unit_norm_embeddings(n=100, dim=384, seed=42)
    assert arr.shape == (100, 384)
    assert arr.dtype == np.float32
    norms = np.linalg.norm(arr, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_make_unit_norm_embeddings_is_deterministic() -> None:
    a = make_unit_norm_embeddings(n=10, dim=16, seed=42)
    b = make_unit_norm_embeddings(n=10, dim=16, seed=42)
    assert np.array_equal(a, b)


def test_make_unit_norm_embeddings_seeds_differ() -> None:
    a = make_unit_norm_embeddings(n=10, dim=16, seed=1)
    b = make_unit_norm_embeddings(n=10, dim=16, seed=2)
    assert not np.array_equal(a, b)


# ━━━ format_vector ━━━


def test_format_vector_basic() -> None:
    arr = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    out = format_vector(arr)
    assert out.startswith("[")
    assert out.endswith("]")
    assert "0.100000" in out


def test_format_vector_round_trip_preserves_dim() -> None:
    arr = np.array([1.0, -2.5, 3.14159], dtype=np.float32)
    out = format_vector(arr)
    parts = out[1:-1].split(",")
    assert len(parts) == 3


# ━━━ recall_at_k ━━━


def test_recall_at_k_empty_intersection() -> None:
    assert recall_at_k(["a", "b", "c"], ["x", "y", "z"], k=3) == 0.0


def test_recall_at_k_partial_intersection() -> None:
    # 2/5 elements overlap
    pred = ["a", "b", "c", "d", "e"]
    truth = ["a", "b", "x", "y", "z"]
    assert recall_at_k(pred, truth, k=5) == pytest.approx(2 / 5)


def test_recall_at_k_full_intersection() -> None:
    # Order independent (set-based)
    assert recall_at_k(["a", "b", "c"], ["c", "a", "b"], k=3) == 1.0


def test_recall_at_k_zero_k_returns_zero() -> None:
    assert recall_at_k(["a"], ["a"], k=0) == 0.0


def test_recall_at_k_truth_shorter_than_k() -> None:
    # If truth has < k elements, we still divide by k → recall capped.
    assert recall_at_k(["a", "b", "c"], ["a"], k=3) == pytest.approx(1 / 3)


# ━━━ recency_decay ━━━


def test_recency_decay_at_zero_age_is_one() -> None:
    now = datetime.now(UTC)
    assert recency_decay(now, now, halflife_days=30.0) == pytest.approx(1.0)


def test_recency_decay_at_halflife_below_one() -> None:
    now = datetime.now(UTC)
    created = now - timedelta(days=30)
    val = recency_decay(created, now, halflife_days=30.0)
    assert val == pytest.approx(math.exp(-1.0))  # ~0.368


def test_recency_decay_future_timestamp_clamps_age_to_zero() -> None:
    # M1 review : `created_at > now` (clock skew, futur volontaire) clampe l'AGE
    # à 0 ⇒ score retourne `exp(0)=1.0` (boost max), JAMAIS > 1.0. Le nom du test
    # précédent ("clamped_to_zero") référait à l'age, pas au score — confusing.
    now = datetime.now(UTC)
    future = now + timedelta(days=10)
    val = recency_decay(future, now, halflife_days=30.0)
    assert val == pytest.approx(1.0)
    # Un score > 1 briserait l'invariant `final_score ∈ [0, 1]` du rerank.
    assert val <= 1.0


# ━━━ rerank_topk ━━━


def test_rerank_topk_orders_by_combined_score() -> None:
    now = datetime.now(UTC)
    candidates = [
        {"chunk_id": "old_match", "distance": 0.05, "created_at": now - timedelta(days=365)},
        {"chunk_id": "fresh_meh", "distance": 0.50, "created_at": now},
        {"chunk_id": "old_meh", "distance": 0.50, "created_at": now - timedelta(days=365)},
    ]
    result = rerank_topk(candidates, now=now, alpha=0.7, top_k=3)
    # `old_match` has best cosine (0.95) which dominates → first
    assert result[0]["chunk_id"] == "old_match"
    # `fresh_meh` has full recency boost (1.0) > `old_meh` (~0)
    assert result[1]["chunk_id"] == "fresh_meh"
    assert result[2]["chunk_id"] == "old_meh"


def test_rerank_topk_truncates_to_top_k() -> None:
    now = datetime.now(UTC)
    candidates = [
        {"chunk_id": str(i), "distance": float(i) / 100.0, "created_at": now} for i in range(10)
    ]
    result = rerank_topk(candidates, now=now, alpha=0.7, top_k=5)
    assert len(result) == 5
    # Lowest distances win
    assert [c["chunk_id"] for c in result] == ["0", "1", "2", "3", "4"]


# ━━━ percentiles_ms ━━━


def test_percentiles_ms_known_series() -> None:
    # Series 0..99 seconds → durations in seconds, percentiles in ms.
    durations = [i / 1000.0 for i in range(100)]  # 0ms, 1ms, ..., 99ms
    out = percentiles_ms(durations, pcts=(50, 95))
    # numpy.percentile with default 'linear' interpolation
    assert 49.0 <= out[50] <= 50.0
    assert 94.0 <= out[95] <= 95.5


# ━━━ cache_path_ground_truth (P4) ━━━


def test_cache_path_changes_when_model_changes() -> None:
    a = cache_path_ground_truth(BenchConfig(model="bge-small-en-v1.5"))
    b = cache_path_ground_truth(BenchConfig(model="text-embedding-3-small"))
    assert a != b


def test_cache_path_changes_when_n_validation_queries_changes() -> None:
    a = cache_path_ground_truth(BenchConfig(n_validation_queries=50))
    b = cache_path_ground_truth(BenchConfig(n_validation_queries=100))
    assert a != b


def test_cache_path_changes_when_n_chunks_changes() -> None:
    a = cache_path_ground_truth(BenchConfig(n_chunks=10_000))
    b = cache_path_ground_truth(BenchConfig(n_chunks=100_000))
    assert a != b


def test_cache_path_changes_when_dim_changes() -> None:
    a = cache_path_ground_truth(BenchConfig(dim=384))
    b = cache_path_ground_truth(BenchConfig(dim=1536))
    assert a != b


def test_cache_path_stable_for_same_config() -> None:
    a = cache_path_ground_truth(BenchConfig())
    b = cache_path_ground_truth(BenchConfig())
    assert a == b


# ━━━ _validate_maintenance_work_mem (M2) ━━━


@pytest.mark.parametrize("value", ["32MB", "256MB", "1GB", "65536kB"])
def test_validate_maintenance_work_mem_accepts_valid(value: str) -> None:
    assert _validate_maintenance_work_mem(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "32",  # missing unit
        "32MB; DROP TABLE x",  # injection attempt
        "abc",
        "",
        "32 MB",  # space
        "32mB",  # case
    ],
)
def test_validate_maintenance_work_mem_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        _validate_maintenance_work_mem(value)
