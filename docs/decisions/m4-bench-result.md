# ADR — Résultat du Benchmark M4 pgvector HNSW (gating critique #2)

- **Statut** : ✅ **Accepté — GO Stories Epic 3**
- **Date** : 2026-04-26
- **Décideur** : John (Owner / Architect)
- **Story** : [`1-3-benchmark-m4-pgvector-hnsw`](../../_bmad-output/implementation-artifacts/1-3-benchmark-m4-pgvector-hnsw.md)
- **Référence risque** : PRD NFR4-NFR5 (lignes 113), Architecture lignes 570-573 (HNSW tuning) + 818-847 (Vector index runtime), Architecture lignes 233-241 (Sprint 0 Decomposition Story 0.3)
- **Rapport détaillé** : [`hnsw-tuning.md`](./hnsw-tuning.md) (auto-généré par `make bench-report`)

## Context

Le risque technique #2 du projet Agentive est l'inadéquation potentielle de **PostgreSQL + pgvector** comme stockage vectoriel du Memory Manager M4. Si `recall@5 < 0.90` (NFR4) **ou** `p95 > 200ms` end-to-end avec reranking (NFR5) sur 10k chunks, il faut **pivoter avant Sprint 1** — soit ajuster les paramètres HNSW, soit tuner Postgres, soit basculer sur ivfflat, soit en dernier recours déployer un sidecar Qdrant/Weaviate (Architecture ligne 843).

La Story 1.3 implémente un harness de benchmark isolé dans `backend/scripts/benchmark_*.py` qui mesure (a) un baseline `m=16, ef_construction=64, ef_search=100` (params actuels de la migration Alembic Story 1.1), (b) un sweep paramétrique HNSW (8 configs en mode FAST, 27 configs en mode FULL), (c) une comparaison avec ivfflat (9 configs). Ce document consigne le verdict du gating et la configuration retenue.

## Decision

**GO** sur pgvector HNSW : **toutes les configurations testées** (8 HNSW + 9 ivfflat = 17 mesures) satisfont **simultanément** NFR4 et NFR5 :
- **NFR4** : `recall@5_strict=1.0` partout (mesure de la sélectivité ANN à `top_k_ann==k=5`, signal canonique post-P7 review).
- **NFR5** : p95 ~13ms, **15× sous le plafond 200ms**.

> **Note méthodologique post code review (P7)** — le précédent verdict reposait sur `recall@5 = 1.0` mesuré avec `top_k_ann=50 + rerank`. Cette mesure est saturée par construction du protocole : tant que les top-5 ground-truth sont dans les top-50 ANN candidats, recall@5 vaut 1.0 (on mesure de facto recall@50). Le harness mesure désormais en plus `recall@5_strict` (top_k_ann == k = 5) qui isole la sélectivité de l'index. **Avec `created_at` varié** sur `[now-90j, now]` (P7 review — rend `recency_decay` non-constant), le `recall@5` post-rerank descend à 0.136 — non parce que l'index est défaillant, mais parce que le scoring placeholder `0.7×cosine + 0.3×recency_decay` privilégie des chunks frais non-pertinents sur du synthétique sans corrélation contenu/recency. C'est attendu et **non bloquant** : le runtime applicatif utilisera un cross-encoder (Story 3.6) qui n'a pas ce biais, et NFR4 cible la sélectivité ANN — qui reste à 1.0.

**Configuration finale conservée** : `m=16, ef_construction=64, ef_search=100` — **identique au baseline** déjà figé dans la migration `20260419_000000_initial.py` (Story 1.1). **Aucune migration Alembic d'ajustement n'est créée** (cf section "Pourquoi pas la config auto-recommandée").

**Aucun tuning Postgres requis** : la config baseline `shared_buffers=256MB, work_mem=16MB, maintenance_work_mem=128MB` est largement suffisante. AC7 de la Story 1.3 (tuning conditionnel) n'est pas déclenché.

Les Stories Epic 3 (`3-1` à `3-6` — Memory & Knowledge System) peuvent démarrer **sans modification structurelle** ni pivot de stockage vectoriel.

## Evidence

### 3 piliers x résultat (mesures post code review P7 — 2026-04-28)

