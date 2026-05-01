# Event bus — convention de nommage

**Statut** : Accepté
**Date** : 2026-04-30
**Story** : 1.4 (Core event bus)
**Pattern obligatoire** : `module.entity.action` (3 segments, lowercase, snake_case)

---

## Règle

Tout `event_type` publié sur le bus DOIT respecter le regex :

```
^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$
```

`shared.event_bus.naming.validate_event_type` enforce cette règle au point de
publication ; un `event_type` mal formé est refusé avec
`InvalidEventTypeError` (RFC 7807, status 422).

## Exemples

| Event type | Statut | Raison |
|---|---|---|
| `system.app.started` | ✅ | 3 segments, prefix connu |
| `m3.workflow.started` | ✅ | 3 segments, prefix `m3` connu |
| `m4.chunk.indexed` | ✅ | 3 segments, prefix `m4` connu |
| `system.health.checked` | ✅ | underscore autorisé dans entity / action |
| `System.Started` | ❌ | majuscules |
| `m3.workflow` | ❌ | 2 segments |
| `m3.workflow.run.started` | ❌ | 4 segments |
| `_internal.x.y` | ❌ | leading underscore |

## Préfixes connus (warn-on-typo)

```
system, m1, m2, m3, m4, m5, m6, m7, m8, m9, m10, m11, m12
```

Un préfixe inconnu **ne lève PAS** d'exception (permissif pour faciliter les
expérimentations) mais émet un log `event_bus.unknown_module_prefix` — utile
pour repérer une typo (`m13` au lieu de `m3`) en relecture de PR.

## Pourquoi 3 segments

- **2 segments** confondent domaine et opération (`workflow.started` masque le
  module porteur).
- **4 segments** forcent des contorsions (`m3.workflow.run.started` vs
  `m3.workflow.started` — laquelle est canonique ?).
- **3 segments** matchent la convention CloudEvents `source/type` et permettent
  un routing pattern-based simple : `re.compile(r"^m3\..*$")` capture tous les
  events Workflow Engine sans listing exhaustif.

## Discovery (Trace Explorer M12)

Le naming canonique permet à Trace Explorer (Story 8.1) de scanner
`shared/contracts/events/*.py` et d'auto-générer la liste des
event_types par module sans registry runtime supplémentaire.

## Référence croisée

- Architecture, ligne 392 (Inter-module communication via event bus)
- `shared/event_bus/naming.py` — implémentation
- `shared/contracts/events/system_events.py` — exemples canoniques Sprint 0
