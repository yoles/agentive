"""Utilities partagées pour le benchmark M4 pgvector — Story 1.3.

Ce module regroupe :
- Génération de chunks synthétiques + embeddings unit-norm (numpy).
- Bulk insert via psycopg AsyncCopy (10x plus rapide que session.add_all).
- Calcul ground truth brute-force (seq scan + drop temporaire de l'index HNSW).
- Métriques recall@k set-based et reranking placeholder (cosine x 0.7 + recency x 0.3).
- Helpers SQL : `SET LOCAL hnsw.ef_search`, `pg_relation_size`.

Convention : toutes les fonctions sont async et prennent une `psycopg.AsyncConnection`.
Connection role = `agentive_owner` (DDL + bypass RLS — cf Story 1.1 ADR + Story 1.2 m3-spike-result.md).
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

import numpy as np
import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from agentive_backend.shared.config import settings

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Types
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ChunkMeta(TypedDict):
    chunk_id: uuid.UUID
    distance: float
    created_at: datetime


@dataclass(frozen=True)
class BenchConfig:
    n_chunks: int = 10_000
    dim: int = 384
    model: str = "bge-small-en-v1.5"
    seed_corpus: int = 42
    seed_queries_validation: int = 17
    seed_queries_latency: int = 31
    n_validation_queries: int = 50
    n_latency_queries: int = 100
    top_k_ann: int = 50
    top_k_final: int = 5
    # P7 review : créés à `now - random(0, created_at_jitter_days)` pour rendre
    # le rerank `0.7*cosine + 0.3*recency` non-dégénéré (sinon recency_decay = 1.0
    # constant ⇒ rerank = ordre cosine pur, recall mesure recall@top_k_ann pas k).
    created_at_jitter_days: float = 90.0
    # P7 : seed dédié au jitter (indépendant du seed_corpus pour permettre de
    # changer la distribution temporelle sans regénérer les embeddings).
    seed_created_at: int = 73


BENCH_NAMESPACE_NAME = "bench-m4-synthetic"
BENCH_ARTIFACTS_DIR = Path("_bench_artifacts")
HNSW_INDEX_BGE = "chunk_embeddings_bge_hnsw"
HNSW_INDEX_OPENAI = "chunk_embeddings_openai_hnsw"
IVFFLAT_INDEX_BGE = "chunk_embeddings_bge_ivfflat"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Connection helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def make_psycopg_dsn() -> str:
    """Convert SQLAlchemy `postgresql+psycopg://...` to plain `postgresql://...` for psycopg."""
    sqla_dsn = str(settings.database_url_owner)
    return sqla_dsn.replace("postgresql+psycopg://", "postgresql://", 1)


async def connect_owner() -> AsyncConnection:
    """Open a psycopg AsyncConnection as `agentive_owner` (DDL + bypass RLS)."""
    return await psycopg.AsyncConnection.connect(make_psycopg_dsn(), row_factory=dict_row)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Synthetic data generation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def make_unit_norm_embeddings(n: int, dim: int, seed: int) -> np.ndarray:
    """Nxdim float32 array with each row L2-normalized (unit vectors on the dim-sphere)."""
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal(size=(n, dim), dtype=np.float32)
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (raw / norms).astype(np.float32)


def format_vector(arr: np.ndarray) -> str:
    """Format a 1-D numpy vector as pgvector literal `'[1.0,2.0,...]'`."""
    return "[" + ",".join(f"{x:.6f}" for x in arr.tolist()) + "]"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Schema bootstrap (namespace + chunks)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def get_or_create_bench_namespace(conn: AsyncConnection) -> uuid.UUID:
    """Idempotent : return the UUID of the bench namespace, creating it if missing."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM namespaces WHERE name = %s",
            (BENCH_NAMESPACE_NAME,),
        )
        row = await cur.fetchone()
        if row is not None:
            return uuid.UUID(str(row["id"]))

        await cur.execute(
            """
            INSERT INTO namespaces (name, type, department, project, embedding_backend)
            VALUES (%s, 'operationnelle', 'sprint-0', 'bench-m4', 'local')
            RETURNING id
            """,
            (BENCH_NAMESPACE_NAME,),
        )
        row = await cur.fetchone()
        await conn.commit()
        if row is None:
            raise RuntimeError("Failed to create bench namespace")
        return uuid.UUID(str(row["id"]))


