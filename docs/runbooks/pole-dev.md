# Pôle Dev — provisionner, lancer une demande, lire la réponse

> Story 5.1 (FR43/FR44). **Sprint 2 : le Pôle Dev se pilote en client HTTP, pas dans une UI** —
> décision de John du 2026-09-14. Le Chat est l'Epic 6 (Sprint 3) ; quand il arrivera, il se
> branchera sur exactement les endpoints ci-dessous sans toucher aux agents.

## 1. Quand l'utiliser

- Première mise en service du pôle sur un environnement (dev, staging).
- Après un changement du catalogue (`features/agent_registry/templates/dev/*.yaml`).
- Pour lancer une demande au Dev Lead et suivre son run.

## 2. Prérequis

- Le stack tourne (`make up`) et les migrations sont appliquées (`make migrate`).
- Une clé LLM valide (`ANTHROPIC_API_KEY` ou `OPENAI_API_KEY`) — sinon la Mise en Place refuse
  le lancement sur son check `llm_providers_healthy`.
- `AGENTIVE_API_TOKEN` pour l'en-tête `Authorization`.

## 3. Provisionner

```bash
make seed-dev
```

> ⚠️ **La PREMIÈRE exécution enregistre un serveur MCP**, ce qui exige
> `AGENTIVE_ALLOW_MCP_REGISTRATION=true` :
>
> ```bash
> AGENTIVE_ALLOW_MCP_REGISTRATION=true make seed-dev
> ```
>
> Sans ce drapeau, le provisioning **refuse et nomme le réglage**. Les exécutions
> suivantes, qui ne font que vérifier l'existant, n'en ont **pas** besoin : la garde vit
> dans `ToolHubService.connect_server`, au point où le sous-processus est spawné. C'est
> ce qui la rend signifiante — un drapeau de sécurité qu'on demanderait d'activer à
> chaque re-provisioning ne serait plus qu'une case à cocher. Cf § « Le serveur de
> lecture de code » ci-dessous pour ce que ce oui engage.

Ce que ça fait, **dans cet ordre** :

1. **Les serveurs MCP** déclarés (`templates/dev/mcp-servers.yaml`), enregistrés via
   `ToolHubService.connect_server` — c'est la découverte qui peuple la table `tools`.
2. **Les namespaces mémoire** déclarés par le catalogue (`dev-metier`, type `metier`).
3. **Les agent-templates**, créés depuis leur archétype puis configurés.
4. **Les outils MCP** déclarés par chaque template, résolus par nom contre `tools`.
5. **Le workflow d'entrée** « Pôle Dev — prise de demande (v2 : dev_lead → code_researcher) ».

**Les serveurs d'abord, et c'est la même règle d'ordre une couche plus bas.** Un template
qui déclare un outil dont aucun serveur n'expose le nom fait échouer le provisioning.

> ⚠️ Cet ordre **réduit** la fenêtre d'état partiel, il ne la supprime pas — la première
> version de ce paragraphe disait « refuser avant d'écrire », ce qui était plus large que
> vrai. Provisionner un serveur EST une écriture (une row `tool_servers` + les rows
> `tools` de la découverte), et une faute de frappe dans un nom d'outil fait toujours
> échouer l'assignation **après** la création du template. En cas d'échec en cours de
> route, la sortie liste ce qui a déjà été écrit ; cf § 8 Rollback.

**L'ordre n'est pas cosmétique.** Un template dont le `push_memory.namespace` n'existe pas fait
**refuser** tout lancement de run par la Mise en Place (`memory_namespaces_accessible`,
Story 4.5 AC2 — échec permanent, donc `422`). Provisionner les templates d'abord livrerait des
agents configurés et incapables de démarrer.

**Idempotent**, sous une condition qu'il vaut mieux lire ici que découvrir : tant que le DAG
du workflow d'entrée et les UUID de templates ne changent pas, une seconde exécution ne crée
rien, ne duplique rien, et n'émet aucun event d'audit gratuit. La sortie liste
`créés` / `mis à jour` / `inchangés`. Si un homonyme du workflow d'entrée existe avec un DAG
différent (templates recréés, base restaurée), le provisioning **refuse avant de créer** et
nomme les rows concernées — il n'existe aucun chemin de suppression de workflow.

### Si le provisioning échoue sur un outil