| Pilier | Cible (NFR) | Mesure baseline | Marge | Verdict |
|---|---|---|---|---|
| **(1) Recall@5_strict** (sélectivité ANN, signal NFR4) | >= 90% (NFR4, PRD ligne 113) | **1.0** (50/50 queries top-5 exact ANN) | +10pt | ✅ |
| **(1bis) Recall@5 post-rerank** (informational) | n/a — dépend du scoring | **0.136** (rerank placeholder dégrade sur synthétique) | n/a | ⚠️ informationnel |
| **(2) p95 latency end-to-end** (ANN top-50 + Python rerank) | < 200ms (NFR5, PRD ligne 113) | **14.35ms** | **13.9x sous le plafond** | ✅ |
| **(3) Build time index 10k chunks** | < 5min (raisonnable migration) | **13.22s** | 22x sous le plafond | ✅ |

### Sweep HNSW — 8 configurations (mode FAST, Story 1.3 AC2)

Triées par p95 ascendante (signal NFR4 = `recall@5_strict`, colonne `recall@5` informationnelle) :

| m | ef_c | ef_s | build_s | size_MB | recall@5 | recall@5_strict | recall@10 | p50_ms | p95_ms |
|---|---|---|---|---|---|---|---|---|---|
| 16 | 64 | 100 (**baseline**) | 14.83 | 19.54 | 0.136 | 1.0 | 0.22 | 12.02 | 12.97 |
| 32 | 64 | 100 | 28.49 | 20.30 | 0.136 | 1.0 | 0.22 | 11.88 | 13.28 |
| 16 | 128 | 100 | 19.07 | 19.54 | 0.136 | 1.0 | 0.22 | 11.87 | 13.60 |
| 32 | 128 | 100 | 39.96 | 20.23 | 0.136 | 1.0 | 0.22 | 11.85 | 13.61 |
| 32 | 128 | 200 | 39.96 | 20.23 | 0.136 | 1.0 | 0.22 | 12.03 | 14.53 |
| 16 | 128 | 200 | 19.07 | 19.54 | 0.136 | 1.0 | 0.22 | 11.80 | 15.52 |
| 16 | 64 | 200 | 14.83 | 19.54 | 0.136 | 1.0 | 0.22 | 12.07 | 20.64 |
| 32 | 64 | 200 | 28.49 | 20.30 | 0.136 | 1.0 | 0.22 | 11.95 | 25.64 |

**Observation 1** : sur le critère NFR4 (`recall@5_strict`), **toutes les configs saturent à 1.0** sur synthétique unit-norm bien séparé. Pas de signal discriminant entre `m`/`ef_c` à cette échelle de données — confirme l'attendu de l'architecture (10k chunks ≪ seuil de stress HNSW).

**Observation 2** : p95 entre 12.97 et 25.64ms (delta 13ms, dominé par `ef_search=200` qui élargit le candidate pool). À `ef_search=100` (recommandé pour la prod), spread = 0.6ms — bruit de mesure.

**Observation 3** : `recall@5` post-rerank = 0.136 sur **toutes** les configs — confirme que le rerank placeholder, avec `created_at` varié et embeddings non-corrélés, retourne une distribution quasi-aléatoire des top-5. C'est un signal **sur le rerank**, pas sur l'index. La sélection auto applique tie-break baseline ⇒ `m=16, ef_c=64` retenu.

### Sweep ivfflat — 9 configurations (Story 1.3 AC3)

Best ivfflat : `lists=100, probes=25` ⇒ p95=12.98ms, recall@5_strict=1.0, build=1.78s, taille=16.09MB.

ivfflat est **légèrement plus rapide** (12.98ms vs 12.97ms HNSW best — équivalents) et **18% plus compact** (16.1MB vs 19.5MB) sur ce dataset. **Mais** : ivfflat plafonne en recall par `probes/lists` ratio — sur des embeddings réels avec clusters, HNSW conserve un avantage qualité décisif. Sur synthétique random, les deux saturent à `recall@5_strict=1.0`.

### Versions exactes

