"""HNSW parameter sweep — Story 1.3 gating NFR5.

Sweep matrix officiel (Architecture ligne 571) :
    - m ∈ [8, 16, 32]
    - ef_construction ∈ [64, 128, 256]
    - ef_search ∈ [40, 100, 200]

Total : 9 builds x 3 ef_search = 27 mesures.

Variante CI / fast mode (env BENCH_FAST=1 ou flag --fast) :
    - m ∈ [16, 32]
    - ef_construction ∈ [64, 128]
    - ef_search ∈ [100, 200]
Total : 4 builds x 2 ef_search = 8 mesures (~10 min).

Pré-requis : `python -m scripts.benchmark_m4` doit avoir tourné AU MOINS UNE
FOIS pour seeder les chunks + cacher le ground truth dans `_bench_artifacts/`.
Sinon ce script re-génère les chunks à la volée (mais sans cache ground truth →
~30s additionnels une seule fois).

Output : `_bench_artifacts/hnsw_results.csv` (27 ou 8 lignes selon mode).
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
import time
from datetime import UTC, datetime
from itertools import product

import structlog

from scripts._bench_common import (
    BENCH_ARTIFACTS_DIR,
    GROUND_TRUTH_TOP_K,
    HNSW_INDEX_BGE,
    BenchConfig,
    ann_search_with_rerank,
    bulk_insert_chunks,
    compute_ground_truth,
    connect_owner,
    create_hnsw_index,
    drop_index_if_exists,
    get_or_create_bench_namespace,
    load_ground_truth,
    make_unit_norm_embeddings,
    percentiles_ms,
    pg_relation_size_mb,
    recall_at_k,
    restore_baseline_hnsw_index,
    save_ground_truth,
    truncate_bench_data,
    vacuum_analyze_chunk_embeddings,
)

log = structlog.get_logger("bench.hnsw")

SWEEP_FULL = {
    "m_values": [8, 16, 32],
    "ef_construction_values": [64, 128, 256],
    "ef_search_values": [40, 100, 200],
}

SWEEP_FAST = {
    "m_values": [16, 32],
    "ef_construction_values": [64, 128],
    "ef_search_values": [100, 200],
}

# M3 review : threshold use >=  côté sélection — strict > rejette une mesure pile à 0.9000.
NFR4_THRESHOLD = 0.90
# M6 review : warmup avant la mesure de latence pour ne pas mélanger cold/warm
# pages. 10 queries discardées suffisent à charger les pages HNSW intermédiaires
# en page cache Postgres (l'index HNSW reste petit, ~20MB, donc résident).
WARMUP_QUERIES = 10

CSV_FIELDS = [
    "m",
    "ef_construction",
    "ef_search",
    "build_time_s",
    "index_size_mb",
    "recall_at_5",
    "recall_at_5_strict",
    "recall_at_10",
    "latency_p50_ms",
    "latency_p95_ms",
]


def is_fast_mode() -> bool:
    return os.environ.get("BENCH_FAST", "").lower() in {"1", "true", "yes"} or "--fast" in sys.argv


async def ensure_chunks_and_ground_truth(conn, cfg: BenchConfig) -> tuple[list[list[str]], list]:
    """Seed chunks if missing + (re)compute or load ground truth. Returns (truth, queries_validation)."""
    ns_id = await get_or_create_bench_namespace(conn)

    # Check chunk count for this namespace
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) AS n FROM memory_chunks WHERE namespace_id = %s",
            (ns_id,),
        )
        row = await cur.fetchone()
        existing = int(row["n"]) if row else 0

    if existing != cfg.n_chunks:
        log.info("[bench-hnsw] reseeding_chunks", existing=existing, target=cfg.n_chunks)
        await truncate_bench_data(conn, ns_id)
        embeddings = make_unit_norm_embeddings(cfg.n_chunks, cfg.dim, cfg.seed_corpus)
        await bulk_insert_chunks(
            conn,
            ns_id,
            embeddings,
            cfg.model,
            seed=cfg.seed_corpus,
            created_at_jitter_days=cfg.created_at_jitter_days,
            seed_created_at=cfg.seed_created_at,
        )
    else:
        log.info("[bench-hnsw] chunks_already_seeded", n=existing)

    queries_validation = make_unit_norm_embeddings(
        cfg.n_validation_queries, cfg.dim, cfg.seed_queries_validation
    )

    truth = load_ground_truth(cfg)
    if truth is None:
        log.info("[bench-hnsw] computing_ground_truth")
        await drop_index_if_exists(conn, HNSW_INDEX_BGE)
        truth = await compute_ground_truth(
            conn, queries_validation, cfg.model, top_k=GROUND_TRUTH_TOP_K
        )
        save_ground_truth(cfg, truth)

    return truth, queries_validation


async def measure_one(
    conn,
    cfg: BenchConfig,
    truth: list[list[str]],
    queries_validation,
    queries_latency,
    ef_search: int,
    now: datetime,
) -> tuple[float, float, float, float, float]:
    """Run recall + strict recall + latency pass at given ef_search.

    Returns (recall@5, recall@5_strict, recall@10, p50_ms, p95_ms).

    P7 review : `recall@5_strict` mesure ANN top-5 vs ground top-5 (top_k_ann == k).
    Le `recall@5` "non-strict" mesure top-5_after_rerank(top_k_ann=50) — qui sature
    artificiellement à 1.0 sur des embeddings bien séparés. Le strict recall capture
    la vraie sélectivité de l'index ANN au seuil k.
    """
    recall_5_acc: list[float] = []
    recall_5_strict_acc: list[float] = []
    recall_10_acc: list[float] = []
    for qi, q in enumerate(queries_validation):
        # Mesure principale : top_k_ann=50 + rerank → top-5/top-10 final
        predicted, _dur = await ann_search_with_rerank(
            conn,
            q,
            cfg.model,
            ef_search=ef_search,
            top_k_ann=cfg.top_k_ann,
            top_k_final=10,
            now=now,
        )
        recall_5_acc.append(recall_at_k(predicted, truth[qi], 5))
        recall_10_acc.append(recall_at_k(predicted, truth[qi], 10))

        # P7 review : strict recall — ANN top-5 sans pool pre-rerank
        predicted_strict, _dur_strict = await ann_search_with_rerank(
            conn,
            q,
            cfg.model,
            ef_search=ef_search,
            top_k_ann=cfg.top_k_final,
            top_k_final=cfg.top_k_final,
            now=now,
        )
        recall_5_strict_acc.append(recall_at_k(predicted_strict, truth[qi], cfg.top_k_final))

    # M6 review : warmup discardé avant la mesure latence pour stabiliser le p95.
    for q in queries_latency[:WARMUP_QUERIES]:
        await ann_search_with_rerank(
            conn,
            q,
            cfg.model,
            ef_search=ef_search,
            top_k_ann=cfg.top_k_ann,
            top_k_final=cfg.top_k_final,
            now=now,
        )

    latencies_s: list[float] = []
    for q in queries_latency:
        _predicted, dur = await ann_search_with_rerank(
            conn,
            q,
            cfg.model,
            ef_search=ef_search,
            top_k_ann=cfg.top_k_ann,
            top_k_final=cfg.top_k_final,
            now=now,
        )
        latencies_s.append(dur)
    pct = percentiles_ms(latencies_s, pcts=(50, 95))

    return (
        sum(recall_5_acc) / len(recall_5_acc),
        sum(recall_5_strict_acc) / len(recall_5_strict_acc),
        sum(recall_10_acc) / len(recall_10_acc),
        pct[50],
        pct[95],
    )


async def run_sweep() -> None:
    sweep = SWEEP_FAST if is_fast_mode() else SWEEP_FULL
    mode_tag = "FAST" if is_fast_mode() else "FULL"
    cfg = BenchConfig()
    log.info("[bench-hnsw] start", mode=mode_tag, **sweep)
    t_start = time.perf_counter()

    BENCH_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BENCH_ARTIFACTS_DIR / "hnsw_results.csv"
    rows: list[dict[str, object]] = []

    conn = await connect_owner()
    try:
        truth, queries_validation = await ensure_chunks_and_ground_truth(conn, cfg)
        queries_latency = make_unit_norm_embeddings(
            cfg.n_latency_queries, cfg.dim, cfg.seed_queries_latency
        )
        now = datetime.now(UTC)

        combos = list(product(sweep["m_values"], sweep["ef_construction_values"]))
        log.info("[bench-hnsw] total_builds", n=len(combos))

        # P3 review : open in append mode + flush par ligne ⇒ aucune perte de
        # mesures si le sweep crash mid-run (OOM, OOM Postgres, kill du dev).
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            f.flush()

            for build_idx, (m, ef_c) in enumerate(combos, start=1):
                await drop_index_if_exists(conn, HNSW_INDEX_BGE)
                build_s = await create_hnsw_index(
                    conn, HNSW_INDEX_BGE, cfg.model, cfg.dim, m=m, ef_construction=ef_c
                )
                # M9 review : VACUUM ANALYZE avant pg_relation_size pour mesurer
                # la taille "settled" (T3.3 de la story).
                await vacuum_analyze_chunk_embeddings(conn)
                size_mb = await pg_relation_size_mb(conn, HNSW_INDEX_BGE)
                log.info(
                    "[bench-hnsw] index_built",
                    build=build_idx,
                    m=m,
                    ef_c=ef_c,
                    build_s=round(build_s, 2),
                    size_mb=round(size_mb, 2),
                )

                for ef_s in sweep["ef_search_values"]:
                    r5, r5_strict, r10, p50, p95 = await measure_one(
                        conn, cfg, truth, queries_validation, queries_latency, ef_s, now
                    )
                    row = {
                        "m": m,
                        "ef_construction": ef_c,
                        "ef_search": ef_s,
                        "build_time_s": round(build_s, 2),
                        "index_size_mb": round(size_mb, 2),
                        "recall_at_5": round(r5, 4),
                        "recall_at_5_strict": round(r5_strict, 4),
                        "recall_at_10": round(r10, 4),
                        "latency_p50_ms": round(p50, 2),
                        "latency_p95_ms": round(p95, 2),
                    }
                    rows.append(row)
                    writer.writerow(row)
                    f.flush()
                    log.info("[bench-hnsw] measured", **row)

        log.info("[bench-hnsw] csv_exported", path=str(out_path), rows=len(rows))

        # Final summary
        total_s = time.perf_counter() - t_start
        # M3 review : `>=` plutôt que `>` — match strict NFR4.
        # P7 review : NFR4 utilise recall_at_5_strict (sélectivité ANN sans
        # confounding du rerank). Le recall_at_5 régulier reste informationnel.
        passing = [r for r in rows if float(r["recall_at_5_strict"]) >= NFR4_THRESHOLD]  # type: ignore[arg-type]
        if passing:
            best = min(passing, key=lambda r: float(r["latency_p95_ms"]))  # type: ignore[arg-type]
            log.info(
                "[bench-hnsw] best_passing_config",
                m=best["m"],
                ef_construction=best["ef_construction"],
                ef_search=best["ef_search"],
                recall_at_5=best["recall_at_5"],
                recall_at_5_strict=best["recall_at_5_strict"],
                p95_ms=best["latency_p95_ms"],
            )
        else:
            log.warning("[bench-hnsw] no_config_satisfies_nfr4", n_total=len(rows))

        log.info("[bench-hnsw] total_duration_s", total_s=round(total_s, 2), mode=mode_tag)

    finally:
        # P5 review : restaure l'index HNSW baseline (m=16, ef_c=64) en fin de
        # sweep — sinon le DB dev reste avec les params du dernier triplet et
        # tout démarrage Epic 3 hit un index non-baseline.
        try:
            await restore_baseline_hnsw_index(conn)
            log.info("[bench-hnsw] baseline_index_restored", m=16, ef_construction=64)
        except Exception as exc:
            # Best-effort restore en finally — on log mais on ne propage pas
            # (sinon on masque l'exception originale du sweep).
            log.error("[bench-hnsw] baseline_restore_failed", error=str(exc))
        await conn.close()


def main() -> None:
    asyncio.run(run_sweep())


if __name__ == "__main__":
    main()
