"""Benchmark M4 pgvector — Story 1.3 gating NFR5 (recall@5 >= 90%, p95 < 200ms).

Pipeline complet :
    1. Reset bench namespace + chunks
    2. Generate 10k chunks synthétiques (384 dims, unit-norm) + COPY bulk insert
    3. Drop existing partial HNSW index → compute ground truth via brute-force seq scan
    4. Recreate HNSW index avec params **baseline** (m=16, ef_construction=64)
    5. Mesurer N_LATENCY queries end-to-end (ANN + reranking Python) à ef_search=100
    6. Compute recall@5, recall@5_strict, recall@10, p50/p95 latency
    7. Export CSV + console summary

Pour le sweep complet (27 combos m x ef_construction x ef_search), cf
`scripts/benchmark_hnsw.py`. Ce script-ci est le harness de validation rapide
(baseline) et la **première étape** du flow `make bench` orchestrant ensuite
benchmark_hnsw.py + benchmark_ivfflat.py + _bench_report.py.

Convention : aucun argument CLI — la config est figée dans `BenchConfig` (cf
`_bench_common.py`) pour reproductibilité. Les seeds garantissent un dataset
déterministe entre les runs.
"""

from __future__ import annotations

import asyncio
import csv
import time
from datetime import UTC, datetime

import structlog

from scripts._bench_common import (
    BENCH_ARTIFACTS_DIR,
    GROUND_TRUTH_TOP_K,
    HNSW_INDEX_BGE,
    BenchConfig,
    ann_search_with_rerank,
    bulk_insert_chunks,
    cache_path_ground_truth,
    compute_ground_truth,
    connect_owner,
    create_hnsw_index,
    drop_index_if_exists,
    fetch_versions,
    get_or_create_bench_namespace,
    load_ground_truth,
    make_unit_norm_embeddings,
    percentiles_ms,
    pg_relation_size_mb,
    recall_at_k,
    save_ground_truth,
    truncate_bench_data,
    vacuum_analyze_chunk_embeddings,
)

log = structlog.get_logger("bench.m4")

# M6 review : warmup avant la mesure de latence — discarde 10 queries.
WARMUP_QUERIES = 10


