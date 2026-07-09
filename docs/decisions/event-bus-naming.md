# Event bus — convention de nommage

**Statut** : Accepté (amendé 2026-07-09 — renommage DDD, cf. [module-naming.md](module-naming.md))
**Date** : 2026-04-30
**Story** : 1.4 (Core event bus)
**Pattern obligatoire** : `module.entity.action` (3 segments, lowercase, snake_case)

---

## Règle

Le préfixe `module` **est le nom du package** sous `features/` (`agent_registry`,
`tool_hub`, `playground`, …) — un seul nom par contexte borné, partout
(amendement 2026-07-09 : les préfixes de planning `mN` sont supprimés).

Tout `event_type` publié sur le bus DOIT respecter le regex :

```
^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$
```

`shared.event_bus.naming.validate_event_type` enforce cette règle au point de
publication ; un `event_type` mal formé est refusé avec
`InvalidEventTypeError` (RFC 7807, status 422).

## Exemples

| Event type | Statut | Raison |
|---|---|---|
| `system.app.started` | ✅ | 3 segments, prefix connu |
| `workflow_engine.workflow.started` | ✅ | 3 segments, prefix = package `workflow_engine` |
| `memory_manager.chunk.indexed` | ✅ | 3 segments, prefix = package `memory_manager` |
| `system.health.checked` | ✅ | underscore autorisé dans chaque segment |
| `System.Started` | ❌ | majuscules |
| `workflow_engine.workflow` | ❌ | 2 segments |
| `workflow_engine.workflow.run.started` | ❌ | 4 segments |
| `_internal.x.y` | ❌ | leading underscore |

## Préfixes connus (warn-on-typo)

```
system, agent_configurator, agent_registry, chat, company_architect,
dashboard, memory_manager, playground, reporting, scheduler, tool_hub,
topology, trace, workflow_engine
```

Un préfixe inconnu **ne lève PAS** d'exception (permissif pour faciliter les
expérimentations) mais émet un log `event_bus.unknown_module_prefix` — utile
pour repérer une typo (`workflow_engin` au lieu de `workflow_engine`) en
relecture de PR. Ajouter le préfixe à `KNOWN_MODULE_PREFIXES` en même temps
que tout nouveau feature module.

## Pourquoi 3 segments

- **2 segments** confondent domaine et opération (`workflow.started` masque le
  module porteur).
- **4 segments** forcent des contorsions (`workflow_engine.workflow.run.started` vs
  `workflow_engine.workflow.started` — laquelle est canonique ?).
- **3 segments** matchent la convention CloudEvents `source/type` et permettent
  un routing pattern-based simple : `re.compile(r"^workflow_engine\..*$")` capture tous les
  events Workflow Engine sans listing exhaustif.

## Discovery (Trace Explorer M12)

Le naming canonique permet à Trace Explorer (Story 8.1) de scanner
`shared/contracts/events/*.py` et d'auto-générer la liste des
event_types par module sans registry runtime supplémentaire.

## Référence croisée

- Architecture, ligne 392 (Inter-module communication via event bus)
- `shared/event_bus/naming.py` — implémentation
- `shared/contracts/events/system_events.py` — exemples canoniques Sprint 0
