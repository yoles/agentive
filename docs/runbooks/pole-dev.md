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

Ce que ça fait, **dans cet ordre** :

1. **Les namespaces mémoire** déclarés par le catalogue (`dev-metier`, type `metier`).
2. **Les agent-templates**, créés depuis leur archétype puis configurés.
3. **Les outils MCP** déclarés, résolus par nom.
4. **Le workflow d'entrée** « Dev Lead — prise de demande » (mono-node).

**L'ordre n'est pas cosmétique.** Un template dont le `push_memory.namespace` n'existe pas fait
**refuser** tout lancement de run par la Mise en Place (`memory_namespaces_accessible`,
Story 4.5 AC2 — échec permanent, donc `422`). Provisionner les templates d'abord livrerait des
agents configurés et incapables de démarrer.

**Idempotent** : une seconde exécution ne crée rien, ne duplique rien, et n'émet aucun event
d'audit gratuit. La sortie liste `créés` / `mis à jour` / `inchangés`.

### Si le provisioning échoue sur un outil

```
Dev Lead déclare des outils introuvables : read_file. Enregistrer le serveur MCP qui
les expose (POST /api/v1/tools/servers) avant de rejouer le provisioning.
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
> SSE). Arbitrage à porter à John. Les Stories 5.2 (filesystem + ripgrep) et 5.5 (GitHub) n'ont
> pas ce problème : leurs serveurs sont **tiers** et s'enregistrent normalement.

## 4. Lancer une demande

```bash
export TOKEN="$AGENTIVE_API_TOKEN"
export WF="<workflow_id rendu par make seed-dev>"

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
    "message": "Compris. Je mobilise Dev Lead. ETA ~1 min.",
    "agents": ["Dev Lead"],
    "eta_minutes": 1,
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
curl -sS "http://localhost:8000/api/v1/workflows/runs/$RUN" -H "Authorization: Bearer $TOKEN" \
  | jq '.metrics.per_node[].contract_problems // empty'
```

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
- Les migrations : `alembic downgrade` de `20260914000001` retire
  `workflow_runs.acknowledgement` (les runs existants perdent leur accusé — c'est de la trace, pas
  de l'état d'exécution).

## 9. Vérifications

```bash
make seed-dev                    # deuxième exécution : tout doit être "inchangé"
make check-stories               # cohérence des statuts de story
```

- `GET /api/v1/agents/templates/{id}` → `archetype: "orchestrateur"`, `config.system_prompt` non
  vide et énumérant les 8 rôles, `config.push_memory.namespace = "dev-metier"`,
  `config.output_contract.core` = `{plan, delegations, status}`.
- `GET /api/v1/agents/templates/{id}/tools` → la liste attendue.
- Un `POST /runs` rend un `201` avec un `acknowledgement` non vide.
