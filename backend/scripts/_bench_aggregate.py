"""Agrégateur des CSV bench → comparison_hnsw_vs_ivfflat.csv.

P8 review (Story 1.3 AC3 + T4.3) : la story marquait `_bench_aggregate.py`
implémenté mais le fichier n'existait pas — l'agrégation était implicite dans
`_bench_report.py` (rendu Markdown) sans CSV unifiée.

Ce script lit en entrée :
    - `_bench_artifacts/hnsw_results.csv`
    - `_bench_artifacts/ivfflat_results.csv`

Et émet :
    - `_bench_artifacts/comparison_hnsw_vs_ivfflat.csv` avec colonnes communes
      (`indexer, params, recall_at_5, recall_at_5_strict, latency_p95_ms,
      build_time_s, index_size_mb`).

Le script tolère qu'un des deux CSV soit absent (cas où on a tourné un seul
sweep) — il agrège ce qui est disponible. Si **aucun** des deux n'existe, exit 1
(signal CI clair que la pipeline n'a rien produit).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

BENCH_ARTIFACTS_DIR = Path("_bench_artifacts")
HNSW_CSV = BENCH_ARTIFACTS_DIR / "hnsw_results.csv"
IVFFLAT_CSV = BENCH_ARTIFACTS_DIR / "ivfflat_results.csv"
OUTPUT_CSV = BENCH_ARTIFACTS_DIR / "comparison_hnsw_vs_ivfflat.csv"

UNIFIED_FIELDS = [
    "indexer",
    "params",
    "recall_at_5",
    "recall_at_5_strict",
    "latency_p95_ms",
    "build_time_s",
    "index_size_mb",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _hnsw_to_unified(row: dict[str, str]) -> dict[str, str]:
    params = f"m={row['m']},ef_c={row['ef_construction']},ef_s={row['ef_search']}"
    return {
        "indexer": "hnsw",
        "params": params,
        "recall_at_5": row.get("recall_at_5", ""),
        "recall_at_5_strict": row.get("recall_at_5_strict", ""),
        "latency_p95_ms": row.get("latency_p95_ms", ""),
        "build_time_s": row.get("build_time_s", ""),
        "index_size_mb": row.get("index_size_mb", ""),
    }


def _ivfflat_to_unified(row: dict[str, str]) -> dict[str, str]:
    params = f"lists={row['lists']},probes={row['probes']}"
    return {
        "indexer": "ivfflat",
        "params": params,
        "recall_at_5": row.get("recall_at_5", ""),
        "recall_at_5_strict": row.get("recall_at_5_strict", ""),
        "latency_p95_ms": row.get("latency_p95_ms", ""),
        "build_time_s": row.get("build_time_s", ""),
        "index_size_mb": row.get("index_size_mb", ""),
    }


def aggregate() -> int:
    hnsw_rows = _read_csv(HNSW_CSV)
    ivfflat_rows = _read_csv(IVFFLAT_CSV)

    if not hnsw_rows and not ivfflat_rows:
        print(
            "❌ Aucun CSV trouvé. Lancer `make bench-hnsw-fast` et/ou `make bench-ivfflat`.",
            file=sys.stderr,
        )
        return 1

    unified: list[dict[str, str]] = []
    unified.extend(_hnsw_to_unified(r) for r in hnsw_rows)
    unified.extend(_ivfflat_to_unified(r) for r in ivfflat_rows)

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=UNIFIED_FIELDS)
        writer.writeheader()
        writer.writerows(unified)

    print(f"✅ Aggregated {len(hnsw_rows)} HNSW + {len(ivfflat_rows)} ivfflat rows → {OUTPUT_CSV}")
    return 0


def main() -> None:
    sys.exit(aggregate())


if __name__ == "__main__":
    main()
