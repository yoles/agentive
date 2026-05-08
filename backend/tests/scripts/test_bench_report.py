"""Unit tests pour _bench_report — logique de sélection + parsing CSV.

Couvre les patches review :
- P6 : `select_best_config` tie-break baseline (m=16, ef_c=64) si delta p95 ≤ 1.5ms.
- M3 : recall threshold `>=` plutôt que `>` strict.
- M8 : `filter_well_formed` skip rows malformés sans crash.
"""

from __future__ import annotations

from scripts._bench_report import (
    BASELINE_EF_CONSTRUCTION,
    BASELINE_M,
    NFR4_THRESHOLD,
    NFR5_THRESHOLD_MS,
    TIE_BREAK_P95_MS,
    filter_well_formed,
    select_best_config,
)


def _row(m: int, ef_c: int, ef_s: int, recall: float, p95: float) -> dict[str, str]:
    return {
        "m": str(m),
        "ef_construction": str(ef_c),
        "ef_search": str(ef_s),
        "build_time_s": "10.0",
        "index_size_mb": "20.0",
        "recall_at_5": str(recall),
        "recall_at_10": str(recall),
        "latency_p50_ms": str(p95 - 1.0),
        "latency_p95_ms": str(p95),
    }


# ━━━ select_best_config (P6 tie-break) ━━━


def test_select_best_returns_none_when_no_config_passes() -> None:
    rows = [_row(16, 64, 100, recall=0.5, p95=10.0)]  # recall trop bas
    assert select_best_config(rows) is None


def test_select_best_picks_lowest_p95_when_no_baseline_in_passing() -> None:
    rows = [
        _row(8, 64, 100, recall=1.0, p95=20.0),
        _row(32, 64, 100, recall=1.0, p95=15.0),  # winner
    ]
    best = select_best_config(rows)
    assert best is not None
    assert int(best["m"]) == 32


def test_select_best_tie_break_prefers_baseline_when_close() -> None:
    # Baseline p95=14.5 ; alternative p95=14.0 ; delta=0.5ms < TIE_BREAK_P95_MS
    rows = [
        _row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=1.0, p95=14.5),
        _row(32, 64, 100, recall=1.0, p95=14.0),
    ]
    best = select_best_config(rows)
    assert best is not None
    assert int(best["m"]) == BASELINE_M
    assert int(best["ef_construction"]) == BASELINE_EF_CONSTRUCTION


def test_select_best_no_tie_break_when_delta_significant() -> None:
    # Baseline p95=20.0 ; alternative p95=10.0 ; delta=10ms ≫ TIE_BREAK_P95_MS
    rows = [
        _row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=1.0, p95=20.0),
        _row(32, 64, 100, recall=1.0, p95=10.0),
    ]
    best = select_best_config(rows)
    assert best is not None
    assert int(best["m"]) == 32


def test_select_best_recall_threshold_uses_gte() -> None:
    # M3 review : un row exactement à NFR4_THRESHOLD doit passer (>= et non >).
    rows = [_row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=NFR4_THRESHOLD, p95=14.0)]
    assert select_best_config(rows) is not None


def test_select_best_p95_threshold_uses_strict_lt() -> None:
    # NFR5 cible "< 200ms" — strict pour rejeter une mesure pile au plafond.
    rows = [_row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=1.0, p95=NFR5_THRESHOLD_MS)]
    assert select_best_config(rows) is None


def test_select_best_tie_break_picks_ef_search_100_over_others() -> None:
    # Quand plusieurs entrées baseline existent (différents ef_search), on
    # préfère ef_search=100 (config historique).
    rows = [
        _row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 40, recall=1.0, p95=14.5),
        _row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=1.0, p95=14.4),
        _row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 200, recall=1.0, p95=14.6),
        _row(32, 64, 100, recall=1.0, p95=13.5),  # delta = 1.0 ms ≤ TIE_BREAK
    ]
    best = select_best_config(rows)
    assert best is not None
    assert int(best["m"]) == BASELINE_M
    assert int(best["ef_search"]) == 100


# ━━━ filter_well_formed (M8) ━━━


def test_filter_well_formed_skips_missing_keys() -> None:
    rows = [
        {"recall_at_5": "1.0", "latency_p95_ms": "14.0"},  # missing m / ef_construction / ef_search
        _row(16, 64, 100, recall=1.0, p95=14.0),  # complete
    ]
    out = filter_well_formed(
        rows, ["recall_at_5", "latency_p95_ms", "m", "ef_construction", "ef_search"]
    )
    assert len(out) == 1
    assert out[0]["m"] == "16"


def test_filter_well_formed_skips_unparseable_values() -> None:
    rows = [
        _row(16, 64, 100, recall=1.0, p95=14.0) | {"recall_at_5": "not-a-float"},  # corrompue
        _row(32, 64, 100, recall=1.0, p95=14.0),
    ]
    out = filter_well_formed(
        rows, ["recall_at_5", "latency_p95_ms", "m", "ef_construction", "ef_search"]
    )
    assert len(out) == 1
    assert out[0]["m"] == "32"


def test_filter_well_formed_passes_all_when_clean() -> None:
    rows = [_row(16, 64, 100, 1.0, 14.0), _row(32, 64, 100, 1.0, 13.0)]
    out = filter_well_formed(
        rows, ["recall_at_5", "latency_p95_ms", "m", "ef_construction", "ef_search"]
    )
    assert len(out) == 2


# ━━━ TIE_BREAK_P95_MS sanity ━━━


def test_tie_break_constant_is_documented() -> None:
    # P6 review : changer cette constante doit déclencher une revue (non
    # cosmétique : c'est ce qui dicte la stabilité de la migration Alembic).
    assert TIE_BREAK_P95_MS == 1.5


# ━━━ effective_recall_for_nfr4 (P7) ━━━


def test_select_best_uses_strict_recall_when_present() -> None:
    # P7 review : recall_at_5 régulier = 0.50 (rerank dégrade), mais recall_at_5_strict = 1.0
    # ⇒ NFR4 satisfait (sélectivité ANN OK).
    rows = [
        _row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=0.50, p95=14.0)
        | {"recall_at_5_strict": "1.0"}
    ]
    assert select_best_config(rows) is not None


def test_select_best_falls_back_to_recall_at_5_when_no_strict() -> None:
    # Compatibilité avec les anciennes CSV (sans colonne strict) — fallback recall_at_5.
    rows = [_row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=1.0, p95=14.0)]
    assert select_best_config(rows) is not None


def test_select_best_rejects_when_strict_recall_below_threshold() -> None:
    # Strict recall = 0.5 < NFR4 ⇒ rejet, même si recall_at_5 régulier serait OK.
    rows = [
        _row(BASELINE_M, BASELINE_EF_CONSTRUCTION, 100, recall=1.0, p95=14.0)
        | {"recall_at_5_strict": "0.5"}
    ]
    assert select_best_config(rows) is None