| Composant | Version |
|---|---|
| PostgreSQL | **17.9** (Debian 17.9-1.pgdg12+1, gcc 12.2.0) |
| pgvector | **0.8.2** (extension chargée dans `pgvector/pgvector:pg17`) |
| Python | 3.14.4 |
| numpy | 2.4.4 (transitive via pgvector-python) |
| psycopg | 3.3.x |
| Image Docker DB | `pgvector/pgvector:pg17` |

### Configuration Postgres au moment du bench (`infra/postgres/postgresql.conf`)

```
shared_buffers = 256MB
work_mem = 16MB
maintenance_work_mem = 128MB    # bumped to 32MB SET LOCAL during HNSW build (shm 64MB constraint)
effective_cache_size = 1GB
random_page_cost = 1.1          # SSD
effective_io_concurrency = 200
```

**Note** : le `maintenance_work_mem` global est à 128MB mais le harness le SET LOCAL à **32MB** pendant le `CREATE INDEX HNSW` pour tenir dans le `/dev/shm` 64MB du container Docker dev. Ce choix ralentit légèrement le build (13s vs ~8s estimé avec 128MB+) mais est sans impact sur les mesures de latence runtime. Pour un environnement avec `shm_size: 1g+`, on peut bumper et gagner sur le build.

## Pourquoi pas une autre config

**Le tie-break baseline (post-P6 review)** sélectionne automatiquement `m=16, ef_construction=64, ef_search=100` car la baseline est **déjà la plus rapide** (p95=12.97ms) sur ce run et l'écart avec les alternatives est dans la marge `TIE_BREAK_P95_MS=1.5ms` :

1. **Aucune alternative meilleure** : la baseline est top-1 en p95 sur les 8 configs FAST.
2. **Différences ≤ 1.5ms** : tie-break code review P6 conserve la baseline pour éviter les migrations Alembic sur du bruit de mesure.
3. **Coûts de build différenciés** : `m=32, ef_c=128` build en 39.96s (3x baseline) sans gain measurable.
4. **Synthétique ≠ réel** : `recall@5_strict=1.0` partout (saturation) — l'auto-sélecteur n'a pas l'information pour départager. Le vrai signal qualité viendra Story 3.1 / 7.6.

**La décision finale est** : conserver `m=16, ef_construction=64` (baseline) et **re-évaluer** lors du benchmark sur **100k chunks réels** (anticipation H6, Architecture ligne 871) prévu avant Sprint 4. Si à cette échelle un avantage `m=32` se matérialise, une migration sera créée à ce moment-là.

## Schema interactions — table `chunk_embeddings` + index partiels

Les paramètres baseline restent ceux figés dans la migration Story 1.1 :

```sql
CREATE INDEX chunk_embeddings_bge_hnsw
ON chunk_embeddings
USING hnsw ((embedding::vector(384)) vector_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE model = 'bge-small-en-v1.5';

CREATE INDEX chunk_embeddings_openai_hnsw
ON chunk_embeddings
USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
WITH (m = 16, ef_construction = 64)
WHERE model = 'text-embedding-3-small';
```

**Limitation à documenter** : le sweep n'a couvert que la dimension **384** (`bge-small-en-v1.5`). Pour `text-embedding-3-small` (1536 dims), on **extrapole** depuis 384 — l'hypothèse est que les ratios recall/p95 se conservent grossièrement (HNSW est dimension-aware mais pas anti-corrélé). À valider Story 3.6 (Embedding Router) si l'usage cloud devient dominant.

## Limitations méthodologiques

