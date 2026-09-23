# Rejeu d'un nœud et appels d'outils (Story 5.0)

## Quand l'utiliser

- Avant d'**assigner un outil à un agent** utilisé dans un workflow — pour décider si cet outil a le droit d'y être.
- Après un **crash, un timeout provider ou un `resume`** sur un run qui utilisait des outils, pour savoir ce qui a pu être réexécuté.
- Quand un outil **écrit** quelque part (fichier, ticket, message, appel d'API tiers) et qu'on constate un doublon.

## La propriété, nommée sans euphémisme

**Un nœud rejoué ré-appelle les outils qu'il avait déjà appelés.** Ce n'est pas un bug en attente de correctif : c'est une conséquence assumée de deux décisions antérieures, et la Story 5.0 la documente plutôt que de la masquer.

Deux chemins mènent au rejeu, et **aucun des deux n'est rare** :

1. **Le retry interne du nœud** (`error_policy.on_timeout = retry_with_backoff`, qui est le **défaut** — un template sans `error_policy` en hérite). La boucle d'outils vit **à l'intérieur** de la boucle de retry, pas autour : un échec provider au 5ᵉ tour rejoue l'interaction **complète** du nœud depuis son premier message, donc ré-appelle les outils des tours 1 à 4. C'est délibéré — rejouer seulement le dernier tour laisserait le modèle avec un historique dont il ne pourrait rien conclure.
2. **Le resume d'un run** (crash-resume par le worker de recovery, ou `POST /runs/{id}/control` avec `resume`). La reprise part du **dernier checkpoint LangGraph committé**, c'est-à-dire de la dernière frontière de **nœud** terminé. Un nœud interrompu **en milieu de boucle** n'a produit aucun checkpoint : il redémarre entièrement.

**Et un appel d'outil n'est pas nécessairement idempotent.** Rien dans MCP ne le garantit, rien dans le Tool Hub ne le déclare, et le protocole n'offre aucune clé d'idempotence. Un outil qui poste un message le postera deux fois.

### Ce qui borne les dégâts aujourd'hui

| Garde-fou | Ce qu'il borne | Ce qu'il ne borne PAS |
|---|---|---|
| `AGENTIVE_TOOL_LOOP_MAX_ITERATIONS` (8) | Le nombre de tours d'**une** exécution de nœud | Le nombre de fois que ce nœud est exécuté |
| `AGENTIVE_TOOL_LOOP_MAX_TOOL_CALLS` (24) | Les appels cumulés d'**une** exécution | Idem |
| `AGENTIVE_TOOL_LOOP_MAX_WALL_CLOCK_S` (180 s) | La durée totale d'**une** exécution de nœud, appels LLM ET appels d'outils | Idem — et c'est ce plafond, pas les deux du dessus, qui coupe en pratique |
| `error_policy.max_retries` (défaut 3, plafonné à `MAX_RUNTIME_RETRIES`) | Le nombre de rejeux internes | Les resumes, qui sont illimités |

Le pire cas d'appels d'outils pour **un** nœud est donc `max_tool_calls × (1 + max_retries)` par exécution, **multiplié par le nombre de resumes** — et un run peut être resumé arbitrairement souvent (cf. `service.py`, `_ACCOUNTED_NODE_STATUSES`).

## Prérequis / la contrainte à appliquer

**En Sprint 2, seuls des outils en LECTURE SEULE sont assignables aux agents d'un workflow.** C'est la seule mitigation qui tienne tant qu'il n'existe pas de clé d'idempotence : un outil sans effet de bord peut être rejoué sans conséquence, et la question de la déduplication ne se pose plus.

Concrètement, avant d'assigner un outil à un template d'agent :

1. Lire la description de l'outil **et** ce que fait réellement le serveur MCP qui l'expose. Une description ne fait pas foi.
2. Refuser l'assignation si l'outil peut : écrire un fichier, créer/modifier une ressource distante, envoyer un message, déclencher un job, consommer un quota facturé à l'appel, ou muter quoi que ce soit d'observable hors du processus.
3. En cas de doute, refuser. Le Playground reste disponible pour l'usage manuel : l'opérateur y voit chaque appel et ne subit ni retry automatique ni resume.

Cette contrainte n'est **pas** appliquée par le code de façon générale — il n'existe aucun champ « read-only » sur `Tool`, et l'inventer sans que les serveurs MCP le déclarent produirait une garantie fausse. Elle reste procédurale pour un serveur tiers, et c'est ici qu'elle est écrite. Le rendre applicable en général est le travail d'une story ultérieure (allowlist par template, ou déclaration d'idempotence côté serveur).

> **Story 5.2 — elle cesse d'être procédurale pour le seul serveur du dépôt.** `dev-code-search`
> (`infra/mcp/servers/code_search.py`) est le premier serveur MCP de production, et il est en
> lecture seule **par construction** : aucun mode d'ouverture en écriture, aucun `subprocess`,
> aucune exécution, aucun outil générique — propriété gardée par un test structurel qui analyse
> l'AST du module. ⚠️ Cette garde est une **heuristique syntaxique**, pas une preuve : elle
> couvre les modes d'ouverture et une liste d'appels interdits, pas tout ce qu'un import
> pourrait faire. Elle vaut mieux qu'une relecture, elle ne la remplace pas entièrement.
>
> Les quatre outils du Code Researcher peuvent donc être rejoués **sans effet de bord sur le
> disque**. « Sans conséquence » serait trop dire, et la revue de la Story 5.2 l'a relevé : un
> rejeu re-spawne un sous-processus, relit des fichiers, et **ce qu'il lit repart dans un
> prompt facturé**. C'est la lecture seule qui est acquise, pas la gratuité.
>
> ⚠️ Ce qui n'est **pas** vérifié : que le rejeu d'un nœud ré-appelle bien ses outils (5.0 AC5).
> Aucun test d'intégration ne couvre ce chemin aujourd'hui. La propriété est plausible — elle
> découle de la façon dont `agent_node` reconstruit son état — mais elle est supposée, et la
> revue a corrigé la phrase qui prétendait le contraire.
> Cf [`dev-pole-code-search-server.md`](../decisions/dev-pole-code-search-server.md).

## Procédure — constater ce qui a été rejoué

Les métriques par nœud portent de quoi le reconstituer (Story 5.0 T6.3) :

```sql
SELECT
  key AS node_id,
  value->>'tool_calls'            AS appels_outils,
  value->>'tool_loop_iterations'  AS tours_de_boucle,
  value->'tool_names'             AS outils_appeles,
  value->>'tool_failures'         AS echecs,
  value->>'llm_attempts'          AS traversees_de_chaine
FROM workflow_runs,
     jsonb_each(metrics->'per_node')
WHERE id = '<run_id>'
ORDER BY key;
```

Comment lire le résultat :

- `llm_attempts > 1` — la chaîne **complète** de providers a été retraversée. Les outils de cette exécution ont été appelés autant de fois.
- `tool_calls` compte les appels de la **dernière** tentative retenue, pas la somme de toutes. Pour le nombre réel d'appels partis vers un serveur MCP, croiser avec les logs : `mcp.tool_executor_*` porte le nom de l'outil à chaque exécution.
- `tool_failures > 0` sur un nœud par ailleurs réussi — le modèle a abouti **malgré** un outil cassé. À investiguer même si le run est vert.
- Un même `node_id` présent dans plusieurs runs du même workflow : chaque resume est un run à part entière côté événements (`service.py`, ~ligne 916), donc sommer sur le `workflow_id` et non sur le `run_id`.

Pour un run resumé, la liste des nœuds qui n'ont **pas** été rejoués est celle des nœuds dont `node_statuses` vaut `success` ou `skipped` au moment du resume (`_already_executed_node_ids`). Tout le reste a redémarré depuis le début.

## Rollback

Il n'y a rien à annuler côté moteur : le rejeu est le comportement nominal. Le rollback porte sur l'**effet de bord** qu'un outil non idempotent a produit en double, et il est spécifique à cet outil — c'est précisément pourquoi la contrainte ci-dessus existe.

Pour empêcher un run de continuer à rejouer pendant l'investigation : `POST /runs/{id}/control` avec `cancel` (cf. [`run-control-et-fallback.md`](./run-control-et-fallback.md)). Un run `paused` ne rejoue rien tant qu'il n'est pas resumé.

## Vérifications

- `AGENTIVE_TOOL_LOOP_MAX_ITERATIONS`, `AGENTIVE_TOOL_LOOP_MAX_TOOL_CALLS` et `AGENTIVE_TOOL_LOOP_MAX_WALL_CLOCK_S` sont bien ceux attendus dans l'environnement visé — ce sont les seules bornes du coût d'un nœud, et elles sont lues **au démarrage** du processus : un changement exige un redémarrage.
- Le pire cas d'un nœud n'est PAS `max_tool_calls × tool_call_timeout_s` (24 × 30 s = 12 min) : ce produit n'est atteignable par personne, parce que `AGENTIVE_TOOL_LOOP_MAX_WALL_CLOCK_S` (180 s par défaut) coupe la boucle bien avant. C'est CE plafond-là, et lui seul, que `recovery.derive_stale_threshold_s` lit. Conséquence pratique : dès qu'un outil est lent, l'arrêt par horloge murale est le cas NORMAL, pas l'exception — un `tool_call_timeout_s` de 30 s laisse passer six appels lents au plus, pas vingt-quatre.
- La plage utile de `AGENTIVE_TOOL_LOOP_MAX_WALL_CLOCK_S` est **120–200 s**, et les deux bornes sont calculées, pas choisies : en dessous de 120 s on tuerait un nœud sans aucun outil qui bascule d'un provider à l'autre (la valeur est refusée au démarrage par `ge=120.0` sur le champ `Settings` ET par `derive_stale_threshold_s`, qui reste l'autorité puisqu'elle seule connaît `NODE_TIMEOUT_S` et la longueur de chaîne) ; au-dessus de ~202,5 s la fenêtre de détection d'un run planté dépasse une heure à la pire combinaison légale des autres réglages. Chaque seconde de budget d'outils en plus coûte dix secondes de fenêtre de détection.
- Tout outil assigné à un agent de workflow a été passé au filtre lecture-seule ci-dessus. Rien ne le vérifie à votre place.

## Référence

- Boucle : `backend/src/agentive_backend/shared/llm/tool_loop.py`
- Exécuteur MCP (seul endroit qui appelle un outil, seul endroit qui enveloppe le résultat) : `backend/src/agentive_backend/infra/mcp/tool_executor.py`
- Rejeu interne du nœud : `backend/src/agentive_backend/features/workflow_engine/engine/agent_node.py`, `_complete_with_retry`
- Resume : `backend/src/agentive_backend/features/workflow_engine/service.py` et `recovery.py`
- Plafonds : `.env.example`, section « Boucle d'outils »
