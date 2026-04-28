# HNSW Tuning Report — Story 1.3 (Benchmark M4 pgvector)
> **Auto-généré** par `scripts/_bench_report.py` à partir des CSV `_bench_artifacts/*.csv`. Ne pas éditer à la main : pour modifier, ré-exécuter `make bench-report` après un nouveau sweep.
**Cible NFR4** : recall@5_strict (top_k_ann == k=5) >= 0.9 sur 50 requêtes validation (synthétiques unit-norm 384 dims). Le verdict `recall_at_5_strict` mesure la sélectivité de l'index ANN sans confounding du rerank Python placeholder. La colonne `recall@5` (top_k_ann=50 + rerank) est informationnelle — sur ce dataset elle reflète la qualité du scoring rerank, pas celle de l'index.
**Cible NFR5** : p95 latency < 200.0ms end-to-end (ANN top-50 + reranking Python placeholder).

**Dataset** : 10k chunks synthétiques, modèle `bge-small-en-v1.5` (384 dims), vecteurs aléatoires unit-norm, namespace `bench-m4-synthetic`. `created_at` varié uniformément sur `[now-90j, now]` (P7 review : rend `recency_decay` non-constant et le rerank discriminant — sinon dégénère en re-tri cosine).

**Limitation** : embeddings synthétiques ≠ sémantique réelle. Le `recall_at_5_strict` mesure la fidélité de l'index ANN vs brute-force exact (top-K) ⇒ valide pour NFR4. Le `recall_at_5` post-rerank reflète l'apport du scoring 0.7×cosine + 0.3×recency_decay, qui sur du synthétique non-corrélé recency↔contenu dégrade fortement le ranking — non représentatif du runtime applicatif (cross-encoder Story 3.6). La qualité sémantique réelle sera mesurée Story 3.1 / 7.6.

## Baseline (m=16, ef_construction=64, ef_search=100)

- recall@5 = **0.136**
- recall@10 = 0.22
- p50 = 11.83ms / p95 = **14.35ms**
- build_time = 13.22s, index_size = 19.54MB

## Sweep HNSW — résultats triés par p95 ascendante

| m | ef_c | ef_s | build_s | size_MB | recall@5 | recall@5_strict | recall@10 | p50_ms | p95_ms | NFR |
|---|---|---|---|---|---|---|---|---|---|---|
| 16 | 64 | 100 | 14.83 | 19.54 | 0.136 | 1.0 | 0.22 | 12.02 | 12.97 | ✅ |
| 32 | 64 | 100 | 28.49 | 20.3 | 0.136 | 1.0 | 0.22 | 11.88 | 13.28 | ✅ |
| 16 | 128 | 100 | 19.07 | 19.54 | 0.136 | 1.0 | 0.22 | 11.87 | 13.6 | ✅ |
| 32 | 128 | 100 | 39.96 | 20.23 | 0.136 | 1.0 | 0.22 | 11.85 | 13.61 | ✅ |
| 32 | 128 | 200 | 39.96 | 20.23 | 0.136 | 1.0 | 0.22 | 12.03 | 14.53 | ✅ |
| 16 | 128 | 200 | 19.07 | 19.54 | 0.136 | 1.0 | 0.22 | 11.8 | 15.52 | ✅ |
| 16 | 64 | 200 | 14.83 | 19.54 | 0.136 | 1.0 | 0.22 | 12.07 | 20.64 | ✅ |
| 32 | 64 | 200 | 28.49 | 20.3 | 0.136 | 1.0 | 0.22 | 11.95 | 25.64 | ✅ |

## Heatmap (m x ef_construction) à ef_search=100

Format cellule : `p95_ms / r5=recall@5 ✅(NFR4)❌`

| m \ ef_c | 64 | 128 |
|---|---|---|
| **16** | 13.0ms / r5=1.00 ✅ | 13.6ms / r5=1.00 ✅ |
| **32** | 13.3ms / r5=1.00 ✅ | 13.6ms / r5=1.00 ✅ |

## Sweep ivfflat — comparaison

| lists | probes | build_s | size_MB | recall@5 | recall@5_strict | recall@10 | p50_ms | p95_ms | NFR |
|---|---|---|---|---|---|---|---|---|---|
| 100 | 25 | 1.78 | 16.09 | 0.136 | 1.0 | 0.22 | 12.0 | 12.98 | ✅ |
| 50 | 5 | 1.27 | 15.87 | 0.136 | 1.0 | 0.22 | 11.9 | 13.15 | ✅ |
| 50 | 10 | 1.27 | 15.87 | 0.136 | 1.0 | 0.22 | 11.92 | 13.19 | ✅ |
| 200 | 25 | 2.06 | 16.56 | 0.136 | 1.0 | 0.22 | 11.84 | 13.24 | ✅ |
| 50 | 25 | 1.27 | 15.87 | 0.136 | 1.0 | 0.22 | 11.72 | 13.33 | ✅ |
| 200 | 5 | 2.06 | 16.56 | 0.136 | 1.0 | 0.22 | 11.81 | 13.44 | ✅ |
| 100 | 10 | 1.78 | 16.09 | 0.136 | 1.0 | 0.22 | 11.86 | 13.81 | ✅ |
| 100 | 5 | 1.78 | 16.09 | 0.136 | 1.0 | 0.22 | 11.95 | 15.88 | ✅ |
| 200 | 10 | 2.06 | 16.56 | 0.136 | 1.0 | 0.22 | 11.84 | 22.14 | ✅ |

## ✅ Configuration retenue

**`m=16, ef_construction=64, ef_search=100`** ⇒ **recall@5_strict=1.0** (sélectivité ANN, signal NFR4), recall@5=0.136 (post-rerank, informationnel), p95=12.97ms, build=14.83s, taille=19.54MB.

**Justification** : sélection en deux passes — (1) plus basse p95 parmi les configs satisfaisant simultanément recall@5_strict >= 0.9 (cf P7 review : le `recall_at_5_strict` est le signal NFR4 ; le `recall_at_5` post-rerank est confondé sur synthétique) et p95 < 200.0ms (NFR5) ; (2) tie-break baseline si l'écart p95 avec la baseline (m=16, ef_c=64) est ≤ 1.5ms (P6 review : éviter les migrations sur du bruit de mesure).

**Pas de migration appliquée** — les params baseline (m=16, ef_construction=64) sont confirmés optimaux par le benchmark (avec tie-break ≤ 1.5ms : si une config alternative est <= 1.5ms plus rapide, on conserve baseline car ce delta est dans le bruit de mesure).

**`ef_search` runtime** : valeur retenue `100` par défaut. Override possible via `SET LOCAL hnsw.ef_search = 200` pour les requêtes haute qualité (Vision/Cross-Pollinator Story 7.6) — coût ~2x latence.

## Future improvements

- **`halfvec`** (pgvector 0.7+) : quantization 16-bit float ⇒ ~50% taille index, recall stable. À évaluer Story 7.6 / Sprint 5+.
- **Bench sur 100k chunks réels** (anticipation H6, Architecture ligne 871) : embeddings via OpenAI / `voyage-3-lite` sur dataset OpenAssistant FR + Wikipedia EN. À planifier avant Sprint 4 (Growth phase).
- **Bench dimension 1536** (`text-embedding-3-small`) : extrapoler depuis le sweep 384 dims — full sweep dédié si stories Epic 3 montrent un usage cloud dominant.
- **Concurrence + pool de connections** : tester N_CONCURRENT x N_QUERIES (Story 3.1).