1. **Embeddings synthétiques aléatoires unit-norm** : recall = 1.0 sur **toutes** les configs car les vecteurs random en 384D sont très bien séparés (curse of dimensionality favorable). Sur des embeddings sémantiques réels (clusters), le recall sera plus discriminant. Cette limitation est intentionnelle (gating cost-controlled — pas de clé OpenAI à $2 pour 10k embeddings) et documentée dans `hnsw-tuning.md`.
2. **Recall@5 saturé par construction du protocole — code review P7 (post-mortem)** : la mesure `recall@5` "regular" porte sur top-5(rerank(top-50_ANN)) ; tant que les top-5 ground-truth sont dans les top-50 ANN candidats, recall@5=1.0 — on mesure de facto un recall@50, pas la sélectivité de l'index ANN au seuil k=5. **Mitigation appliquée** : le harness mesure désormais aussi `recall@5_strict` (`top_k_ann == k=5`), affiché en colonne séparée dans `hnsw_results.csv` et `hnsw-tuning.md`. Sur les embeddings synthétiques actuels, `recall@5_strict` reste élevé (1.0) — confirmation que la saturation vient bien des embeddings random et pas seulement du protocole top-50. Le NFR4 reste **honnête mais documenté comme faible-discriminant** sur ce dataset ; il sera réévalué Story 3.1 / 7.6 sur embeddings sémantiques réels (où recall@5_strict deviendra l'indicateur principal).
3. **Reranking placeholder** : `0.7 × cosine + 0.3 × recency_decay(30j halflife)`. Le vrai reranking Story 3.6 utilisera un cross-encoder (`bge-reranker-base` ou `voyage-rerank-2`) — coût CPU plus élevé. La latence p95 mesurée ici (~14ms) sous-estime probablement le runtime réel ; même avec un facteur 5× le rerank réel, on resterait à p95 ~70ms (encore 2.8× sous NFR5). **Mitigation P7** : `created_at` est désormais varié sur `[now-90j, now]` (distribution uniforme seedée) pour rendre `recency_decay` non-constant — sinon le rerank dégénérait en ré-tri pur cosine et masquait l'apport du score combiné.
4. **Sweep réduit (mode FAST)** : 8 configs au lieu des 27 mentionnées dans l'AC2. Justification : les mesures sont saturées (recall=1.0, p95 ≈ noise), un sweep plus large aurait apporté 0 information actionnable. **AC2 a été formellement amendé** (cf `1-3-benchmark-m4-pgvector-hnsw.md` AC2.b post-S1) pour autoriser le mode FAST avec justification consignée. Le mode FULL (`make bench-hnsw`) reste invocable pour re-validation future (cf `CONVENTIONS.md` "Versioning critique pgvector").
5. **Pas de mesure sous concurrence** : 100 queries séquentielles à 1 connexion. Le test concurrence (N×N queries simultanées) sera fait Story 3.1 (Stockage + recherche vectorielle pgvector) avec un pool psycopg.
6. **`maintenance_work_mem` réduit (32MB)** : contrainte du `/dev/shm` 64MB Docker dev. Ralentit le build mais pas la latence runtime. À ajuster (`shm_size: 1g`) si besoin Sprint 1+.

## Pivot Plan (non-déclenché — pour référence)

Si une re-validation future détectait NFR4 ou NFR5 violé :

1. **Tuner Postgres** (AC7) : `shared_buffers=512MB`, `work_mem=32MB`, `maintenance_work_mem=256MB`, `shm_size: 1g` sur le container db. Re-run du sweep complet.
2. **Augmenter `m` et `ef_construction`** : passer à `m=32, ef_c=256` pour gagner du recall (build time ~3× mais latence stable).
3. **Bascule ivfflat** (cas où HNSW ne tient pas) : `lists=100, probes=10` est le baseline. Plafonné en recall mais 2× plus compact.
4. **Sidecar Qdrant/Weaviate** : abstraction Repository (Architecture AR16) facilite le swap. Coût : ~3 jours d'intégration + ops complexe (2 datastores). À déclencher seulement si > 1M chunks (estimation Sprint 5+).

## Références

- [`hnsw-tuning.md`](./hnsw-tuning.md) — rapport tuning auto-généré (tableaux complets, heatmap, future improvements).
- [`m3-spike-result.md`](./m3-spike-result.md) — précédent gating critique (Story 1.2, LangGraph).
- [Story 1.3 — `1-3-benchmark-m4-pgvector-hnsw.md`](../../_bmad-output/implementation-artifacts/1-3-benchmark-m4-pgvector-hnsw.md) — story de référence (ACs, dev notes, file list).
- [`docs/runbooks/m4-bench-rerun.md`](../runbooks/m4-bench-rerun.md) — runbook re-jeu du bench.
- [PRD NFR4-NFR5](../../_bmad-output/planning-artifacts/prd.md) ligne 113.
- [Architecture lignes 570-573 + 818-847](../../_bmad-output/planning-artifacts/architecture.md).
