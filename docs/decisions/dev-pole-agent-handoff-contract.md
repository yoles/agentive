---
date: '2026-09-16'
story: '5.3 — Architect Analyst + Code Producer'
status: accepté
---

# Ce qu'un agent du Pôle Dev reçoit de son amont, et d'où viennent ses conventions

Deux arbitrages, tranchés ensemble parce qu'ils décident tous deux de **ce qu'un agent voit
avant de produire**. Le premier était nommément confié à cette story par la Story 5.2 ; le
second est apparu en cherchant à tenir l'AC2 à la lettre.

---

## 1. Régime de passage : sortie brute ou résumé de passage ?

### Le constat

La Story 4.7 a rendu les **résumés de passage** systématiques : avant de transmettre la sortie
d'un node au suivant, le moteur l'appelle une fois de plus pour la condenser en
`{decisions, artifacts_refs, blockers, next_questions}`. La raison est solide — sur un DAG
linéaire, la charge amont croît d'une sortie complète par étape, donc le coût total croît
**quadratiquement** avec le nombre de nodes.

Le levier d'exemption a été livré avec : `agent_node._build_user_message` lit
`config["include_raw_previous_output"]` et, quand il vaut le littéral `True`, transmet les
sorties brutes. **Mais aucun champ d'API ne permettait de l'écrire.** Les huit champs de
`UpdateTemplateRequest` (`system_prompt`, `input_contract`, `output_contract`, `llm_model`,
`llm_params`, `provider_chain`, `error_policy`, `push_memory`) ne le couvraient pas, et
`extra="forbid"` refusait tout le reste. Le catalogue du Pôle Dev passant par ce DTO
(`DevAgentDefinition.to_update_request`), **aucun YAML ne pouvait déclarer ce réglage**.

Tant que le pôle n'avait qu'un ou deux nodes, personne ne l'a senti. La Story 5.2 l'a nommé, l'a
documenté dans `code_researcher.yaml`, et a confié l'arbitrage à la Story 5.3 « qui ajoute des
nodes et sentira le sujet plus fort ». C'est le cas : sur quatre nodes, un contrat structuré
traverse **trois** condensations avant d'atteindre le dernier agent.

### Ce que la condensation coûte réellement ici

Le Code Producer doit citer, pour chaque modification qu'il propose, l'`id` de l'étape
d'approche qui la justifie (`code_diffs[].approach_ref`, AC3). Ces `id` vivent dans
`approach.steps[]` de l'Architect Analyst. **Un résumé de passage ne les préserve pas** : sa
forme est fixée par `DEFAULT_HANDOFF_SUMMARY_SYSTEM_PROMPT` et ne comporte aucun de ces champs.
Sous le régime de résumé, l'AC3 n'est donc pas « difficile à vérifier », elle est **impossible à
tenir**.

### Les options

| Option | Coût | Effet |
|---|---|---|
| **(a) Élargir le DTO** d'un champ optionnel typé | Six surfaces à toucher (cf ci-dessous) | Les contrats structurés arrivent intacts ; les résumés inutiles cessent d'être **produits** |
| (b) Garder les résumés, écrire les prompts pour la forme condensée | Aucun code | L'AC1 (« contrat structuré consommable par l'agent suivant ») devient fausse |
| (c) Contourner : re-sérialiser à la main, repasser par `task_input` | Une mécanique parallèle au moteur | Rien que (a) ne rende mieux, et une divergence de plus à maintenir |

### Décision : (a)

`UpdateTemplateRequest.include_raw_previous_output: StrictBool | None`.

**Pourquoi le champ est légitime malgré l'élargissement du contrat HTTP public.** L'argument qui
l'avait repoussé en 5.2 — « changer le contrat public pour un seul template » — ne tient plus
quand il s'agit de **trois** templates sur une chaîne de quatre nodes. Le champ est par ailleurs
**additif et optionnel** : aucun payload existant ne devient invalide.

**Pourquoi `StrictBool` et non `bool`.** En mode permissif, Pydantic coercerait `"true"` en
`True`. Or le moteur ne reconnaît **que** le littéral booléen : toute autre valeur est ignorée
(avec un log `workflow_engine.include_raw_previous_output_ignored`, ajouté par la revue du
2026-09-12 précisément parce que le silence rendait le défaut indiagnostiquable). Accepter une
chaîne à la frontière HTTP produirait un `True` en base pour un appelant et un silence pour le
suivant.

