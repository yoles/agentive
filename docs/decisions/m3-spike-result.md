# ADR — Résultat du Spike M3 LangGraph (gating critique #1)

- **Statut** : ✅ **Accepté — GO Sprint 1**
- **Date** : 2026-04-25
- **Décideur** : John (Owner / Architect)
- **Story** : [`1-2-spike-m3-langgraph`](../../_bmad-output/implementation-artifacts/1-2-spike-m3-langgraph.md)
- **Référence risque** : PRD lignes 465-468 (Risque #1 CRITIQUE — M3 Workflow Engine), Architecture ligne 149 (AR3), Architecture lignes 220-241 (Sprint 0 Decomposition Story 0.2)

## Context

Le risque technique #1 du projet Agentive (cf PRD) est l'inadéquation potentielle de **LangGraph** comme abstraction du Workflow Engine M3. Si LangGraph ne supporte pas nativement les 3 capacités fondamentales (1) checkpointing persisté Postgres avec reprise post-crash, (2) scatter-gather (fan-out/fan-in d'agents parallèles), (3) human-in-the-loop natif, il faut **pivoter avant Sprint 1** — soit vers un framework alternatif (CrewAI / Temporal / Burr), soit vers une abstraction custom (asyncio + pickle Postgres).

La Story 1.2 implémente un spike isolé dans `backend/spike/` qui exerce les 3 piliers via un workflow minimal **2 agents (Producer + Reviewer) + 1 quality gate**. Ce document consigne le verdict du gating.

## Decision

**GO** sur les 3 piliers : LangGraph **1.1.8** + `langgraph-checkpoint-postgres` **3.0.5** supportent nativement et de manière satisfaisante les 3 capacités exigées. Le projet continue Sprint 1 sans modification structurelle. Les Stories Epic 4 (`4-1-creer-workflow` à `4-7-resumes-passage-automatiques`) peuvent démarrer **sans pivot d'abstraction**.

## Evidence

### 3 piliers × résultat

| Pilier | API utilisée | Test | Résultat |
|---|---|---|---|
| **(1) Checkpointing Postgres** | `AsyncPostgresSaver.from_conn_string(...)` + `await checkpointer.setup()` (3 tables auto : `checkpoints` / `checkpoint_writes` / `checkpoint_blobs`) | `test_m3_basic` + `test_m3_resume` | ✅ **OK** |
| **(2) Reprise post-SIGKILL** (NFR11) | `graph.ainvoke(None, {"configurable": {"thread_id": ...}})` sur thread existant ⇒ LangGraph reprend après le dernier checkpoint sans re-exécuter le producer | `test_m3_resume` (subprocess kill -9, vérifie `iterations == 1` après resume) | ✅ **OK** |
| **(3) Human-in-the-loop** | `interrupt(payload)` côté nœud + `Command(resume=value)` côté caller. Le retour de `interrupt` est la valeur passée à `Command(resume=...)`. Routing post-decision via `Command(goto="next_node", update={...})`. | `test_m3_hitl_approve` + `test_m3_hitl_reject` | ✅ **OK** |
| **(4) Scatter-gather** (bonus, préparation Epic 5) | Conditional edge depuis `START` retournant `[Send("worker", payload), ...]`. Reducer `Annotated[list[T], operator.add]` concatène les contributions parallèles. | `test_m3_scatter_gather` (3 summarizers, parallélisme ratio < 1.5x un seul) | ✅ **OK** |

### Métriques mesurées (poste dev local, MockLLM)

| Métrique | Valeur | Cible AC | Marge |
|---|---|---|---|
| Workflow basique (Producer → Gate → Reviewer) — durée test | **0.58s** | < 30s (AC1) | 50× sous le plafond |
| HITL approve — durée test | **0.56s** | < 30s | 50× sous le plafond |
| HITL reject + 2ème passage — durée test | **1.04s** | < 30s | 30× sous le plafond |
| Reprise post-SIGKILL — subprocess + reload + reprise | **4.25s** | < 30s | 7× sous le plafond |
| Scatter-gather (3 × 0.5s simulés) — durée totale | **0.52s** | < 1.5x duration unique = 0.75s | sous plafond ⇒ parallélisme effectif (~96% efficiency) |
| Suite complète `pytest tests/spike/` | **13.43s** | < 90s (AC8) | 6.7× sous le plafond CI |

### Versions exactes (lockfile `backend/uv.lock`)

| Package | Version |
|---|---|
| `langgraph` | **1.1.8** (pinné strict via `==`, AC7 / G3) |
| `langgraph-checkpoint-postgres` | **3.0.5** |
| `langgraph-checkpoint` (transitive) | 4.0.2 |
| `psycopg[binary]` | 3.3.3 |
| Python | 3.14.4 |
| Postgres + pgvector | `pgvector/pgvector:pg17` (image officielle) |

### Schema interactions — table `workflow_runs.checkpoint` (Story 1.1) vs tables LangGraph

`AsyncPostgresSaver.setup()` crée **ses propres tables** dans le schéma `public` :

- `checkpoints` (PK `(thread_id, checkpoint_ns, checkpoint_id)`) — header + metadata sérialisée
- `checkpoint_writes` — pending writes transients
- `checkpoint_blobs` — payloads binaires (msgpack interne)

La colonne `workflow_runs.checkpoint JSONB` créée en Story 1.1 **ne sert pas pour LangGraph** ; elle est destinée au futur `features/m3_workflow_engine/` (Epic 4) pour stocker un **résumé applicatif** du checkpoint (status workflow, dernier nœud exécuté, métriques cost/latency) — le checkpoint LangGraph reste source de vérité technique. Cette duplication est volontaire : observabilité applicative (M6 Dashboard, M12 Trace Explorer) lit `workflow_runs`, runtime lit les tables LangGraph. À documenter dans l'ADR Epic 4.

### Permissions DB

`AsyncPostgresSaver.setup()` requiert **CREATE TABLE** dans le schéma `public`. Le rôle `agentive_app` n'a pas ce droit (principe du moindre privilège — voir `infra/postgres/init.sql`). Le spike utilise donc `agentive_owner` (DSN `database_url_owner`) pour le `setup()`. **Pour Epic 4**, la stratégie sera : exécuter le `setup()` une seule fois en migration Alembic dédiée (rôle `agentive_owner`), puis utiliser `agentive_app` au runtime pour read/write avec des grants ciblés.

## Consequences

### Positives

- ✅ **Sprint 1 démarre sans pivot** — toutes les Stories Epic 4 (M3 Workflow Engine) sont implémentables avec LangGraph 1.1.8 sans abstraction custom.
- ✅ **Observabilité native** : LangGraph expose `aget_tuple()` et `astream(stream_mode="checkpoints")` ⇒ le runbook `docs/runbooks/m3-checkpoint-inspect.md` est immédiatement utilisable, et M12 Trace Explorer (Epic 8) pourra rebrancher dessus.
- ✅ **Scatter-gather préparé** : le pattern `Send` + reducer `operator.add` est validé pour Epic 5 (Dev Department — Dev Lead orchestre Architect/Producer/Reviewer en parallèle).
- ✅ **Régression bloquée** : le job CI `spike-m3` (`.github/workflows/ci.yml`) re-exécute les 5 tests à chaque push ⇒ toute upgrade de `langgraph` qui régresse l'un des 3 piliers est détectée immédiatement.

### Négatives / Risques résiduels (avec mitigations)

- ⚠️ **Couplage à la table interne LangGraph** : si LangGraph 2.x change le schéma des tables checkpointer, une migration sera nécessaire (politique G3 dans `CONVENTIONS.md` + Story 1.2 AC7 — chaque upgrade majeure relance le spike).
- ⚠️ **`setup()` requiert `agentive_owner`** : la stratégie Epic 4 doit explicitement séparer "setup migration-time" (owner) et "ops runtime" (app). Documenté ci-dessus, à reprendre dans la Story 4.2.
- ✅ **Race commit/SIGKILL résolue** (initialement palliée par `await asyncio.sleep(0.2)`) : le helper `_maybe_crash_after` poll désormais `aget_tuple` jusqu'à voir le sentinel field (`producer_output` / `approved`) committé en Postgres avant de déclencher le SIGKILL — déterministe, sans latency permanente côté production. Le spike ne contient plus aucun sleep arbitraire dans le path nominal du quality_gate.
- ⚠️ **MockLLM en CI** : la latency réelle Anthropic / OpenAI n'est pas testée ici. Le scénario `make spike-m3-real` (avec `ANTHROPIC_API_KEY` set) reste manuel — à automatiser en Sprint 1 dans un job CI optionnel (avec secret), Story 1.6.

### Sur les composants en aval

- **Epic 4 — M3 Workflow Engine** : Stories 4.1-4.7 démarrent en s'appuyant sur les patterns validés ici (`AsyncPostgresSaver`, `interrupt`, `Send`). La Story 4.1 référencera ce document comme source.
- **Epic 5 — Dev Department** : la Story 5.1 (Dev Lead orchestrateur) utilisera `Send` pour fan-outer Architect/Producer/Reviewer en parallèle.
- **Epic 6 — Chat Interface** : la Story 6.4 (Output Cards validation inline) consomme l'API `Command(resume=...)` — l'UI Chat enverra le `Command(resume={"approved": bool, "feedback": str})` quand l'utilisateur valide/rejette inline.
- **CI** : tous les pushes futurs incluent `spike-m3` dans la build summary ⇒ régression bloquée.

## Pivot Plan (NON APPLIQUÉ — conservé pour mémoire)

Si la prochaine upgrade de `langgraph` échoue les tests `spike-m3`, les options de pivot considérées étaient :

1. **Abstraction Python custom** sur `asyncio` + pickle/msgpack en Postgres (`workflow_runs.checkpoint JSONB` + table dédiée). Coût estimé : 5-7 jours, dette nouvelle.
2. **Framework alternatif** : Temporal (over-engineered pour solo dev, infra additionnelle), Burr (proche de LangGraph, peu adopté), CrewAI (pas de checkpointing natif → fallback abstraction custom de toute façon).
3. **Scope réduit** : abandonner scatter-gather natif, le composer manuellement via `asyncio.gather()` côté application. Conserver LangGraph pour le sequencing + HITL.

Ces options restent **documentées** ici en cas de pivot futur (par exemple si LangGraph 2.x casse l'API).

## Revisitabilité

Cette décision est **révisable** si :

- Une mise à jour majeure de LangGraph (1.x → 2.x ou un breaking change CHANGELOG) régresse un des 3 piliers ⇒ relancer le spike, mettre à jour ce document, ouvrir une session `bmad-correct-course` avant de continuer.
- Une mesure de latency en production sur LLM réel révèle un overhead LangGraph > 50ms par nœud (perception utilisateur dégradée NFR2). Investiguer alors si l'abstraction maison ne serait pas plus performante.
- Un besoin Epic 4+ ne s'exprime pas naturellement dans LangGraph (ex : DAGs très dynamiques, branchements conditionnels complexes non supportés par `add_conditional_edges`).

## References

- [Story 1.2 — spike implementation](../../_bmad-output/implementation-artifacts/1-2-spike-m3-langgraph.md)
- [Architecture — risque #1 + Sprint 0 Decomposition](../../_bmad-output/planning-artifacts/architecture.md) (lignes 149, 220-241, 2068-2072)
- [PRD — Risque technique #1 CRITIQUE](../../_bmad-output/planning-artifacts/prd.md) (lignes 465-468)
- [LangGraph official docs — checkpointing](https://docs.langchain.com/oss/python/langgraph/add-memory)
- [LangGraph official docs — interrupts (HITL)](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [LangGraph official docs — Send (map-reduce)](https://docs.langchain.com/oss/python/langgraph/use-graph-api)
- [Runbook — inspect spike checkpoint](../runbooks/m3-checkpoint-inspect.md)
