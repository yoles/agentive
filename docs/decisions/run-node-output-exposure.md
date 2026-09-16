# Où vit la sortie complète d'un node, et par où elle sort (Story 5.7)

**Status:** Accepted (Sprint 2)
**Date:** 2026-09-15
**Story:** 5.7 — Exposer la sortie d'un run à un opérateur (T1.2, T8.2)
**Amende:** [`dev-pole-code-search-server.md`](./dev-pole-code-search-server.md) (Story 5.2)

## Décision

1. **La sortie complète d'un node est LUE à la demande dans le checkpointer
   LangGraph** (`AsyncPostgresSaver`), jamais recopiée ailleurs. C'est
   l'option (b) de T1.2.
2. **Elle sort par une route dédiée**, `GET /api/v1/workflows/runs/{run_id}`
   et `GET /api/v1/workflows/runs/{run_id}/nodes/{node_id}/output`, **jamais
   par la frame SSE** — la décision de `_state_event` (« Deliberately NOT
   forwarded: `node_outputs_preview` ») n'est pas renversée.
3. **Elle est rendue comme une CHAÎNE** (le rendu JSON de la sortie), bornée
   par `AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS`, et **toute coupure est dite**
   (`truncated`, `total_chars`, `returned_chars`, `next_offset`).

## Le constat de départ

Il existe **deux** sources pour la sortie d'un node, et les confondre est
l'erreur que cette story devait éviter :

| Source | Ce qu'elle porte | Où |
|---|---|---|
| `workflow_runs.checkpoint["node_outputs_preview"]` | un **aperçu** tronqué à 500 caractères par node (`_CHECKPOINT_PREVIEW_MAX_CHARS`, `service.py::_preview`) | colonne JSONB applicative |
| Le checkpointer LangGraph | la sortie **entière**, dans `channel_values["node_outputs"]` | tables `checkpoints` / `checkpoint_blobs` / `checkpoint_writes` (`app/lifespan.py`, `AsyncPostgresSaver`) |

500 caractères ne sont pas une sortie : celle du Code Researcher porte
`relevant_files`, `dependencies_graph`, `existing_patterns` et `risk_areas`,
et dépasse ce plafond dès le premier run réel.

## Les trois options, et leur coût

### (a) Élargir le plafond du preview — **rejetée**

Simple d'apparence, et c'est son piège. `node_outputs_preview` vit dans
`workflow_runs.checkpoint`, une colonne **réécrite en entier à chaque
superstep** (`_sync_checkpoint`) et **relue à chaque reconnexion SSE** ainsi
qu'à chaque repoll de statut (`_STATUS_REPOLL_INTERVAL_S`, 15 s). Élargir le
plafond fait payer cette taille à des chemins qui n'ont aucun besoin de la
sortie — et un plafond qu'on relève parce qu'il gêne ne borne plus rien.

### (b) Lire le checkpointer LangGraph à la demande — **retenue**

- **Aucune duplication** : une sortie, une source de vérité. Le preview reste
  ce qu'il est, un aperçu de diagnostic, et garde son plafond de 500.

  ⚠️ **Les deux sources sont FUSIONNÉES par node, pas choisies globalement.**
  Un node dont la décision de routage a échoué a produit une sortie que seul
  l'aperçu porte (`RoutingDecisionFailedError`, IG3) : n'itérer que le canal
  d'état faisait taire cette sortie par la route de détail alors que la route
  par node la servait. Chaque entrée porte donc sa propre `source`.
- **Aucune nouvelle surface de rétention**, ce qui est décisif vis-à-vis de
  l'anti-scope de la story : la Story 4.10 purge DÉJÀ ces tables
  (`CheckpointRetentionWorker` → `AsyncPostgresSaver.adelete_thread`), et
  `workflow_runs.checkpoint_purged_at` horodate la purge. Persister la sortie
  ailleurs aurait créé une donnée que rien ne purge — c'est-à-dire rouvrir le
  sujet de la 4.10 sans le dire.
- **Le coût est payé par le lecteur, pas par le run** : la lecture n'arrive
  que quand un opérateur la demande, contrairement à (a) qui la fait payer à
  chaque frame.

Son coût réel, et il est assumé : **le routeur dépend d'une API qui n'est pas
la nôtre** — `AsyncPostgresSaver.aget(config)`, qui rend le `Checkpoint` et
dont on lit `channel_values["node_outputs"]`. C'est CETTE méthode qu'une
montée de version doit faire re-vérifier ; l'ADR en nommait une autre
(`aget_tuple`) à sa première rédaction, ce qui aurait fait surveiller la
mauvaise. Trois choses bornent le couplage :

- `langgraph` est **pinné strictement** (`==1.1.8`, cf `CONVENTIONS.md`), et
  toute montée de version déclenche déjà la re-exécution du spike 1.2 ;
- la dépendance est **injectée**, jamais importée au runtime par la feature :
  `app.state.workflow_checkpointer`, typé sous `TYPE_CHECKING` seulement.
  C'est exactement la posture que `retention.py` tient déjà (`_checkpointer`),
  et `langgraph` n'est pas dans les modules interdits du **Contrat 5** — ce
  sont les SDK de fournisseurs LLM qui le sont ;
- la lecture est **enfermée dans une seule fonction** (`_read_node_outputs`),
  qui rend un `dict` neutre. Si l'API bouge, un seul endroit bouge.