**Les six surfaces**, toutes touchées ensemble parce qu'une seule oubliée donne un défaut
silencieux :

1. `features/agent_registry/schemas.py` — le champ **et** la liste de `_at_least_one_field`, qui
   énumère ses champs à la main : un champ absent de ce tuple fait partir en `422` un payload
   qui ne porte que lui.
2. `features/agent_registry/domain/value_objects.py` — `AgentConfig.from_mapping`, `to_mapping`
   et `merge_updates`. Le test est `is not None`, jamais un test de vérité : `False` est
   signifiant (« remets-moi les résumés »), et le faire disparaître rendrait le réglage
   impossible à annuler.
3. `features/agent_registry/service.py` — le passage dans `merge_updates`.
4. `features/agent_registry/dev_catalog.py` — `DevAgentDefinition` et `to_update_request`.
5. `scripts/seed_dev.py` — `_OWNED_CONFIG_KEYS`. Une clé absente de ce tuple est écrite une fois
   puis **jamais comparée** : une divergence YAML ↔ base serait rapportée « inchangé ».
6. `frontend/src/features/agent_registry/schemas.ts` — le miroir Zod. Le laisser diverger est le
   défaut que la Story 5.1 avait nommément anticipé.

### Qui opte out, et qui reste sur les résumés

| Node | Régime | Raison |
|---|---|---|
| `dev_lead` | — (aucun amont) | |
| `code_researcher` | **résumés** | Son amont est un plan de délégation, dont il n'a besoin que de sa propre ligne. Son `system_prompt` est écrit POUR le résumé (Story 5.2), et le changer bumperait sa version — donc imposerait de rejouer le protocole de qualité manuel (`dev-lead-prompt-quality-protocol.md`) pour un gain nul |
| `architect_analyst` | **brut** | Consomme les quatre tableaux du Chercheur : les chemins de fichiers ne survivent pas à une condensation |
| `code_producer` | **brut** | Doit citer les `approach.steps[].id` de l'Analyste (AC3) |

**Effet mesuré, et il va dans le bon sens.**
`graph_builder._any_successor_reads_summaries` ne produit **aucun** résumé quand tous les
successeurs d'un node ont opté out. Deux des trois arêtes ne coûtent donc plus rien :

| | Nodes | Résumés | Total |
|---|---|---|---|
| Story 5.2 (2 nodes) | 2 | 1 | **3 appels LLM** |
| Story 5.3 (4 nodes) | 4 | 1 | **5 appels LLM** |

Sans l'opt-out, le DAG à quatre nodes aurait coûté **7** appels. L'exemption n'est donc pas
« payer plus cher pour avoir plus d'information » : c'est un échange dont les deux côtés sont
favorables ici.

### Ce que l'opt-out déplace, et qu'il faut surveiller

`MAX_UPSTREAM_OUTPUT_CHARS` (50 000 caractères, `agent_node`) devient la **seule** borne sur ce
que reçoit un node. Sa politique d'éviction retire **la plus grosse entrée d'abord**, et une
entrée seule qui dépasse est tronquée avec un marqueur `...[TRUNCATED]`. Sur ce DAG, la plus
grosse entrée est la sortie du Code Researcher — celle dont l'Analyste a le plus besoin. La marge
réelle se mesure sur un run, elle ne se suppose pas : cf `docs/runbooks/pole-dev.md` § 6 ter.

⚠️ À noter aussi : opter out fait passer **toutes** les entrées amont en brut, y compris celles
dont le résumé existe par ailleurs. Le Producteur voit donc le plan brut du Dev Lead, et non son
résumé — `_serialize_upstream` ne substitue les résumés que lorsque `prefer_handoffs` est vrai,
et le choix est global au node consommateur, pas par entrée.

---

## 2. D'où viennent les conventions du projet pour le Code Producer

### Le constat

L'AC2 de la Story 5.3 demande que le `system_prompt` du Code Producer « référence les conventions
projet via le namespace mémoire `métier` ». Trois faits, relevés dans le dépôt :