async def run_baseline_bench(cfg: BenchConfig) -> None:
    log.info("[bench] start", **cfg.__dict__)
    t_start = time.perf_counter()

    conn = await connect_owner()
    try:
        versions = await fetch_versions(conn)
        log.info("[bench] versions", **versions)

        # ─── Step 1: namespace + clean previous bench data ───
        ns_id = await get_or_create_bench_namespace(conn)
        log.info("[bench] namespace_ready", namespace_id=str(ns_id))
        await truncate_bench_data(conn, ns_id)

        # ─── Step 2: generate + bulk insert ───
        t0 = time.perf_counter()
        embeddings = make_unit_norm_embeddings(cfg.n_chunks, cfg.dim, cfg.seed_corpus)
        log.info(
            "[bench] embeddings_generated",
            n=cfg.n_chunks,
            dim=cfg.dim,
            gen_s=round(time.perf_counter() - t0, 3),
        )

        t0 = time.perf_counter()
        await bulk_insert_chunks(
            conn,
            ns_id,
            embeddings,
            cfg.model,
            seed=cfg.seed_corpus,
            created_at_jitter_days=cfg.created_at_jitter_days,
            seed_created_at=cfg.seed_created_at,
        )
        log.info(
            "[bench] chunks_inserted",
            n=cfg.n_chunks,
            insert_s=round(time.perf_counter() - t0, 3),
        )

        # ─── Step 3: ground truth (drop index → brute force) ───
        cached = load_ground_truth(cfg)
        if cached is not None:
            log.info("[bench] ground_truth_cache_hit", path=str(cache_path_ground_truth(cfg)))
            truth = cached
        else:
            await drop_index_if_exists(conn, HNSW_INDEX_BGE)
            queries_validation = make_unit_norm_embeddings(
                cfg.n_validation_queries, cfg.dim, cfg.seed_queries_validation
            )
            t0 = time.perf_counter()
            truth = await compute_ground_truth(
                conn, queries_validation, cfg.model, top_k=GROUND_TRUTH_TOP_K
            )
            log.info(
                "[bench] ground_truth_computed",
                n_queries=cfg.n_validation_queries,
                duration_s=round(time.perf_counter() - t0, 3),
            )
            saved = save_ground_truth(cfg, truth)
            log.info("[bench] ground_truth_saved", path=str(saved))

        # ─── Step 4: rebuild HNSW with BASELINE params (m=16, ef_c=64) ───
        await drop_index_if_exists(conn, HNSW_INDEX_BGE)
        build_s = await create_hnsw_index(
            conn, HNSW_INDEX_BGE, cfg.model, cfg.dim, m=16, ef_construction=64
        )
        # M9 review : VACUUM ANALYZE avant pg_relation_size.
        await vacuum_analyze_chunk_embeddings(conn)
        size_mb = await pg_relation_size_mb(conn, HNSW_INDEX_BGE)
        log.info(
            "[bench] hnsw_baseline_built",
            m=16,
            ef_construction=64,
            build_s=round(build_s, 2),
            index_size_mb=round(size_mb, 2),
        )

        # ─── Step 5: measure recall + latency at ef_search=100 ───
        # Reuse validation queries for recall, fresh queries for latency
        queries_validation = make_unit_norm_embeddings(
            cfg.n_validation_queries, cfg.dim, cfg.seed_queries_validation
        )
        queries_latency = make_unit_norm_embeddings(
            cfg.n_latency_queries, cfg.dim, cfg.seed_queries_latency
        )
        now = datetime.now(UTC)

        # Recall pass — measure both regular (top_k_ann=50 + rerank → top-5/10)
        # AND strict (top_k_ann == k=5) per P7 review.
        recall_5_acc: list[float] = []
        recall_5_strict_acc: list[float] = []
        recall_10_acc: list[float] = []
        for qi, q in enumerate(queries_validation):
            predicted, _dur = await ann_search_with_rerank(
                conn,
                q,
                cfg.model,
                ef_search=100,
                top_k_ann=cfg.top_k_ann,
                top_k_final=10,
                now=now,
            )
            recall_5_acc.append(recall_at_k(predicted, truth[qi], 5))
            recall_10_acc.append(recall_at_k(predicted, truth[qi], 10))

            predicted_strict, _ds = await ann_search_with_rerank(
                conn,
                q,
                cfg.model,
                ef_search=100,
                top_k_ann=cfg.top_k_final,
                top_k_final=cfg.top_k_final,
                now=now,
            )
            recall_5_strict_acc.append(recall_at_k(predicted_strict, truth[qi], cfg.top_k_final))

        avg_recall_5 = sum(recall_5_acc) / len(recall_5_acc)
        avg_recall_5_strict = sum(recall_5_strict_acc) / len(recall_5_strict_acc)
        avg_recall_10 = sum(recall_10_acc) / len(recall_10_acc)

        # M6 review : warmup loop (discardé) avant la mesure de latence.
        for q in queries_latency[:WARMUP_QUERIES]:
            await ann_search_with_rerank(
                conn,
                q,
                cfg.model,
                ef_search=100,
                top_k_ann=cfg.top_k_ann,
                top_k_final=cfg.top_k_final,
                now=now,
            )

        # Latency pass (separate queries to avoid measuring cache-warmed cases only)
        latencies_s: list[float] = []
        for q in queries_latency:
            _predicted, dur = await ann_search_with_rerank(
                conn,
                q,
                cfg.model,
                ef_search=100,
                top_k_ann=cfg.top_k_ann,
                top_k_final=cfg.top_k_final,
                now=now,
            )
            latencies_s.append(dur)
        pct = percentiles_ms(latencies_s, pcts=(50, 95))

        log.info(
            "[bench] baseline_results",
            recall_at_5=round(avg_recall_5, 4),
            recall_at_5_strict=round(avg_recall_5_strict, 4),
            recall_at_10=round(avg_recall_10, 4),
            p50_ms=round(pct[50], 2),
            p95_ms=round(pct[95], 2),
        )

        # ─── Step 6: export CSV (single-row baseline summary) ───
        BENCH_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = BENCH_ARTIFACTS_DIR / "baseline_results.csv"
        with out_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "indexer",
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
            )
            w.writerow(
                [
                    "hnsw",
                    16,
                    64,
                    100,
                    round(build_s, 2),
                    round(size_mb, 2),
                    round(avg_recall_5, 4),
                    round(avg_recall_5_strict, 4),
                    round(avg_recall_10, 4),
                    round(pct[50], 2),
                    round(pct[95], 2),
                ]
            )
        log.info("[bench] csv_exported", path=str(out_path))

        # ─── Final summary ───
        total_s = time.perf_counter() - t_start
        # M3 review : `>=` plutôt que strict `>` — match exactement NFR4.
        # P7 review : NFR4 = sélectivité de l'index ANN (mesurée par strict recall),
        # pas le score post-rerank (qui dépend de la qualité du scoring placeholder
        # — non représentatif du runtime applicatif).
        nfr4_pass = avg_recall_5_strict >= 0.90
        nfr5_pass = pct[95] < 200.0
        verdict = "✅ GO" if (nfr4_pass and nfr5_pass) else "⚠️ NEEDS SWEEP"
        log.info(
            "[bench] total_duration_s",
            total_s=round(total_s, 2),
            nfr4_recall_at_5_pass=nfr4_pass,
            nfr5_p95_pass=nfr5_pass,
            verdict=verdict,
        )

    finally:
        await conn.close()


def main() -> None:
    asyncio.run(run_baseline_bench(BenchConfig()))


if __name__ == "__main__":
    main()
