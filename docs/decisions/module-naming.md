# Nommage des feature modules — suppression des préfixes `mN`

**Statut** : Accepté
**Date** : 2026-07-09
**Contexte déclencheur** : revue DDD post-audit technique 2026-07

---

## Décision

Les 13 feature modules backend perdent leur préfixe de planning `mN_`
(`m2_agent_registry` → `agent_registry`). Le préfixe des `event_type`
publiés sur le bus devient **le nom du package** (`m2.agent_template.created`
→ `agent_registry.agent_template.created`) — règle : **un seul nom par
contexte borné, partout** (package, events, répertoires de tests, docs).

## Motivation

1. **Langage ubiquitaire (DDD)** — `m5` n'existe dans le vocabulaire d'aucun
   utilisateur ni développeur du domaine. Les numéros sont un artefact du PRD,
   c'est-à-dire du document le plus volatil du projet ; le code y était couplé.
2. **Le schéma avait déjà cassé** — deux modules `m7` (`m7_playground`,
   `m7_chat`) : la numérotation séquentielle ne survit pas aux splits.
3. **Fuite dans le langage publié** — les préfixes `mN.` étaient persistés
   dans `outbox_events.event_type` et gravés dans les contrats
   (`shared/contracts/events/`). Chaque sprint augmentait le coût de
   correction ; le renommage a eu lieu pendant la fenêtre où les données
   étaient jetables (dev/staging, aucun consommateur externe).
4. **Asymétrie front/back** — le frontend utilisait déjà les noms propres
   (`frontend/src/features/agent_registry`, `tool_hub`, `playground`).

## Table de correspondance (traçabilité PRD)

| PRD | Package | Préfixe event |
|---|---|---|
| M1 | `company_architect` | `company_architect.` |
| M2 | `agent_registry` | `agent_registry.` |
| M3 | `workflow_engine` | `workflow_engine.` |
| M4 | `memory_manager` | `memory_manager.` |
| M5 | `tool_hub` | `tool_hub.` |
| M6 | `dashboard` | `dashboard.` |
| M7a | `playground` | `playground.` |
| M7b | `chat` | `chat.` |
| M8 | `agent_configurator` | `agent_configurator.` |
| M9 | `topology` | `topology.` |
| M10 | `reporting` | `reporting.` |
| M11 | `scheduler` | `scheduler.` |
| M12 | `trace` | `trace.` |

Événement resegmenté au passage : `m7.playground.run_completed` →
`playground.run.completed` (le module ne se répète plus dans l'entity).
`m3.llm.fallback_triggered` → `workflow_engine.llm.fallback_triggered`
(le fallback LLM reste rattaché au futur contexte workflow, cf. Story 4.6).

## Données & migration

- `outbox_events.event_type` : backfill par la migration Alembic
  `20260709_000001_rename_event_type_prefixes.py` (mapping explicite,
  downgrade inverse). `audit_events` ne stocke pas d'event_type.
- Labels Prometheus `event_type` : discontinuité des séries historiques
  assumée (environnement dev uniquement à la date de la décision).

## Hors périmètre (assumé)

- `backend/spike/m3_*.py`, `backend/tests/spike/test_m3_*.py`,
  `backend/scripts/benchmark_m4.py` : code de spike/bench jetable, sera
  réécrit dans `features/workflow_engine/` à l'Epic 4 (cf.
  `pyproject.toml` overrides mypy). Non renommé.
- Artefacts historiques (`_bmad-output/`, ADR de résultats de spike,
  audit) : les références `mN` y restent, ce sont des snapshots datés.

## ⚠️ Vigilance frontière `agent_configurator` / `agent_registry`

`agent_configurator` (ex-M8) est un stub. Si son implémentation doit
**écrire** sur l'agrégat `AgentTemplate` (configurer un agent ≈ modifier sa
config), c'est le **même contexte** que `agent_registry` : fusionner plutôt
qu'implémenter le stub — deux modules écrivant sur un même agrégat est une
fausse frontière de contexte (couplage caché garanti). À trancher AVANT
l'epic concerné.

## Référence croisée

- [event-bus-naming.md](event-bus-naming.md) — convention amendée
- `shared/event_bus/naming.py` — `KNOWN_MODULE_PREFIXES` + regex
- `.import-linter` Contrat 1 — énumération des modules (mise à jour)
