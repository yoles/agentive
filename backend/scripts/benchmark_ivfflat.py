"""ivfflat comparison benchmark — Story 1.3 AC3.

Pour comparer HNSW vs ivfflat sur le même dataset (10k chunks, 384 dims, BGE).

Sweep réduit (recommandation pgvector officielle pour 10k rows : `lists ≈ sqrt(N)` ≈ 100) :
    - lists ∈ [50, 100, 200]
    - probes ∈ [5, 10, 25]
Total : 3 builds x 3 probes = 9 mesures (~10 min).

Output : `_bench_artifacts/ivfflat_results.csv`.

Note : ivfflat n'a pas de paramètre `ef_construction` — seul `lists` (build) et
`probes` (runtime via SET LOCAL ivfflat.probes). Le recall max est plafonné par
`probes / lists` (proportion des centroïdes scannés).
"""

from __future__ import annotations

import asyncio
import csv
import time
import uuid as _uuid
from datetime import UTC, datetime

import structlog

from scripts._bench_common import (
    BENCH_ARTIFACTS_DIR,
    GROUND_TRUTH_TOP_K,
    HNSW_INDEX_BGE,
    IVFFLAT_INDEX_BGE,
    BenchConfig,
    bulk_insert_chunks,
    compute_ground_truth,
    connect_owner,
    create_ivfflat_index,
    drop_index_if_exists,
    format_vector,
    get_or_create_bench_namespace,
    load_ground_truth,
    make_unit_norm_embeddings,
    percentiles_ms,
    pg_relation_size_mb,
    recall_at_k,
    rerank_topk,
    restore_baseline_hnsw_index,
    save_ground_truth,
    truncate_bench_data,
    vacuum_analyze_chunk_embeddings,
)

log = structlog.get_logger("bench.ivfflat")

LISTS_VALUES = [50, 100, 200]
PROBES_VALUES = [5, 10, 25]
WARMUP_QUERIES = 10

CSV_FIELDS = [
    "lists",
    "probes",
    "build_time_s",
    "index_size_mb",
    "recall_at_5",
    "recall_at_5_strict",
    "recall_at_10",
    "latency_p50_ms",
    "latency_p95_ms",
]