async def truncate_bench_data(conn: AsyncConnection, namespace_id: uuid.UUID) -> None:
    """Wipe previous bench rows (chunks + embeddings via FK CASCADE).

    Idempotent — safe to call before each `make bench` run to start from clean state.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM memory_chunks WHERE namespace_id = %s",
            (namespace_id,),
        )
        await conn.commit()


BENCH_UUID_NAMESPACE = uuid.UUID("11111111-2222-3333-4444-555555555555")


def deterministic_chunk_id(idx: int, seed: int) -> uuid.UUID:
    """UUIDv5 derived from (seed, idx) — stable across runs avec the same seed.

    Critical for ground truth caching : if chunk_ids changed between runs (uuid4),
    the cached truth file becomes stale and recall would be ~0 (no overlap).
    """
    return uuid.uuid5(BENCH_UUID_NAMESPACE, f"bench-{seed}-{idx}")


async def bulk_insert_chunks(
    conn: AsyncConnection,
    namespace_id: uuid.UUID,
    embeddings: np.ndarray,
    model: str,
    seed: int,
    *,
    batch_size: int = 500,
    created_at_jitter_days: float = 90.0,
    seed_created_at: int = 73,
) -> list[uuid.UUID]:
    """Batched INSERT of n rows into memory_chunks + chunk_embeddings.

    NB : COPY FROM is not supported by Postgres when `FORCE ROW LEVEL SECURITY`
    is set (cf migration 20260419_000000_initial.py:454-455 + Architecture security
    hardening line 619-624). On uses INSERT executemany() instead — reasonably fast
    on 10k rows (~5-15s) and respects the RLS policy `tenant_id IS NULL`.

    `seed` is required (M7 review : empêche un découplage silencieux entre le
    seed de génération d'embeddings et les chunk_ids déterministes — qui doit
    être le même pour la validité du cache ground truth).

    `created_at_jitter_days` répand les `created_at` sur `[now - jitter, now]`
    selon une distribution uniforme seedée par `seed_created_at`. P7 review :
    rend `recency_decay` non-constant et le rerank discriminant (sinon le score
    combiné conserve l'ordre cosine et `recall@k` sature à `recall@top_k_ann`).
    Mettre `created_at_jitter_days=0.0` reproduit l'ancien comportement (now
    constant) — utile pour tests de non-régression.
    """
    n = len(embeddings)
    chunk_ids = [deterministic_chunk_id(i, seed) for i in range(n)]
    now = datetime.now(UTC)

    if created_at_jitter_days <= 0.0:
        created_ats: list[datetime] = [now] * n
    else:
        rng = np.random.default_rng(seed_created_at)
        offsets_days = rng.uniform(0.0, created_at_jitter_days, size=n)
        created_ats = [now - timedelta(days=float(d)) for d in offsets_days]

    chunk_rows = [
        (str(cid), str(namespace_id), f"synthetic chunk {cid}", "{}", ts, None)
        for cid, ts in zip(chunk_ids, created_ats, strict=True)
    ]
    embedding_rows = [
        (str(cid), model, format_vector(emb), ts, None)
        for cid, emb, ts in zip(chunk_ids, embeddings, created_ats, strict=True)
    ]

    async with conn.cursor() as cur:
        for start in range(0, n, batch_size):
            await cur.executemany(
                "INSERT INTO memory_chunks (id, namespace_id, content, metadata, created_at, tenant_id) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
                chunk_rows[start : start + batch_size],
            )
        for start in range(0, n, batch_size):
            await cur.executemany(
                "INSERT INTO chunk_embeddings (chunk_id, model, embedding, created_at, tenant_id) "
                "VALUES (%s, %s, %s::vector, %s, %s)",
                embedding_rows[start : start + batch_size],
            )

    await conn.commit()
    return chunk_ids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Index lifecycle (HNSW + ivfflat)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def drop_index_if_exists(conn: AsyncConnection, index_name: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            sql.SQL("DROP INDEX IF EXISTS {idx}").format(idx=sql.Identifier(index_name))
        )
    await conn.commit()


_MAINT_WORK_MEM_RE = re.compile(r"^[0-9]+(MB|GB|kB)$")


def _validate_maintenance_work_mem(value: str) -> str:
    """M2 review : `SET LOCAL maintenance_work_mem` ne supporte pas `%s` placeholders.
    Au lieu d'interpoler en f-string (vecteur d'injection latent si la valeur venait
    d'une source externe), on whitelist le format Postgres (digits + suffixe MB/GB/kB).
    Bench is owner-only / local config, mais durcir évite les surprises futures.
    """
    if not _MAINT_WORK_MEM_RE.match(value):
        raise ValueError(
            f"Invalid maintenance_work_mem={value!r} — expected pattern '<int>(MB|GB|kB)'"
        )
    return value


async def create_hnsw_index(
    conn: AsyncConnection,
    index_name: str,
    model: str,
    dim: int,
    m: int,
    ef_construction: int,
    maintenance_work_mem: str = "32MB",
) -> float:
    """CREATE the HNSW partial index for the given model. Returns build duration in seconds.

    `maintenance_work_mem` est explicitement réduit (default 128MB) pour tenir dans
    le `/dev/shm` 64MB du container Postgres dev (cf docker-compose.yml). Si le
    container db a `shm_size: 1g`, ce param peut être bumpé pour accélérer le build.
    """
    mwm = _validate_maintenance_work_mem(maintenance_work_mem)
    create_stmt = sql.SQL(
        "CREATE INDEX {idx} ON chunk_embeddings "
        "USING hnsw ((embedding::vector({dim})) vector_cosine_ops) "
        "WITH (m = {m}, ef_construction = {ef_c}) "
        "WHERE model = {model}"
    ).format(
        idx=sql.Identifier(index_name),
        dim=sql.Literal(int(dim)),
        m=sql.Literal(int(m)),
        ef_c=sql.Literal(int(ef_construction)),
        model=sql.Literal(model),
    )
    t0 = time.perf_counter()
    async with conn.cursor() as cur:
        # `SET LOCAL maintenance_work_mem = <quoted-string>` : Postgres veut une
        # string SQL littérale (pas un identifier). On utilise sql.Literal après
        # validation regex — défense en profondeur.
        await cur.execute(
            sql.SQL("SET LOCAL maintenance_work_mem = {v}").format(v=sql.Literal(mwm))
        )
        await cur.execute(create_stmt)
    await conn.commit()
    return time.perf_counter() - t0


async def create_ivfflat_index(
    conn: AsyncConnection,
    index_name: str,
    model: str,
    dim: int,
    lists: int,
) -> float:
    """CREATE the ivfflat partial index. Returns build duration in seconds."""
    create_stmt = sql.SQL(
        "CREATE INDEX {idx} ON chunk_embeddings "
        "USING ivfflat ((embedding::vector({dim})) vector_cosine_ops) "
        "WITH (lists = {lists}) "
        "WHERE model = {model}"
    ).format(
        idx=sql.Identifier(index_name),
        dim=sql.Literal(int(dim)),
        lists=sql.Literal(int(lists)),
        model=sql.Literal(model),
    )
    t0 = time.perf_counter()
    async with conn.cursor() as cur:
        await cur.execute(create_stmt)
        # ANALYZE required for ivfflat planner to pick the index
        await cur.execute("ANALYZE chunk_embeddings")
    await conn.commit()
    return time.perf_counter() - t0


async def vacuum_analyze_chunk_embeddings(conn: AsyncConnection) -> None:
    """M9 review : pg_relation_size sans VACUUM ANALYZE peut sous-estimer la taille
    finale de l'index (pages pas encore "settled"). T3.3 de la story dit explicitement
    "après VACUUM ANALYZE". VACUUM ne peut pas tourner dans une transaction → on
    commit avant, puis on bascule en autocommit le temps d'un statement.
    """
    await conn.commit()
    prev_autocommit = conn.autocommit
    await conn.set_autocommit(True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("VACUUM ANALYZE chunk_embeddings")
    finally:
        await conn.set_autocommit(prev_autocommit)


async def pg_relation_size_mb(conn: AsyncConnection, index_name: str) -> float:
    """Return index size in MB (pg_relation_size, index only).

    M5 review : si l'index est droppé entre la mesure et le SELECT (race rare mais
    possible avec concurrent ops), `pg_relation_size` lève `UndefinedTable`. On
    catch et retourne 0.0 — la mesure size est non-critique vs la mesure latence.
    """
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT pg_relation_size(%s) AS size_bytes", (index_name,))
            row = await cur.fetchone()
            if row is None:
                return 0.0
            return float(row["size_bytes"]) / (1024 * 1024)
    except psycopg.errors.UndefinedTable:
        await conn.rollback()
        return 0.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Ground truth (brute-force top-K via seq scan)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def compute_ground_truth(
    conn: AsyncConnection,
    queries: np.ndarray,
    model: str,
    top_k: int,
) -> list[list[str]]:
    """For each query, brute-force top-K chunk_ids by cosine distance.

    Forces seq scan via `enable_indexscan = OFF` to bypass any HNSW/ivfflat index that may exist.
    Returns: list (per query) of top_k chunk_id strings, ordered by ascending distance.
    """
    truth: list[list[str]] = []
    async with conn.cursor() as cur:
        # Ensure no index is used — force exact top-K
        await cur.execute("SET LOCAL enable_indexscan = OFF")
        await cur.execute("SET LOCAL enable_indexonlyscan = OFF")
        await cur.execute("SET LOCAL enable_bitmapscan = OFF")

        for q in queries:
            await cur.execute(
                "SELECT chunk_id::text AS chunk_id "
                "FROM chunk_embeddings "
                "WHERE model = %s "
                "ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                (model, format_vector(q), top_k),
            )
            rows = await cur.fetchall()
            truth.append([str(r["chunk_id"]) for r in rows])
    return truth


# P7 review : top_k caché = 10 (recall@5 + recall@10 + recall@5_strict tous tirés
# du même set top-10). Garder en module-level pour qu'un changement nécessite
# d'invalider le cache via un nouveau key.
GROUND_TRUTH_TOP_K = 10


def cache_path_ground_truth(cfg: BenchConfig) -> Path:
    """P4 review : la clé doit inclure tous les paramètres qui peuvent
    invalider le cache silencieusement. Si on change `model`, `top_k`, ou
    `n_validation_queries`, l'ancien cache devient corrompu :
    - `n_validation_queries` augmente → IndexError au query 51 sur `truth[qi]`.
    - `model` change (bge → openai 1536d) → recall corrompu sans crash.
    - `top_k` augmente → IndexError sur recall_at_k(predicted, truth[qi], k).
    """
    BENCH_ARTIFACTS_DIR.mkdir(exist_ok=True)
    # Hash le model pour éviter les caractères pathologiques dans le filename.
    model_tag = re.sub(r"[^A-Za-z0-9]+", "-", cfg.model).strip("-")
    key = (
        f"gt_n{cfg.n_chunks}_dim{cfg.dim}_"
        f"sc{cfg.seed_corpus}_sq{cfg.seed_queries_validation}_"
        f"q{cfg.n_validation_queries}_k{GROUND_TRUTH_TOP_K}_m-{model_tag}.json"
    )
    return BENCH_ARTIFACTS_DIR / key


def save_ground_truth(cfg: BenchConfig, truth: list[list[str]]) -> Path:
    """Save GT with metadata sidecar for sanity checks at load time."""
    path = cache_path_ground_truth(cfg)
    payload = {
        "metadata": {
            "n_chunks": cfg.n_chunks,
            "dim": cfg.dim,
            "model": cfg.model,
            "seed_corpus": cfg.seed_corpus,
            "seed_queries_validation": cfg.seed_queries_validation,
            "n_validation_queries": cfg.n_validation_queries,
            "top_k": GROUND_TRUTH_TOP_K,
        },
        "truth": truth,
    }
    path.write_text(json.dumps(payload, indent=None), encoding="utf-8")
    return path


def load_ground_truth(cfg: BenchConfig) -> list[list[str]] | None:
    """P4 review : valide les métadonnées avant utilisation. Si le payload n'a pas
    le format attendu (anciens caches), on traite comme un miss et on regénère.
    Retourne None si absent/invalide → caller compute + save.
    """
    path = cache_path_ground_truth(cfg)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return None
    # Format pré-P4 (liste plate) ou format invalide → invalider.
    if not isinstance(payload, dict) or "metadata" not in payload or "truth" not in payload:
        return None
    meta = payload["metadata"]
    expected = {
        "n_chunks": cfg.n_chunks,
        "dim": cfg.dim,
        "model": cfg.model,
        "seed_corpus": cfg.seed_corpus,
        "seed_queries_validation": cfg.seed_queries_validation,
        "n_validation_queries": cfg.n_validation_queries,
        "top_k": GROUND_TRUTH_TOP_K,
    }
    if any(meta.get(k) != v for k, v in expected.items()):
        return None
    truth = payload["truth"]
    if not isinstance(truth, list) or len(truth) != cfg.n_validation_queries:
        return None
    return truth


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Metrics (recall + reranking)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def recall_at_k(predicted: list[str], truth: list[str], k: int) -> float:
    """Set-based recall@k = |predicted_top_k ∩ truth_top_k| / k.

    Returns 0.0 if k == 0 to avoid division by zero (caller's responsibility to pass k > 0).
    """
    if k == 0:
        return 0.0
    pred_set = set(predicted[:k])
    truth_set = set(truth[:k])
    return len(pred_set & truth_set) / k


def recency_decay(created_at: datetime, now: datetime, halflife_days: float = 30.0) -> float:
    """Exponential decay : score = exp(-age_days / halflife_days).

    M1 review : pour des `created_at` futurs (clock skew), on **clampe l'age à 0**
    ⇒ le score retourne 1.0 (boost max). C'est intentionnel : un contenu daté
    légèrement dans le futur est traité comme "tout frais" plutôt que de générer
    un score > 1 (qui briserait l'invariant `score ∈ [0, 1]` du rerank). La
    précédente formulation "clamped to [0, 1]" était ambiguë.
    """
    age_days = max(0.0, (now - created_at).total_seconds() / 86400.0)
    return math.exp(-age_days / halflife_days)


def rerank_topk(
    candidates: list[ChunkMeta],
    now: datetime,
    alpha: float = 0.7,
    top_k: int = 5,
) -> list[ChunkMeta]:
    """Combine cosine score (1 - distance) and recency decay, return top-K.

    final_score = alpha * (1 - distance) + (1 - alpha) * recency_decay(created_at)
    """
    scored = [
        (
            alpha * (1.0 - c["distance"]) + (1.0 - alpha) * recency_decay(c["created_at"], now),
            c,
        )
        for c in candidates
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _score, c in scored[:top_k]]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ANN search + end-to-end measurement (ANN + rerank)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def ann_search_with_rerank(
    conn: AsyncConnection,
    query: np.ndarray,
    model: str,
    ef_search: int | None,
    top_k_ann: int,
    top_k_final: int,
    now: datetime,
) -> tuple[list[str], float]:
    """End-to-end ANN + Python rerank. Returns (top_k_final chunk_ids, wall_clock_seconds).

    The wall_clock duration covers (a) SQL ANN, (b) row marshalling, (c) Python rerank — that
    is the latency NFR5 constrains.
    """
    t0 = time.perf_counter()
    async with conn.cursor() as cur:
        if ef_search is not None:
            # `SET LOCAL` doesn't accept placeholders — inline the int (no injection risk).
            await cur.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
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
    candidates: list[ChunkMeta] = [
        {
            "chunk_id": uuid.UUID(str(r["chunk_id"])),
            "distance": float(r["distance"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    reranked = rerank_topk(candidates, now=now, top_k=top_k_final)
    duration = time.perf_counter() - t0
    # Need to commit/rollback to release the SET LOCAL transaction — caller must close cleanly
    await conn.commit()
    return [str(c["chunk_id"]) for c in reranked], duration


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Misc helpers (logging, percentiles, version queries)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def percentiles_ms(durations_s: list[float], pcts: tuple[int, ...] = (50, 95)) -> dict[int, float]:
    """Compute percentiles of a duration list (in seconds), return values in milliseconds."""
    arr = np.array(durations_s, dtype=np.float64) * 1000.0
    return {p: float(np.percentile(arr, p)) for p in pcts}


async def fetch_versions(conn: AsyncConnection) -> dict[str, str]:
    """Return pgvector + PostgreSQL versions for ADR documentation."""
    versions: dict[str, str] = {}
    async with conn.cursor() as cur:
        await cur.execute("SELECT version() AS v")
        row = await cur.fetchone()
        if row is not None:
            versions["postgres"] = str(row["v"])
        await cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = await cur.fetchone()
        if row is not None:
            versions["pgvector"] = str(row["extversion"])
    return versions


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# P5 review — Index lifecycle helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


BASELINE_HNSW_M = 16
BASELINE_HNSW_EF_CONSTRUCTION = 64


async def restore_baseline_hnsw_index(
    conn: AsyncConnection,
    *,
    index_name: str = HNSW_INDEX_BGE,
    model: str = "bge-small-en-v1.5",
    dim: int = 384,
) -> None:
    """P5 review : à la fin d'un sweep, l'index `chunk_embeddings_bge_hnsw` reste
    avec les params du dernier triplet sweepé (ou est purement supprimé après
    ivfflat). Tout démarrage d'Epic 3 sur ce DB dev hit alors un index non-baseline
    ou un seq scan. On droppe + recrée avec les params baseline (m=16, ef_c=64).

    Idempotent : safe même si l'index n'existe pas. Utilisé en `finally` block des
    sweeps benchmark_hnsw.py et benchmark_ivfflat.py.
    """
    await drop_index_if_exists(conn, index_name)
    await create_hnsw_index(
        conn,
        index_name=index_name,
        model=model,
        dim=dim,
        m=BASELINE_HNSW_M,
        ef_construction=BASELINE_HNSW_EF_CONSTRUCTION,
    )