```
Dev Lead déclare des outils introuvables : read_file. Ils doivent être exposés par un
serveur du catalogue du pôle (`templates/dev/mcp-servers.yaml`) — un outil homonyme
exposé par un serveur tiers ne compte pas. Enregistrer le serveur, puis rejouer le
provisioning.
```

C'est **volontairement bloquant**. Un template silencieusement dépourvu d'outils est
indiscernable d'un template sans outils — c'est exactement le mensonge par omission que
`tool_invocations=[]` a entretenu deux epics durant (D80, fermée par la Story 5.0).

Même refus si un nom est **ambigu**, c'est-à-dire exposé par plusieurs serveurs MCP : en choisir
un ferait dépendre l'assignation de l'ordre de retour de Postgres.

> **État en Sprint 2 : `tools: []` pour le Dev Lead, et c'est documenté plutôt que masqué.**
> Aucun serveur MCP n'expose le moteur de workflow, et il n'existe pas de chemin légal pour en
> écrire un aujourd'hui : un serveur **stdio** interne n'a pas de réseau
> (`SandboxProfile.unshare_net = True`, Story 2.6) donc pas d'accès Postgres, et la seule
> variante viable — **SSE** — contourne le sandbox et rouvre le défer **D63** (allowlist réseau
> SSE). Arbitrage à porter à John. Cf [`dev-pole-workflow-tooling.md`](../decisions/dev-pole-workflow-tooling.md).
>
> Le **Code Researcher**, lui, porte la première liste d'outils non vide du dépôt — quatre
> outils de lecture servis par un serveur **interne** écrit ici, pas par un serveur tiers.
> La phrase « leurs serveurs sont tiers » qui figurait à cet endroit était fausse : l'image
> backend n'a ni Node, ni `npx`, ni `ripgrep`. Dossier complet dans
> [`dev-pole-code-search-server.md`](../decisions/dev-pole-code-search-server.md).

## 3 bis. Le serveur de lecture de code (`dev-code-search`)

C'est le serveur MCP qui donne au Code Researcher sa capacité d'exploration. Il est **écrit
dans ce dépôt** (`infra/mcp/servers/code_search.py`), lancé en sous-processus stdio sandboxé,
et **en lecture seule par construction** : aucun mode d'ouverture en écriture, aucun
`subprocess`, aucune exécution. Un test structurel analyse son AST pour que cette propriété ne
puisse pas se perdre à la relecture.

### Ce qu'il expose

| Outil | Ce qu'il fait |
|---|---|
| `find_files(pattern, path?, max_results?)` | Localise des fichiers par motif glob (`**/router.py`). |
| `search_content(pattern, path?, glob?, max_results?)` | Le « grep/ripgrep » de l'AC1, en `re` pur. Rend chemin + n° de ligne + ligne. |
| `read_file(path, offset?, max_bytes?)` | Lecture bornée. Rend `next_offset` quand c'est tronqué. |
| `list_directory(path?)` | Liste un répertoire. |

### Ses racines, et ce que les élargir coûte

`AGENTIVE_DEV_CODE_ROOTS` (défaut `/app`, le volume `./backend`). Ce réglage gouverne **deux**
gardes, et les deux sont nécessaires :

1. **L'allowlist du serveur lui-même.** Chaque chemin reçu est résolu (`Path.resolve()`, qui
   **suit les symlinks**) puis vérifié comme descendant d'une racine. Un `..`, un chemin absolu
   hors racine et un lien vers `/etc` échouent tous les trois, par le même test.
2. **Les `--ro-bind` du sandbox bwrap.**

La première n'est pas redondante avec la seconde. Sous le repli `setrlimit` — l'état de tout
poste où bwrap ne fonctionne pas, **y compris l'image de dev de ce projet**, où le binaire
existe mais où le noyau refuse les user namespaces non privilégiés — il n'y a **aucune**
isolation filesystem. L'allowlist du serveur est alors la seule frontière qui existe. Un « ça
marche en local » ne dit donc rien de la production, et réciproquement.

**La politique vit dans `Settings`, jamais dans la base.** `apply_sandbox_policy`
(`infra/mcp/policy.py`) réécrit la **commande**, l'`argv` et l'`env` du serveur **à chaque
appel** depuis les réglages : un `UPDATE` sur `tool_servers.connection_config` qui y écrirait
`--root /`, ou qui remplacerait le binaire lancé, est sans effet. Elle est appliquée par
`infra/mcp/client.py` lui-même, au point de passage obligé de tout spawn — et non plus par
chacun de ses appelants, ce qui faisait reposer la garantie sur le fait qu'aucun n'oublie. C'est une divergence assumée vis-à-vis de la forme littérale du défer D62 (qui décrivait
une colonne `tools.sandbox_profile JSONB`) — une allowlist qui décide de ce qu'un agent LLM
peut lire doit être lisible dans un diff git.