1. **`agent_node` n'applique pas Push Memory dans un workflow.** L'anti-scope est explicite dans
   sa docstring de module (« no Push Memory — those are Playground-only features », D91 point 7).
   Aucune lecture mémoire n'a lieu pendant un run.
2. **Rien n'écrit dans `dev-metier`.** Le seed le **crée** ; il ne le peuple pas. Aucun ingest,
   aucun chunk.
3. **Les fichiers de conventions sont hors de portée des outils.** `CONVENTIONS.md` et
   `.import-linter` vivent à la racine du dépôt ; le container ne monte que `./backend:/app`
   (`docker-compose.yml`) et `AGENTIVE_DEV_CODE_ROOTS` vaut `/app`. **`backend/` ne contient
   aucun fichier `.md`.** Seul `/app/pyproject.toml` est lisible, et il ne porte que les
   conventions d'outillage (`line-length = 100`, `mypy strict = true`, la configuration `ruff`).

La capacité que l'AC décrit **n'existe pas**. Reste à décider ce qu'on livre à la place, et
comment on le dit.

### Les options

- **(a) Porter les conventions dans le `system_prompt`**, distillées depuis `CONVENTIONS.md`.
  Aucune dépendance nouvelle ; une copie à maintenir.
- **(b) Élargir `AGENTIVE_DEV_CODE_ROOTS` à la racine du dépôt.** Met `.env`, `.git/` et `docs/`
  à portée d'un agent LLM. La deny-list du serveur devient alors la **seule** frontière — or sous
  le repli `setrlimit` (l'état de tout poste où bwrap est absent) il n'existe **aucune** isolation
  filesystem. C'est un arbitrage de sécurité : il se prend pour lui-même, pas en effet de bord
  d'une story produit.
- **(c) Monter les fichiers de conventions dans `/app`.** Modifie la topologie de déploiement
  pour un seul agent.

### Décision : (a), et le namespace est déclaré quand même

Les conventions vivent dans le `system_prompt` du Code Producer.

Le bloc `push_memory: {namespace: dev-metier}` **est déclaré**, avec son bloc `namespaces`
identique à celui du `dev_lead.yaml` (`catalog_namespaces` lève sur deux déclarations
divergentes du même nom). Ce n'est pas décoratif et ce n'est pas un mensonge : ce que la
déclaration produit **aujourd'hui** est le contrôle d'existence du namespace au lancement
(`mise_en_place._check_namespaces`, refus `422` si absent). Elle prépare la capacité, elle ne la
livre pas — et le YAML le dit en toutes lettres, comme le fait déjà `dev_lead.yaml`.

Le contre-modèle assumé est `code_researcher.yaml`, qui a **refusé** de déclarer `dev-metier`
« pour faire comme le Dev Lead ». Il avait raison pour lui : son AC ne nommait aucun namespace, et
déclarer aurait ajouté une condition de refus de run sans ajouter de capacité. Ici l'AC nomme le
namespace ; le choix n'est donc pas entre mentir et désobéir, mais entre déclarer en documentant
l'écart et ne rien dire.

**Ce qui reste ouvert, et son porteur.** La lecture mémoire dans un workflow — c'est-à-dire la
capacité que `push_memory` déclare sans la livrer — n'a aujourd'hui **aucune story porteuse**.
Cf `sprint-status.yaml`, clé `5-8-lecture-memoire-workflow`.

---

## Conséquences vérifiables

- Un run du DAG d'entrée coûte **5 appels LLM** (4 nodes + 1 résumé), et le test
  `test_the_producer_reads_the_analysts_raw_output_not_a_handoff_summary` épingle ce nombre.
- Le Code Producer reçoit les `approach.steps[].id` de l'Analyste dans son prompt — vérifié sur
  `provider.calls`, pas sur la configuration.
- `include_raw_previous_output` est comparé à chaque provisioning, donc une divergence entre le
  YAML et la base est **refusée**, pas rapportée « inchangé ».
- Les conventions que le Producteur doit suivre sont lisibles dans un diff git, comme l'allowlist
  de chemins l'est depuis la Story 5.2 — même raison : ce qui décide de ce qu'un agent produit ou
  peut lire se revoit comme du code.
