# Runbook — Re-jouer le bench M4 pgvector HNSW

> **Context** : harness de mesure (Story 1.3, gating NFR4/NFR5). Génère 10k
> chunks synthétiques + sweep paramétrique HNSW + comparaison ivfflat + rapport
> Markdown auto. Source de vérité : `docs/decisions/hnsw-tuning.md`.

## Quand l'utiliser

- **Avant chaque upgrade pgvector** ≥ 0.5 (cf [`CONVENTIONS.md`](../../CONVENTIONS.md) "Versioning critique").
- **Avant tout changement** des paramètres HNSW (`m`, `ef_construction`, `ef_search`) dans une migration Alembic — détecter une régression latence ou recall.
- **Mensuellement** une fois que Story 7.6 (monitoring qualité) est livrée — alerte si `recall@5 < 0.90` ou `p95 > 100ms` (seuil dégradé).
- **Pour debugger** une dégradation de perf observée sur le Push Memory (Story 3.5) ou un échec NFR5 sur des dashboards.

## Pré-requis

- Stack dev tournant (`make up`). Les rôles `agentive_owner` (DDL bypass RLS) doivent être dispo (créés par `infra/postgres/init.sql`).
- Migrations Alembic à jour : `make migrate`.
- Le repo doit être propre (pas de chunks bench résiduels d'un run précédent — le harness les supprime de toute façon, idempotent).

## Usage

### Pipeline complet (cycle gating Story 1.3)

```bash
# 1. Baseline + ground truth (~1 min) — seed les 10k chunks + cache ground truth
make bench

# 2. Sweep complet (~25 min, 27 combos) — OU fast (~10 min, 8 combos pour CI)
make bench-hnsw          # complet
# make bench-hnsw-fast   # variant rapide pour CI

# 3. Comparaison ivfflat (~10 min, 9 combos)
make bench-ivfflat

# 4. Génération du rapport hnsw-tuning.md (lit les CSV produits)
make bench-report

# Output :
# - backend/_bench_artifacts/baseline_results.csv
# - backend/_bench_artifacts/hnsw_results.csv
# - backend/_bench_artifacts/ivfflat_results.csv
# - backend/_bench_artifacts/gt_*.json (cache ground truth, regenerable)
# - docs/decisions/hnsw-tuning.md (rapport auto-généré)
```

### Run rapide pour vérifier non-régression

```bash
make bench && make bench-hnsw-fast && make bench-report
# Lire docs/decisions/hnsw-tuning.md, comparer avec la version commitée.
# Si delta > 10% sur p95 ou > 2pt sur recall@5 → investiguer avant merge.
```

### Re-générer uniquement le rapport (sans re-mesure)

```bash
make bench-report
# Utile après avoir édité la logique de _bench_report.py sans toucher aux mesures.
```

### Job CI manuel

Le job `bench-m4` existe dans `.github/workflows/ci.yml` mais ne tourne **que** sur `workflow_dispatch` :

```bash
gh workflow run ci.yml
# Puis récupérer les artifacts (CSV + Markdown) dans la liste des workflow runs.
```

## Sortie attendue (cas nominal — config baseline)

```text
[bench] versions postgres="PostgreSQL 17..." pgvector="0.7.0"
[bench] namespace_ready namespace_id=...
[bench] embeddings_generated n=10000 dim=384 gen_s=0.4
[bench] chunks_inserted n=10000 insert_s=12.3
[bench] ground_truth_computed n_queries=50 duration_s=8.7
[bench] hnsw_baseline_built m=16 ef_construction=64 build_s=118.4 index_size_mb=18.6
[bench] baseline_results recall_at_5=0.96 recall_at_10=0.94 p50_ms=22.1 p95_ms=48.3
[bench] total_duration_s total_s=145.2 nfr4_recall_at_5_pass=True nfr5_p95_pass=True verdict='✅ GO'
```

## Diagnostic — cas d'échec

### `recall@5 < 0.90` sur baseline

- **Cause probable 1** : embeddings synthétiques mal distribués → vérifier la norme L2 (cf `_bench_common.make_unit_norm_embeddings` : norme = 1 ± 1e-6).
- **Cause probable 2** : index HNSW corrompu → `make migrate` puis `DROP INDEX` manuel + re-run.
- **Cause probable 3** : ground truth caché obsolète (changement de seed sans clear cache) → supprimer `backend/_bench_artifacts/gt_*.json` et re-run.

### `p95 > 200ms` sur baseline (NFR5 violé)

- **Cause probable 1** : Postgres pas warmé → première mesure inclut le chargement page cache. Re-run, le 2ème run doit être stable.
- **Cause probable 2** : tuning Postgres insuffisant → appliquer AC7 Story 1.3 (`shared_buffers=512MB`, `work_mem=32MB`, `maintenance_work_mem=256MB`) dans `infra/postgres/postgresql.conf`, `docker compose restart db`, re-run.
- **Cause probable 3** : reranking Python plus coûteux que le placeholder simulé → analyser la latence par étape (`time.perf_counter` autour de chaque sous-étape de `ann_search_with_rerank`).

### Build d'index très lent (> 5 min sur 10k chunks)

- Augmenter `maintenance_work_mem` à 256MB+ (Architecture ligne 599 H6).
- Vérifier que la DB n'est pas en cours d'autovacuum sur `chunk_embeddings` (`SELECT * FROM pg_stat_progress_vacuum`).

## Index lifecycle (post code review P5)

Les sweeps `benchmark_hnsw.py` et `benchmark_ivfflat.py` **restaurent automatiquement** l'index HNSW baseline (`m=16, ef_construction=64`) en `finally` block — même si le sweep crashe à mi-parcours. Conséquences :

- Le DB dev reste utilisable pour Epic 3 immédiatement après le bench (pas d'index manquant ni de params non-baseline).
- Si la restoration échoue (rare — log error `[bench-*] baseline_restore_failed`), réappliquer manuellement :

  ```bash
  make backend-shell
  uv run python -c "
  import asyncio
  from scripts._bench_common import connect_owner, restore_baseline_hnsw_index
  async def main():
      conn = await connect_owner()
      try: await restore_baseline_hnsw_index(conn)
      finally: await conn.close()
  asyncio.run(main())
  "
  ```

- Ou alternativement : `make migrate` (qui fait DROP + CREATE selon la définition Story 1.1) si la migration baseline est encore à jour.

## Cleanup

Le harness ne touche que la table `memory_chunks` (FK CASCADE → `chunk_embeddings`) du namespace `bench-m4-synthetic`. Pour wipe manuellement :

```sql
DELETE FROM namespaces WHERE name = 'bench-m4-synthetic';
-- (CASCADE supprime tous les chunks + embeddings liés)
```

Les artefacts CSV + ground truth sont dans `backend/_bench_artifacts/` (gitignored).