**Élargir a un coût, et il faut le dire avant de le payer :**

- Tout ce qui est sous une racine ajoutée devient lisible par un agent LLM, et **ce qu'il lit
  part dans un prompt** facturé, puis dans la sortie qu'un humain lira.
- La deny-list interne voyage avec le serveur et continue de s'appliquer — `.env*`, `*.env`,
  `.git`, `.ssh`, `.aws`, `.gnupg`, `*.pem`, `*.key`, `*.crt`, `*.p12`, `id_*`, `*.sqlite*`,
  `secrets*`, `.netrc`, `.npmrc`, `.pypirc`, `credentials*`, **comparés sans tenir compte de
  la casse**. Mais elle filtre des NOMS, pas des contenus : un secret dans un `config.yaml`
  n'est protégé par rien.
- Le fait que `.env` soit aujourd'hui hors de portée tient à ce que le container ne monte que
  `./backend` — **un accident de configuration, pas une conception**.
- L'interpréteur Python du backend doit rester **sous une racine**, sinon le serveur ne démarre
  pas sous bwrap (il ne verrait pas son propre exécutable).

Les trois bornes réglables (`AGENTIVE_DEV_CODE_MAX_READ_BYTES`, `..._MAX_RESULTS`,
`..._MAX_DEPTH`) sont appliquées **à la source**, pas après coup. `..._MAX_DEPTH` borne le
**coût** du parcours et non le nombre de résultats — mais, comme les autres, sa coupure est
**annoncée**.

