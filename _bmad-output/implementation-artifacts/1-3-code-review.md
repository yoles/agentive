# Code Review — Story 1.3 (Benchmark M4 pgvector HNSW)

**Date** : 2026-04-26
**Reviewers** : Blind Hunter (cynical, diff-only) · Edge Case Hunter (diff + projet) · Acceptance Auditor (diff + spec)
**Diff scope** : uncommitted changes (staged + unstaged + untracked) — 17 fichiers, ~2690 lignes
**Mode** : `full` (avec spec)
**Verdict global** : ⚠️ **fix-before-merge** (la décision GO sur NFR4/NFR5 reste valable, mais 8 patches sont prioritaires avant de marquer la story `done`)

---

## 🔴 PATCH — must-fix avant merge (8)

### P1 — CI : l'artifact `docs/decisions/hnsw-tuning.md` n'est pas régénéré
- **Source** : blind+edge+auditor (AC8 partial)
- **Fichier** : `.github/workflows/ci.yml:54-86`
- **Détail** : le job `bench-m4` invoque `python -m scripts._bench_report` directement, **pas** `make bench-report`. Du coup le `cp backend/_bench_artifacts/hnsw-tuning.md docs/decisions/hnsw-tuning.md` (Makefile:155-156) ne s'exécute jamais. Le bind-mount `-v "$PWD/docs:/app/../docs"` est inutilisé. Résultat : l'`actions/upload-artifact@v4` upload **la version committée**, pas une régénération fraîche, alors que AC8 dit explicitement "Upload `docs/decisions/hnsw-tuning.md` (regenerated)".
- **Fix** : soit invoquer `make bench-report` dans CI (avec son `cp` Makefile), soit retirer `docs/decisions/*.md` des `path:` d'upload et n'envoyer que `_bench_artifacts/*.csv` comme source de vérité.

### P2 — `_bench_report.py` exit code 0 quand la CSV est absente
- **Source** : edge
- **Fichier** : `backend/scripts/_bench_report.py:1942-1947`
- **Détail** : le commentaire promet "Quick non-zero exit code", mais `sys.exit(1)` est absent → CI passe au vert même quand la pipeline est cassée.
- **Fix** : ajouter `sys.exit(1)` après le `print(...)` d'erreur.

### P3 — Sweep CSV écrite uniquement à la fin → toute crash mid-sweep perd les mesures
- **Source** : edge
- **Fichier** : `backend/scripts/benchmark_hnsw.py:444-461` (et `benchmark_ivfflat.py` même pattern)
- **Détail** : 27 builds × ~1 min, écriture CSV **après** la dernière. Build #20/27 OOM (`maintenance_work_mem=32MB` avec `m=32, ef_c=256`) → 19 mesures réussies perdues, l'utilisateur relance 25 min de sweep.
- **Fix** : ouvrir la CSV en mode append, écrire l'en-tête une fois, flusher après chaque ligne.

### P4 — Cache ground truth non validé (clé incomplète)
- **Source** : blind+edge
- **Fichier** : `backend/scripts/_bench_common.py:1523-1539`
- **Détail** : la clé de cache est `gt_{n_chunks}_{dim}_{seed_corpus}_{seed_queries_validation}.json`. Elle **omet** `model`, `top_k`, et `n_validation_queries`. Si l'un de ces paramètres change :
  - Cas `n_validation_queries: 50 → 100` : cache contient 50 lignes, le bench itère sur 100 → `IndexError` à `truth[qi]` au query 51.
  - Cas `model: bge → openai` : truth de BGE-384 silencieusement consommé contre des embeddings 1536d → recall corrompu sans crash.
- **Fix** : soit inclure tous les paramètres pertinents dans la clé, soit valider les métadonnées du cache (longueur de chaque liste, model) avant utilisation.

### P5 — Index lifecycle : la sweep laisse l'index `chunk_embeddings_bge_hnsw` (production) dans l'état du dernier sweep ; ivfflat le drop sans restaurer
- **Source** : blind
- **Fichier** : `backend/scripts/benchmark_hnsw.py:410+`, `benchmark_ivfflat.py:2095-2098`
- **Détail** : le nom d'index `chunk_embeddings_bge_hnsw` est partagé avec la migration de production (Story 1.1). Après `make bench-hnsw` ou `make bench-ivfflat`, l'index reste avec les paramètres du **dernier** triplet sweepé (ou est purement supprimé après ivfflat). Tout démarrage d'Epic 3 sur ce DB dev hit un index non-baseline ou un seq scan.
- **Fix** : à la fin de chaque sweep, drop+recreate avec les params baseline (`m=16, ef_construction=64`). Documenter dans `docs/runbooks/m4-bench-rerun.md`.

