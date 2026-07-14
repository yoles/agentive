# Cœur de domaine Agent Registry — Value Objects & agrégat

**Statut** : Accepté (Palier 1 + agrégat `revise` câblés)
**Date** : 2026-07-14
**Contexte déclencheur** : audit technique 2026-07, Étape 4 (DDD) — « domaine
anémique », Vague 2 « Émergence du domaine ».

---

## Décision

Introduire une couche `domain/` **framework-free** dans le contexte borné
`agent_registry`, propriétaire des invariants métier qui vivaient jusqu'ici
éclatés entre les schémas Pydantic (validation au bord HTTP), le code
impératif des services, et l'ORM.

Palier 1 — Value Objects (`domain/value_objects.py`) :

- `Version` (monotone, `>= 1`, `.next()`),
- `Archetype` (`StrEnum`, les 8 slugs universels),
- `Contract` (forme élastique `{core, extras}`),
- `LLMParams` (bornes `temperature`/`max_tokens`),
- `ErrorPolicy` (`on_timeout`/`max_retries`/`backoff_strategy`),
- `ProviderChain` (ordonnée, dédupliquée — règle P-13),
- `AgentConfig` — VO composite sur le JSONB `agent_templates.config`, avec un
  **unique point de parse** (`from_mapping`) et un **unique point de
  sérialisation** (`to_mapping`).

Palier 2 — agrégat (`domain/aggregates.py`) :

- `AgentTemplate.revise(new_config)` encapsule la règle « un changement de
  `system_prompt` ⇒ bump de version + révision de prompt » (audit §4.6),
  ainsi que le garde P-01 (pas de bump si le prompt est byte-identique).

## Câblage

`AgentRegistryService.update_template` :

1. réhydrate l'agrégat depuis la row persistée, **construit inline à partir
   des attributs primitifs de l'ORM** (`id`, `name`, `archetype`, `version`,
   `config`) — aucun import de `infra.db.models` dans `features/`, donc le
   contrat de couches `app → features → infra → shared` reste trivialement
   respecté et l'exception `import-linter` de `shared.repositories` n'a pas
   besoin d'être étendue ;
2. applique le PATCH via `AgentConfig.merge_updates(...)` (les DTO Pydantic
   exposent désormais un `to_domain()` : `LLMParams`, `ContractDefinition`,
   `ErrorPolicy`) ;
3. délègue la conséquence de versioning à `AgentTemplate.revise(...)` ;
4. persiste `aggregate.config.to_mapping()` + l'éventuelle `PromptRevision`
   via les repos existants (signatures inchangées).

Le comportement observable est **strictement identique** à l'implémentation
impérative précédente (couvert par
`tests/unit/agent_registry/test_update_template_service.py`) ; la règle est
désormais unit-testable sans DB
(`tests/unit/agent_registry/domain/test_aggregates.py`).

## Motivation

1. **Un propriétaire unique de l'invariant.** La règle de bump de version
   n'est plus dispersée dans une méthode applicative de 130 LOC ; elle vit
   dans l'agrégat, testable en microsecondes sans Postgres ni mock de session.
2. **Fin du JSONB « deviné ».** Le `config` était re-désérialisé
   défensivement (`.get(key, default)`) avec des défauts dupliqués. `AgentConfig`
   est la source de vérité typée de la forme d'une config d'agent.
3. **Fenêtre préventive.** L'audit recommande d'introduire `AgentConfig`
   **avant** que plusieurs contextes ne lisent le `config` JSONB.

## Compromis assumés

- **Round-trip lossy sur clé inconnue.** `to_mapping()` n'émet que les clés
  connues du VO. Les seuls producteurs actuels de `config`
  (`ArchetypeDefinition.to_template_config()` et le chemin d'update) ne
  produisent jamais de clé hors de cet ensemble ⇒ round-trip lossless en
  pratique. Le `snapshot` immuable d'`AgentInstance` (qui doit figer les
  octets stockés verbatim) **ne** passe **délibérément pas** par ce VO.
- **Triple reflet du jeu de valeurs `Archetype`** (YAML/Pydantic, CHECK DB
  `ck_agent_template_archetype`, `StrEnum` domaine). Condition préexistante :
  la couche domaine ne peut pas dépendre de la machinerie Pydantic de
  chargement YAML. Noté, hors périmètre de ce slice.
- **Double validation `LLMParams`/`ErrorPolicy`** (Pydantic au bord HTTP +
  VO domaine). Ce n'est pas une violation DRY mais **deux frontières de
  confiance distinctes** : une config sourcée de la DB
  (`AgentConfig.from_mapping(row.config)`) n'a jamais traversé la validation
  Pydantic, donc le domaine re-valide.

## Périmètre reporté (Palier 2, slices ultérieurs)

Ces items de la Vague 2 restent à traiter et ne sont **pas** couverts ici :

- **A-11** — ports de persistance (`Protocol`) satisfaits structurellement
  par les repos concrets de `shared.repositories`.
- **A-10** — découpage de `AgentRegistryService` (template / instantiation /
  tool-assignment / archétypes).
- **A-09** — décomposition SRP des méthodes surdimensionnées + chemin d'audit
  unique.
- **A-07** — helper `require_by_id` / lookup-or-404 sur `BaseRepo`.
- **M-08** — segmentation ISP du `Protocol LLMProvider`.
- **A-13** — VO `RetentionPolicy` à cadrer **avant** l'implémentation de
  `memory_manager`.
- Un module `mapping.py` dédié (ORM ↔ domaine) si le câblage inline devient
  répété sur d'autres méthodes (`create_template`, `get_template_by_id`).

## Références

- `docs/audit-technique-agentive.md` — Étape 4, §4.4–4.7, §7.4 (Vague 2).
- `docs/decisions/repository-pattern.md` — exception `import-linter` de
  `shared.repositories`.
- `docs/decisions/module-naming.md` — suppression des préfixes `mN`.