Quatre chemins peuvent rendre un résultat incomplet, et **les quatre le disent** (`truncated:
true` + une clé `note` qui n'existe que dans ce cas) : le plafond de résultats, le budget de
balayage (`MAX_FILES_SCANNED`), la profondeur, et ce qui a été sauté en route — fichier trop
volumineux ou répertoire illisible. Un résultat coupé en
silence fait croire au modèle qu'il a tout vu, et un Chercheur qui rend « je n'ai rien trouvé »
sur une troncature est pire qu'un Chercheur qui échoue.

### Diagnostiquer les deux échecs attendus

**(a) Le serveur ne démarre pas** → le lancement d'un run est refusé par la Mise en Place, sur
le check `mcp_tools_reachable`. C'est une bonne nouvelle : l'échec est au **lancement**, pas au
milieu d'un run.

```bash
curl -sS -X POST "http://localhost:8000/api/v1/workflows/$WF/runs"   -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json"   -d '{"input": {"objective": "x"}}' | jq '.detail, .context'
```

Rejouer la commande **réellement enregistrée** — et pas une invocation équivalente, parce que
c'est celle-là qui échoue. Elle est dans la row, et la politique la recalcule à chaque appel :

```bash
# 1. Ce que la base porte (ce qu'un opérateur voit), et ce que la politique impose.
docker compose exec -T db psql -qtAX -U postgres -d agentive \
  -c "SELECT connection_config FROM tool_servers WHERE name = 'dev-code-search';"
docker compose run --rm backend uv run python -c \
  "from agentive_backend.infra.mcp.policy import code_search_connection_config as c; print(c())"

# 2. La rejouer telle quelle (remplacer <command> et <args> par la sortie ci-dessus).
docker compose run --rm backend <command> <args>
# Attendu : le process reste en attente sur stdin (c'est un serveur JSON-RPC).
```

Ce que les messages disent :

- `racine autorisée inexistante` → `AGENTIVE_DEV_CODE_ROOTS` pointe à côté.
- `racine autorisée relative` / `trop large` / `en deny-list` → le réglage est refusé au
  démarrage du backend, pas ici : relire `AGENTIVE_DEV_CODE_ROOTS`.
- `No such file or directory` sur la commande → **le mode de panne le plus probable en
  pratique** : une base réutilisée sur une machine où le venv vit ailleurs porte une commande
  qui n'existe plus. Rejouer le provisioning sur une base neuve.

> ⚠️ Cette invocation tourne **hors sandbox**. Elle ne reproduit donc pas l'échec propre au
> repli `setrlimit`, où `RLIMIT_CPU` / `RLIMIT_AS` peuvent tuer le sous-processus au milieu
> d'un balayage : celui-là se voit en `tool_failures > 0` **avec** `tool_calls > 0`.

**(b) Un chemin est refusé au runtime** → le run ne meurt PAS. Le refus arrive au modèle comme
un résultat d'erreur exploitable (`isError`), avec son motif, et l'agent peut corriger son
chemin. On le lit dans les métriques du run :

```bash
docker compose exec -T db psql -qtAX -U postgres -d agentive \
  -c "SELECT metrics->'per_node'->'code_researcher' FROM workflow_runs WHERE id = '$RUN';" \
  | jq '{tool_calls, tool_names, tool_failures}'
```

`tool_failures > 0` avec `tool_calls > 0` = des refus, des timeouts, ou un sous-processus tué
par les limites du repli `setrlimit`. Que le run SURVIVE à un refus n'est pas une intention :
c'est épinglé par
`tests/integration/workflow_engine/test_dev_lead_e2e.py::test_a_refused_path_inside_a_run_does_not_kill_the_run`. Les motifs sont dans les
logs serveur (`mcp.tool_executor_tool_error`, `mcp.tool_executor_timeout`).

**`tool_calls == 0` est le symptôme à ne pas rater** : l'agent a répondu sans rien lire, donc
sa sortie est inventée. Vérifier alors que les outils lui sont bien assignés :

```bash
curl -sS "http://localhost:8000/api/v1/agents/templates/$TPL/tools" -H "Authorization: Bearer $TOKEN"
```

## 4. Lancer une demande

```bash
export TOKEN="$AGENTIVE_API_TOKEN"
export WF="<workflow_id rendu par make seed-dev>"
# ⚠️ Depuis la Story 5.2 le workflow d'entrée s'appelle
# « Pôle Dev — prise de demande (v2 : dev_lead → code_researcher) » et porte un
# NOUVEAU `workflow_id`. Le mono-node de la Story 5.1 reste en base, reste
# lançable, et son id reste valide — il exécute simplement le Dev Lead seul.
# Cf § 8 pour pourquoi le nom est versionné plutôt que réutilisé.

curl -sS -X POST "http://localhost:8000/api/v1/workflows/$WF/runs" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"input": {"objective": "Scaffold le module paiements pour Acme"}}'
```

> La clé est **`objective`** : c'est ce que déclare l'`input_contract.core` du Dev Lead, et c'est
> la clé que son `system_prompt` lui dit de lire. ⚠️ **Rien ne valide `input_contract` au
> runtime** — `task_input` est du JSON libre sérialisé tel quel dans le prompt. Une clé
> différente ne produit aucune erreur : juste un agent qui ne voit pas la demande.

Réponse `201`, immédiate (le run tourne en arrière-plan) :

```json
{
  "run_id": "...",
  "status": "running",
  "acknowledgement": {
    "message": "Compris. Je mobilise Dev Lead et Code Researcher. ETA ~2 min.",
    "agents": ["Dev Lead", "Code Researcher"],
    "eta_minutes": 2,
    "eta_source": "heuristic"
  },
  "mise_en_place": { "...": "..." }
}
```

### `eta_source` : `history` ou `heuristic`

- **`history`** — CHAQUE node du DAG avait au moins une durée mesurée sur un run passé
  (médiane de `metrics.per_node[*].duration_ms`, sur les runs `completed` uniquement).
- **`heuristic`** — au moins un node est retombé sur `AGENTIVE_ACK_DEFAULT_NODE_DURATION_S`.
  Trois situations y mènent : aucun historique, un node ajouté depuis, ou une lecture
  d'historique qui a **dépassé** `AGENTIVE_ACK_HISTORY_TIMEOUT_S`. Cette dernière se voit dans
  les logs (`workflow_engine.acknowledgement_history_unavailable`, avec le type d'erreur) : un
  basculement durable de `history` vers `heuristic` sur un workflow qui a de l'historique est le
  signal qu'il faut regarder la base, pas l'accusé.

La distinction est le point : un chiffre estimé ne doit pas se présenter comme mesuré. C'est ce
que la revue du Dry Run a imposé à ses propres estimations (`no_execution_history`,
`node_estimate_from_fallback`).

L'accusé est calculé **sans aucun appel LLM** — l'AC promet une première frame SSE en moins de
deux secondes, et un appel de modèle y mettrait la latence d'un provider et de sa chaîne de repli.

## 5. Suivre le run

```bash
curl -sS -N "http://localhost:8000/api/v1/workflows/runs/$RUN/events" \
  -H "Authorization: Bearer $TOKEN"
```

La **première** frame est un `state` envoyé à la connexion, et porte le même `acknowledgement`
que le `201` — y compris si le run a déjà progressé (rattrapage). Puis viennent
`step_completed`, et `completed` / `failed`.

Pilotage : `POST /api/v1/workflows/runs/$RUN/pause|resume|cancel|retract`
(cf [`run-control-et-fallback.md`](./run-control-et-fallback.md)).

## 6. Lire la décomposition

La sortie du Dev Lead est un objet JSON :

```json
{
  "status": "done",
  "summary": "…",
  "plan": [{"id": "s1", "title": "…", "rationale": "…"}],
  "delegations": [{"subtask_id": "s1", "target_role": "code_researcher",
                   "instruction": "…", "rationale": "…"}]
}
```

Les `target_role` appartiennent au **jeu fermé** des 8 rôles Dev
(`shared/contracts/dev_roles.py`) : `code_researcher`, `architect_analyst`, `code_producer`,
`code_reviewer`, `test_engineer`, `cicd_watcher`, `doc_writer`, `sprint_reporter`. Le prompt du
Dev Lead est **rendu depuis ce module** (jeton `${DEV_ROLES}`) : ajouter un rôle là-bas change le
prompt sans édition, et les deux ne peuvent pas diverger.

`validate_delegation_plan(...)` (même module) signale les incohérences : `status` invalide, rôle
hors du jeu fermé, délégation vers une sous-tâche inexistante, sous-tâche que personne ne prend
**ou que plusieurs prennent**, `failed` sans `blocking_question`, `done` sans aucune sous-tâche.
Elle **signale, ne corrige pas** : réécrire un plan incohérent serait décider à la place de
l'orchestrateur.

**Le moteur applique ce contrôle lui-même** (T6.3, tranchée en revue). Quand un node rend une
sortie parsable et que son `output_contract.core` déclare `plan` ET `delegations`, `agent_node`
passe cette sortie au validateur et écrit les incohérences dans
`metrics.per_node[<node_id>].contract_problems` — avec un log `workflow_engine.delegation_plan_incoherent`.

Trois précisions sur ce choix :

- **Le déclencheur est le CONTRAT déclaré, pas le nom de l'agent.** Ce n'est pas un point
  d'application inventé pour le Dev Lead : c'est le moteur qui vérifie ce qu'un template a déclaré
  produire. Un agent des Stories 5.2 → 5.6 déclarant le même contrat est vérifié sans une ligne de
  plus.
- **Marquer, pas refuser.** T6.3 permettait les deux ; tuer un run sur une maladresse de format
  serait disproportionné, d'autant que le cas *non parsable* est déjà traité ailleurs (repli
  `raw_output` + règle `no-parsable-output` à 0.9). Ce qui est traité ici est le cas
  « parsable mais incohérent ».
- **Le constat va dans `metrics`, pas dans `node_outputs`.** La sortie du node est la réponse de
  l'agent ; y injecter un diagnostic du moteur la ferait diverger de son propre contrat.

La clé `contract_problems` est **absente** quand il n'y a rien à dire : c'est sa présence qui est
le signal.

```bash
docker compose exec -T db psql -qtAX -U postgres -d agentive \
  -c "SELECT jsonb_path_query(metrics, '\$.per_node.*.contract_problems') FROM workflow_runs WHERE id = '$RUN';"
```

### 6 bis. Lire l'exploration du Code Researcher

> ⚠️ **`GET /api/v1/workflows/runs/{run_id}` N'EXISTE PAS.** Les seules routes de ce préfixe
> sont `/events` (SSE), `/pause`, `/resume`, `/cancel`, `/retract`, plus `/instances` côté
> `agent_registry` — et `node_outputs` est une clé d'**état LangGraph**, jamais projetée en HTTP
> (`workflow_engine/router.py` : « Deliberately NOT forwarded: `node_outputs_preview` »). Les
> recettes ci-dessous lisent donc `workflow_runs` **en base**, ce que font déjà les tests.
> Ouvrir une route qui projette `metrics` reste à faire ; tant qu'elle n'existe pas, c'est
> `psql` ou rien.

Le second node rend un objet JSON de cette forme (la sortie elle-même vit dans le checkpoint
LangGraph ; ce qui est interrogeable aujourd'hui est `workflow_runs.metrics` et le flux SSE) :

```json
{
  "status": "done",
  "summary": "…",
  "relevant_files": [{"path": "/app/src/…/router.py", "role": "…", "why": "…"}],
  "dependencies_graph": {"features/tool_hub": ["shared/repositories", "infra/mcp"]},
  "existing_patterns": [{"name": "router/service/schemas", "description": "…",
                         "examples": ["/app/src/…/service.py"]}],
  "risk_areas": [{"area": "…", "risk": "…", "evidence": "/app/…"}]
}
```

**Les quatre champs sont dans `output_contract.core`**, donc branchables par une edge (la
validation de la Story 4.1 refuse toute condition portant une variable absente du `core` de
l'émetteur) et consommables tels quels par l'Architect Analyst de la Story 5.3.

**La question à se poser en premier n'est pas « le plan est-il bon » mais « a-t-il lu ? »** :

```bash
docker compose exec -T db psql -qtAX -U postgres -d agentive \
  -c "SELECT metrics->'per_node'->'code_researcher' FROM workflow_runs WHERE id = '$RUN';" \
  | jq '{tool_calls, tool_names, tool_failures}'
```

`tool_calls: 0` avec des `relevant_files` non vides = **une sortie inventée**. Le prompt
l'interdit explicitement (« tu n'affirmes jamais l'existence d'un fichier que tu n'as pas vu
rendu par un outil »), mais un prompt n'est pas une garantie — ces trois compteurs le sont,
et ils sont la raison pour laquelle l'AC2 les exige.

Vérification du contraire — **elle compte ce qu'elle a vérifié**, parce qu'une sortie vide ne
doit pas se lire comme un succès :

```bash
docker compose exec -T db psql -qtAX -U postgres -d agentive \
  -c "SELECT jsonb_path_query(state_snapshot, '\$.**.relevant_files[*].path') FROM workflow_runs WHERE id = '$RUN';" \
  | tr -d '"' | sed '/^$/d' > /tmp/paths.txt
test -s /tmp/paths.txt || echo "AUCUN CHEMIN À VÉRIFIER — ce n'est PAS un succès"
n=0; bad=0
while read -r f; do
  n=$((n+1))
  # `< /dev/null` : sans lui, `docker compose exec` consomme stdin et la boucle
  # s'arrête après le PREMIER fichier en rapportant « tout existe ».
  docker compose exec -T backend test -f "$f" < /dev/null || { echo "INEXISTANT: $f"; bad=$((bad+1)); }
done < /tmp/paths.txt
echo "$n chemin(s) vérifié(s), $bad inexistant(s)"
```

> ⚠️ **Ce que le Code Researcher voit du Dev Lead n'est pas son plan complet.** `agent_node`
> condense la sortie de chaque node amont en un résumé de passage (Story 4.7) avant de la
> donner au suivant : ce qui arrive est `{decisions, artifacts_refs, blockers,
> next_questions}`, pas le tableau `delegations`. Un run du DAG d'entrée coûte de ce fait
> **trois** appels LLM, pas deux. Le levier existe (`config.include_raw_previous_output`,
> lu par `agent_node`) mais aucun champ de `UpdateTemplateRequest` ne permet de l'écrire, donc
> le catalogue ne peut pas le déclarer aujourd'hui — élargir ce DTO change le contrat HTTP
> public pour un seul template, et l'arbitrage revient à la **Story 5.3**, qui ajoute des
> nodes et sentira le sujet plus fort. Le prompt du Code Researcher est écrit pour cette
> réalité : il cherche ce qui le concerne dans le résumé, et se rabat sur `objective` sinon.

## 7. Juger la qualité du prompt (manuel)

Les tests E2E tournent sur `MockProvider` : ils prouvent que le contrat survit au moteur, **pas**
que le modèle produit une bonne décomposition. Ce jugement est manuel, sur les 3 cas de référence
de l'AC3, avec de vraies clés :

| Cas | Demande |
|---|---|
| (a) scaffolding | « Scaffold le module paiements pour Acme » |
| (b) refactoring | « Refactorer le module paiements pour extraire la logique de reprise » |
| (c) bug fix | « Le webhook paiement renvoie 500 depuis hier soir » |

Critères de lecture : 2 à 6 sous-tâches ; l'ordre explorer → analyser → produire → reviewer/tester
→ documenter est respecté ou son écart est justifié ; chaque rôle assigné est le plus proche du
besoin ; aucune sous-tâche orpheline.

Même posture que `make spike-m3-real` / `make spike-m3-mock` (Story 1.2) : la reproductibilité est
mockée, le jugement est réel.

### Résultat du dernier passage

| Date | Modèle | (a) scaffolding | (b) refactoring | (c) bug fix |
|---|---|---|---|---|
| — | — | non exécuté | non exécuté | non exécuté |

> ⚠️ **Ce protocole n'a encore jamais été exécuté avec de vraies clés.** La tâche T8.3 de la
> Story 5.1 demandait le protocole « **et ce qu'il a donné** » ; seule la première moitié est
> faite. Tant que cette table est vide, la qualité du prompt de décomposition est **non
> vérifiée** — les tests E2E prouvent que le contrat traverse le moteur sans déformation, pas
> que le modèle produit un bon plan. Remplir la table au premier passage réel.

## 7 bis. Juger la qualité d'exploration du Code Researcher (manuel)

Même limite, même traitement que ci-dessus, et pour la même raison : les tests E2E tournent
sur `MockProvider`. Ce qu'ils prouvent — et c'est plus que pour le Dev Lead — c'est que le
node appelle **réellement** ses outils (`tool_calls > 0`, `tool_names` non vide) et que les
chemins rendus par le serveur **existent sur le disque**
(`tests/integration/mcp/test_code_search_dogfooding.py` interroge le vrai serveur sur le vrai
dépôt). Ce qu'ils ne prouvent pas : que le modèle a choisi de regarder au bon endroit, ni que
ce qu'il en conclut est juste.

### Protocole

Prérequis : `AGENTIVE_ALLOW_MCP_REGISTRATION=true make seed-dev` joué, une vraie clé LLM,
`AGENTIVE_DEV_CODE_ROOTS=/app`.

Demande de référence — celle de l'**AC3**, à passer telle quelle :

```bash
curl -sS -X POST "http://localhost:8000/api/v1/workflows/$WF/runs" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"input": {"objective": "Ajouter un nouveau module M13 au backend"}}'
```

Critères de lecture, dans cet ordre — **le premier est éliminatoire** :

1. **`tool_calls > 0`.** Sinon la sortie est inventée, et les critères suivants n'ont aucun
   sens à être notés.
2. **Chaque `path` de `relevant_files` existe** (commande de vérification au § 6 bis).
3. **Au moins deux modules distincts de `features/` sont couverts.** Un exemple unique est un
   exemple, pas un pattern.
4. **`existing_patterns` nomme la structure récurrente** : `router.py` / `service.py` /
   `schemas.py` par module, le repo dans `shared/repositories/`, les events dans
   `shared/contracts/events/`.
5. **`risk_areas` mentionne au moins une contrainte réelle du dépôt** — `.import-linter`, les
   11 règles d'or, les migrations Alembic — et non un risque générique de développement.
6. **Méthode** : la trace montre une recherche (`find_files` / `search_content`) **avant** les
   lectures, pas une enfilade de `read_file` au hasard.

### Résultat du dernier passage

| Date | Modèle | `tool_calls` | (2) chemins réels | (3) ≥ 2 modules | (4) patterns | (5) risques | (6) méthode |
|---|---|---|---|---|---|---|---|
| — | — | non exécuté | non exécuté | non exécuté | non exécuté | non exécuté | non exécuté |

> ⚠️ **Ce protocole n'a encore jamais été exécuté avec de vraies clés.** Tant que cette table
> est vide, la qualité du jugement d'exploration est **non vérifiée**. La table est versionnée
> vide plutôt qu'absente, exactement comme celle du § 7 : une table vide affirme visiblement
> que le protocole n'a pas tourné, là où l'absence de table laisse croire qu'il n'en fallait
> pas. Remplir au premier passage réel.

## 8. Rollback

- Le provisioning n'efface rien, et c'est une limite à connaître : `update_template` a une
  sémantique **PATCH**. Re-`make seed-dev` après avoir *modifié* une valeur du YAML la ré-applique
  bien ; après en avoir **retiré** un champ (`llm_model`, `error_policy`, …), la valeur reste en
  base — aucun chemin d'API ne la remet à `null`. Le provisioning **refuse alors de continuer** et
  nomme les champs concernés, plutôt que de rapporter « inchangé » sur une config périmée. Pour
  repartir d'un template propre : `PUT /api/v1/agents/templates/{id}` avec la config voulue.
- Un namespace préexistant dont le `type` ou le `department` diffère du catalogue fait aussi
  **échouer** le provisioning. La Mise en Place ne contrôle que l'existence du nom : sans ce refus,
  les runs partiraient et écriraient dans un namespace du mauvais type.
- **Le workflow d'entrée est versionné DANS SON NOM, et ce n'est pas cosmétique.**
  `create_workflow` est idempotent par empreinte SHA-256 de `{name, dag}`, et `workflows.name`
  n'est **pas** unique (`infra/db/models.py`). Passer le DAG de un à deux nodes change
  l'empreinte : sans changer le nom, la Story 5.2 aurait créé une **seconde** ligne
  « Dev Lead — prise de demande » à côté de celle de la 5.1, et le `workflow_id` noté par
  l'opérateur pointerait toujours sur l'ancienne — un seed qui rapporte « créé » à chaque
  exécution, et deux homonymes que rien ne distingue. Les deux autres options ont été
  écartées : **retirer** l'ancien demanderait un chemin de suppression de workflow qui n'existe
  nulle part dans le dépôt (constat de la Story 4.15), et se contenter d'une **recherche par
  nom** laisserait deux homonymes en base. Le provisioning signale explicitement l'ancien
  workflow dans son rapport — **tous** ses homonymes, la non-unicité du nom étant justement le
  sujet — et **refuse de continuer AVANT de créer** dès qu'il trouve un homonyme du nouveau nom
  dont le DAG ne monte pas les templates attendus. C'est une correction de la revue de la
  Story 5.2 : le contrôle ne levait que sur *deux* homonymes, donc il laissait créer le
  doublon puis se bloquait à l'exécution suivante — définitivement, faute de chemin de
  suppression.
- Les serveurs MCP : le provisioning n'en supprime aucun. Pour forcer une nouvelle découverte
  (un outil ajouté au serveur, par exemple), supprimer la row `tool_servers` puis rejouer —
  `connect_server` n'a **aucun** chemin de re-découverte, un nom déjà pris lève `ConflictError`
  avant même de spawner le serveur.

  > ⚠️ **Ne pas laisser de run partir entre les deux.** `tools.server_id` et
  > `agent_template_tools.tool_id` sont en `ON DELETE CASCADE` : supprimer la row emporte les
  > quatre outils **et** les assignations du template. Dans cette fenêtre, le Code Researcher a
  > `assigned_tools: []`, un run se lance normalement, et il produit exactement le
  > `tool_calls == 0` que le § 6 bis qualifie de « sortie inventée ». Le rejeu réassigne bien ;
  > c'est la fenêtre qui est dangereuse. Après rejeu, vérifier :
  > `GET /api/v1/agents/templates/$TPL/tools` doit rendre les quatre outils.
- Les migrations : `alembic downgrade` de `20260914000001` retire
  `workflow_runs.acknowledgement` (les runs existants perdent leur accusé — c'est de la trace, pas
  de l'état d'exécution).

## 9. Vérifications

```bash
make seed-dev                                        # 2e exécution : tout "inchangé", SANS le drapeau
make check-stories                                   # cohérence des statuts de story
```

**Dev Lead**
- `GET /api/v1/agents/templates/{id}` → `archetype: "orchestrateur"`, `config.system_prompt` non
  vide et énumérant les 8 rôles, `config.push_memory.namespace = "dev-metier"`,
  `config.output_contract.core` = `{plan, delegations, status}`.
- `GET /api/v1/agents/templates/{id}/tools` → `assigned_tools: []` (état assumé, cf § 3).

**Code Researcher**
- `GET /api/v1/agents/templates/{id}` → `archetype: "chercheur"`,
  `config.output_contract.core` = `{relevant_files, dependencies_graph, existing_patterns,
  risk_areas}`.
- `GET /api/v1/agents/templates/{id}/tools` → **quatre** outils :
  `list_directory`, `read_file`, `find_files`, `search_content`. **C'est la première liste non
  vide du dépôt** — la Story 5.0 a livré la boucle d'outils, la 5.1 le mécanisme d'assignation,
  et aucun template n'en portait un seul.

**Le run**
- Un `POST /runs` rend un `201` avec un `acknowledgement` non vide nommant **deux** agents.
- `metrics.per_node.code_researcher.tool_calls > 0` et `tool_names` non vide — la seule preuve
  que la sortie est appuyée sur une lecture réelle.