### P6 — Auto-rapport `hnsw-tuning.md` contradit l'ADR `m4-bench-result.md`
- **Source** : auditor
- **Fichier** : `backend/scripts/_bench_report.py:1839-1848`, `docs/decisions/hnsw-tuning.md:55-59`
- **Détail** : `select_best_config` choisit le minimum de p95 → recommande `(m=32, ef_c=64)` et écrit "une migration Alembic doit être créée". L'ADR retient `(m=16, ef_c=64)` baseline parce que les écarts p95 sont dans le bruit (~1ms). Les deux artefacts livrés sont contradictoires.
- **Fix** : ajouter une logique tie-break dans `select_best_config` (ex : si `p95_diff < 1.5ms`, préférer le triplet baseline). Sinon, post-éditer l'auto-rapport via une note d'override.

### P7 — Méthodologie recall : la mesure recall@5 sature à 1.0 par design
- **Source** : blind+auditor (4 critical methodology)
- **Fichier** : `backend/scripts/_bench_common.py:1605-1633`, `benchmark_hnsw.py:344-385`
- **Détail** : double effet qui rend la mesure **non discriminante** :
  1. **Top_k_ann=50 ≫ k=5** : la rerank picks top-5 parmi 50 candidats ANN. Tant que les top-5 ground truth sont dans les top-50 ANN, recall@5 = 1.0. On mesure de facto `recall@50 ≥ 100%`, pas la sélectivité de l'ANN au seuil k=5.
  2. **Rerank dégénéré** : tous les chunks ont `created_at = now` → `recency_decay = 1.0` constant → le score combiné `0.7·cosine + 0.3·1.0` préserve l'ordre cosine. Le rerank ne contribue **aucun signal discriminant**, et l'ordre top-5 du rerank = ordre top-5 brute-force.
- **Conséquence** : le verdict GO sur **NFR4 (recall > 90%)** repose sur une mesure essentiellement vide. NFR5 (p95) reste robuste.
- **Fix** : ajouter une mesure "strict recall" avec `top_k_ann == k` (ANN top-5 vs ground top-5) ; faire varier `created_at` sur le corpus pour rendre le rerank non-dégénéré ; resserrer la limitation dans la section Evidence de l'ADR.

### P8 — `_bench_aggregate.py` / `comparison_hnsw_vs_ivfflat.csv` jamais livrés (AC3 partiel)
- **Source** : auditor
- **Fichier** : devrait exister à `backend/scripts/_bench_aggregate.py`
- **Détail** : AC3 + T4.3 ("CSV unifié `indexer, params, recall_at_5, latency_p95_ms, build_time_s, index_size_mb`") sont marqués `[x]` mais le fichier n'existe pas. L'agrégation est implicite dans `_bench_report.py` (rendu Markdown) mais la CSV unifiée n'est pas produite.
- **Fix** : créer `_bench_aggregate.py` qui lit `hnsw_results.csv` + `ivfflat_results.csv` et émet `comparison_hnsw_vs_ivfflat.csv` avec colonnes communes.

---

## 🟡 PATCH — should-fix (10)