### (c) Persister la sortie complète dans une colonne ou une table — **rejetée**

C'est (a) avec une étape de plus. On écrirait deux fois la même donnée, dont
une copie que **rien ne purge** : la rétention de la Story 4.10 connaît
`workflow_runs` et les tables du checkpointer, pas une troisième table. La
story elle-même l'interdit en anti-scope (« élargir ce qui est persisté sans
elle serait rouvrir son sujet »).

## Ce que la réponse dit quand elle n'a rien à dire

Une source qui peut disparaître doit le dire, sinon « purgé » et « vide » se
lisent pareil. La réponse porte donc `node_outputs_source` :

**Deux questions, deux champs.** La première version n'en avait qu'un, et
c'est le défaut que la revue a trouvé : le code choisissait entre `preview` et
`unavailable` d'après la PRÉSENCE d'un aperçu en base — une propriété de la
row — et non d'après la cause. Un pool saturé sur un run qui avait un aperçu
se lisait donc « fil LangGraph purgé, rien n'est reprenable ».

`node_outputs_source` — **d'où vient ce que vous lisez**, et rien d'autre :

| Valeur | Ce que ça veut dire |
|---|---|
| `checkpointer` | la sortie a été lue dans le fil LangGraph ; `truncated` dit si elle a été coupée pour la réponse |
| `preview` | ce qui est rendu est **l'aperçu de 500 caractères** de la row |
| `none` | il n'y a rien à montrer — sur ce seul champ, ce n'est pas un diagnostic |

`checkpointer_reachable` — **la source faisant autorité a-t-elle pu être
consultée ?** `false` signifie qu'on ne sait rien de la sortie d'aucun node,
ce qui est un fait différent de « ce run n'a rien produit ». Les deux champs
sont orthogonaux : un repli peut être servi pendant que la source est en
panne, et le lecteur a le droit de le savoir.

`checkpoint_purged_at` est rendu à côté, **inconditionnellement sur les deux
routes** : c'est un fait sur le run, pas sur la réponse. Le conditionner d'un
côté seulement donnait deux sémantiques à la même colonne.

## Pourquoi une chaîne et pas du JSON imbriqué

La sortie d'un node est produite par un LLM : c'est une **entrée externe non
maîtrisée** (NFR9, règle d'or #9), et l'Epic 6 la rendra dans une UI. Deux
conséquences :

- elle est rendue **telle quelle, en chaîne** — rien n'est exécuté, rien n'est
  interprété, rien n'est re-parsé côté serveur pour en extraire des champs ;
- elle passe par `redact_secrets` **avant** d'être coupée. Avant, et non
  après : la rédaction change la longueur, donc la faire après rendrait les
  `next_offset` d'une page à l'autre incohérents.

Une chaîne est aussi ce qui rend la pagination **honnête** : un offset en
caractères est reprenable, là où re-couper un objet JSON imbriqué produirait
du JSON invalide — exactement le défaut que la revue de la 5.2 a corrigé dans
`read_file` (`dev_code_max_read_bytes`, « le modèle reçoit du JSON non
parsable, pas un contenu tronqué proprement »).

## Ce que la borne borne

`AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS` (défaut 8 000) s'applique **par node**, et
il compte des **caractères**, pas des octets — le rendu part en
`ensure_ascii=False`, donc jusqu'à 4 octets par point de code. Le pire cas de
la route de détail est `plafond × nodes × 4`, le nombre de nodes étant borné à
100 (`_MAX_NODES`, `schemas.py`) : jusqu'à **~3,2 Mo** au défaut, et **~80 Mo**
au plafond légal de 200 000. (~16 K caractères sur le DAG d'entrée du Pôle Dev,
qui porte 2 nodes.) La sortie entière d'un node s'atteint page par page sur la
route dédiée, `next_offset` après `next_offset`.

**Ce plafond ne borne pas le travail du serveur**, et il ne faut pas le croire :
la sortie entière de chaque node est rendue puis caviardée **avant** d'être
découpée, donc `limit=1` coûte autant que `limit=8000` et paginer un gros node
relit tout à chaque page. Il protège le client et le réseau. Borner la lecture
elle-même supposerait de plafonner ce que l'état LangGraph accepte de porter,
ce qui est un autre sujet — et pas celui de cette story.

Un `limit` demandé au-delà du plafond est **refusé en nommant le réglage**
(`422`), jamais rogné en silence — même posture que la garde
`AGENTIVE_ALLOW_MCP_REGISTRATION`.

## Conséquences

- `docs/runbooks/pole-dev.md` repasse en `curl` (§ 3, § 6 bis, § 8) et perd son
  encart « cet endpoint n'existe pas ».
- Le protocole de jugement du § 7 bis (AC3 de la Story 5.2) redevient
  exécutable en le suivant.
- La frame SSE **ne change pas**. Si un jour elle devait porter la sortie, ce
  serait un renversement explicite du commentaire « Deliberately NOT
  forwarded » de `_state_event`, pas un effet de bord.

> **Renvois par NOM, pas par numéro de ligne.** La première version de cet ADR
> citait `router.py:709` — une ligne que le diff de cette même story a
> déplacée en `:1113`, rendant le renvoi faux **au moment où il était écrit**.
> Les Dev Notes de la story avertissaient pourtant : la 5.2 portait trois
> renvois vers des fichiers inexistants. Un nom de symbole survit au diff qui
> l'entoure ; un numéro de ligne, non.