async def ivfflat_search_with_rerank(
    conn,
    query,
    model: str,
    probes: int,
    top_k_ann: int,
    top_k_final: int,
    now: datetime,
) -> tuple[list[str], float]:
    """ivfflat-specific ANN+rerank — uses `SET LOCAL ivfflat.probes` instead of `hnsw.ef_search`."""
    t0 = time.perf_counter()
    async with conn.cursor() as cur:
        # `SET LOCAL` doesn't accept placeholders — inline the int.
        await cur.execute(f"SET LOCAL ivfflat.probes = {int(probes)}")
        await cur.execute(
            "SELECT chunk_id::text AS chunk_id, "
            "       (embedding <=> %s::vector) AS distance, "
            "       created_at "
            "FROM chunk_embeddings "
            "WHERE model = %s "
            "ORDER BY distance "
            "LIMIT %s",
            (format_vector(query), model, top_k_ann),
        )
        rows = await cur.fetchall()
    candidates = [
        {
            "chunk_id": _uuid.UUID(str(r["chunk_id"])),
            "distance": float(r["distance"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    reranked = rerank_topk(candidates, now=now, top_k=top_k_final)
    duration = time.perf_counter() - t0
    await conn.commit()
    return [str(c["chunk_id"]) for c in reranked], duration


async def run_ivfflat_sweep() -> None:
    cfg = BenchConfig()
    log.info("[bench-ivfflat] start", lists=LISTS_VALUES, probes=PROBES_VALUES)
    t_start = time.perf_counter()

    BENCH_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = BENCH_ARTIFACTS_DIR / "ivfflat_results.csv"
    rows: list[dict[str, object]] = []

    conn = await connect_owner()
    try:
        ns_id = await get_or_create_bench_namespace(conn)

        # Ensure chunks present
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) AS n FROM memory_chunks WHERE namespace_id = %s",
                (ns_id,),
            )
            row = await cur.fetchone()
            existing = int(row["n"]) if row else 0

        if existing != cfg.n_chunks:
            log.info("[bench-ivfflat] reseeding_chunks", existing=existing)
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

        queries_validation = make_unit_norm_embeddings(
            cfg.n_validation_queries, cfg.dim, cfg.seed_queries_validation
        )
        truth = load_ground_truth(cfg)
        if truth is None:
            await drop_index_if_exists(conn, HNSW_INDEX_BGE)
            await drop_index_if_exists(conn, IVFFLAT_INDEX_BGE)
            truth = await compute_ground_truth(
                conn, queries_validation, cfg.model, top_k=GROUND_TRUTH_TOP_K
            )
            save_ground_truth(cfg, truth)

        queries_latency = make_unit_norm_embeddings(
            cfg.n_latency_queries, cfg.dim, cfg.seed_queries_latency
        )
        now = datetime.now(UTC)

        # Drop both partial indexes before starting sweep (ivfflat sweep recreates only ivfflat)
        await drop_index_if_exists(conn, HNSW_INDEX_BGE)

        # P3 review : streaming CSV writes — header puis flush ligne par ligne.
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            f.flush()

            for lists in LISTS_VALUES:
                await drop_index_if_exists(conn, IVFFLAT_INDEX_BGE)
                build_s = await create_ivfflat_index(
                    conn, IVFFLAT_INDEX_BGE, cfg.model, cfg.dim, lists=lists
                )
                # M9 review : VACUUM ANALYZE avant la mesure de taille.
                await vacuum_analyze_chunk_embeddings(conn)
                size_mb = await pg_relation_size_mb(conn, IVFFLAT_INDEX_BGE)
                log.info(
                    "[bench-ivfflat] index_built",
                    lists=lists,
                    build_s=round(build_s, 2),
                    size_mb=round(size_mb, 2),
                )

                for probes in PROBES_VALUES:
                    # Recall pass
                    recall_5_acc: list[float] = []
                    recall_5_strict_acc: list[float] = []
                    recall_10_acc: list[float] = []
                    for qi, q in enumerate(queries_validation):
                        predicted, _dur = await ivfflat_search_with_rerank(
                            conn,
                            q,
                            cfg.model,
                            probes=probes,
                            top_k_ann=cfg.top_k_ann,
                            top_k_final=10,
                            now=now,
                        )
                        recall_5_acc.append(recall_at_k(predicted, truth[qi], 5))
                        recall_10_acc.append(recall_at_k(predicted, truth[qi], 10))

                        # P7 review : strict recall (top_k_ann == k)
                        predicted_strict, _ds = await ivfflat_search_with_rerank(
                            conn,
                            q,
                            cfg.model,
                            probes=probes,
                            top_k_ann=cfg.top_k_final,
                            top_k_final=cfg.top_k_final,
                            now=now,
                        )
                        recall_5_strict_acc.append(
                            recall_at_k(predicted_strict, truth[qi], cfg.top_k_final)
                        )

                    # M6 review : warmup before latency measurement
                    for q in queries_latency[:WARMUP_QUERIES]:
                        await ivfflat_search_with_rerank(
                            conn,
                            q,
                            cfg.model,
                            probes=probes,
                            top_k_ann=cfg.top_k_ann,
                            top_k_final=cfg.top_k_final,
                            now=now,
                        )

                    # Latency pass
                    latencies_s: list[float] = []
                    for q in queries_latency:
                        _predicted, dur = await ivfflat_search_with_rerank(
                            conn,
                            q,
                            cfg.model,
                            probes=probes,
                            top_k_ann=cfg.top_k_ann,
                            top_k_final=cfg.top_k_final,
                            now=now,
                        )
                        latencies_s.append(dur)
                    pct = percentiles_ms(latencies_s, pcts=(50, 95))

                    row = {
                        "lists": lists,
                        "probes": probes,
                        "build_time_s": round(build_s, 2),
                        "index_size_mb": round(size_mb, 2),
                        "recall_at_5": round(sum(recall_5_acc) / len(recall_5_acc), 4),
                        "recall_at_5_strict": round(
                            sum(recall_5_strict_acc) / len(recall_5_strict_acc), 4
                        ),
                        "recall_at_10": round(sum(recall_10_acc) / len(recall_10_acc), 4),
                        "latency_p50_ms": round(pct[50], 2),
                        "latency_p95_ms": round(pct[95], 2),
                    }
                    rows.append(row)
                    writer.writerow(row)
                    f.flush()
                    log.info("[bench-ivfflat] measured", **row)

        log.info("[bench-ivfflat] csv_exported", path=str(out_path), rows=len(rows))

        total_s = time.perf_counter() - t_start
        log.info("[bench-ivfflat] total_duration_s", total_s=round(total_s, 2))

    finally:
        # P5 review : restaure l'index HNSW baseline. Le sweep ivfflat droppe
        # initialement l'HNSW partiel (pour brute-force ground truth) et ne le
        # recrée pas — on doit le restaurer pour ne pas casser Epic 3 démarrage.
        try:
            await drop_index_if_exists(conn, IVFFLAT_INDEX_BGE)
            await restore_baseline_hnsw_index(conn)
            log.info("[bench-ivfflat] baseline_hnsw_restored", m=16, ef_construction=64)
        except Exception as exc:
            # Best-effort restore en finally — on log mais on ne propage pas
            # (sinon on masque l'exception originale du sweep).
            log.error("[bench-ivfflat] baseline_restore_failed", error=str(exc))
        await conn.close()


def main() -> None:
    asyncio.run(run_ivfflat_sweep())


if __name__ == "__main__":
    main()