| # | Titre | Fichier | Sévérité |
|---|---|---|---|
| M1 | `recency_decay` futur → boost max ; test enshrines le bug (nom dit "clamped to zero", assert `==1.0`) | `_bench_common.py:1559-1562` + `tests/scripts/test_bench_common.py:2309-2314` | medium |
| M2 | `maintenance_work_mem` interpolé en f-string (vecteur d'injection latent) | `_bench_common.py:1422-1449` | medium |
| M3 | Strict `> 0.90` exclut `recall=0.9000` exactement (incohérent avec NFR4) | `benchmark_hnsw.py:466`, `_bench_report.py:1797` | medium |
| M4 | Healthcheck CI loop ne fail-fast pas → erreur trompeuse côté alembic | `.github/workflows/ci.yml:58-63` | medium |
| M5 | `pg_relation_size_mb` race si l'index est droppé pendant le sweep → exception non gérée | `_bench_common.py:1476-1483` | medium |
| M6 | Pas de warmup avant la mesure de latence → mélange cold/warm dans le p95 | `_bench_common.py:1605-1633` | medium |
| M7 | `bulk_insert_chunks` `seed=42` par défaut découplé de `cfg.seed_corpus` (régression silencieuse possible) | `_bench_common.py:1352-1371` | medium |
| M8 | CSV malformée → `KeyError`/`ValueError` dans `_bench_report.py` | `_bench_report.py:1723, 1797` | medium |
| M9 | `pg_relation_size` mesuré sans `VACUUM ANALYZE` préalable (T3.3 dit "après VACUUM ANALYZE") | `_bench_common.py:226-253` | low/medium |
| M10 | `if-no-files-found: warn` masque les échecs CSV upload | `.github/workflows/ci.yml:78-85` | medium |

## 🟢 PATCH — nice-to-have (8)

| # | Titre | Fichier |
|---|---|---|
| L1 | `chunked()` dead code | `_bench_common.py:1662-1665` |
| L2 | `_safe_model_literal` → utiliser `psycopg.sql.Literal()` | `_bench_common.py:1412-1419` |
| L3 | `Makefile bench-hnsw-fast: up` n'attend pas la DB ready | `Makefile:144` |
| L4 | `make bench-report` cp sans vérifier la source | `Makefile:155-156` |
| L5 | `pct[95] < 200.0` strict ; doc dit "< 200ms" (`<= 200.0` plus tolérant) | `benchmark_m4.py:713` |
| L6 | `Path("_bench_artifacts").mkdir(exist_ok=True)` sans `parents=True` (incohérent vs `_bench_report.py`) | 4 fichiers |
| L7 | Pas de `timeout-minutes` sur le job CI bench (risque runaway) | `.github/workflows/ci.yml:38-86` |
| L8 | `compute_ground_truth` ne commit pas → transaction reste ouverte avec `enable_indexscan=OFF` | `_bench_common.py:1503-1520` |

---

## 🚫 BAD SPEC — la spec aurait dû éviter ça (1)

### S1 — AC2 ran 8 configs (FAST) au lieu des 27 prévus
- **Source** : blind+auditor
- **Détail** : AC2 dit explicitement "9 builds × 3 ef_search = 27 lignes". Le dev a tourné `BENCH_FAST=1` (8 configs) et a justifié post-hoc dans les Dev Notes ("saturation, mesures redondantes"). Le justification est **circulaire** : la saturation à recall=1.0 est elle-même un artefact (cf P7), donc "saturation → arrêt mesures → décision" tourne en rond.
- **Suggestion d'amendement** : soit (a) re-tourner le sweep FULL (~25 min) pour avoir les 27 lignes promises, soit (b) amender formellement AC2 dans `1-3-benchmark-m4-pgvector-hnsw.md` pour autoriser le mode FAST avec justification explicite acceptée par le PO. La self-relief dans Dev Notes ne suffit pas pour une story `gating critique`.

---

## 📋 DEFER — pré-existant ou hors scope actuel (8)

1. **`bulk_insert_chunks` 24MB en RAM** : OK à 10k, mais ne scale pas. À ré-évaluer Story 3.1 (M4 Memory Manager) si on doit indexer 100k+ chunks. (`_bench_common.py:1370-1396`)
2. **`get_or_create_bench_namespace` race** : bench est séquentiel par design, pas de runs parallèles. (`_bench_common.py:1301-1324`)
3. **`bulk_insert_chunks` GUC `app.tenant_id` interaction** : non déclenché aujourd'hui. À documenter dans le runbook M4. (`_bench_common.py:1383-1397`)
4. **`text-embedding-3-small` 1536d jamais benché** : AC6 court-circuité (pas de migration nécessaire). À traiter en Story 3.6 (Embedding Router). (`m4-bench-result.md`)
5. **`format_vector` `.6f` truncation** : interne consistant (ground truth + ANN utilisent même sérialisation). À reconsidérer si comparaison cross-protocol (binary) un jour. (`_bench_common.py:1291-1293`)
6. **`OUTPUT_PATH.write_text` non atomique** : edge case disque plein. (`_bench_report.py:1940`)
7. **`recency_decay` overflow / div par zéro** : aucun caller ne passe `halflife<=0` aujourd'hui. (`_bench_common.py:1559-1562`)
8. **Tests unitaires manquants pour boundaries (n=0, dim=0, percentiles vides)** : couverture suffisante pour un harness de bench. (`tests/scripts/test_bench_common.py`)

---

## 📊 Résumé

| Catégorie | Count |
|---|---|
| `intent_gap` | 0 |
| `bad_spec` | 1 |
| `patch` (must-fix P1-P8) | 8 |
| `patch` (should-fix M1-M10) | 10 |
| `patch` (nice-to-have L1-L8) | 8 |
| `defer` | 8 |
| `reject` (drop) | 6 |

**Total findings retenus** : 35
**Layer failures** : aucune (3/3 reviewers ont retourné un rapport complet)

---

## 🎯 Verdict gating

| NFR | Mesuré | Cible | Marge | Verdict méthodo |
|---|---|---|---|---|
| **NFR4 — recall@5** | 1.0 partout | > 0.90 | "infinie" mais **artificielle** (P7) | ⚠️ faible |
| **NFR5 — latency p95** | ~14 ms | < 200 ms | 13.8× sous le seuil | ✅ robuste |

**Pivot** : pas justifié. pgvector tient NFR5 avec une marge confortable.
**GO Sprint 1** : possible **après P1-P8** ; le verdict NFR4 est honnête mais documente sa limitation (P7).
