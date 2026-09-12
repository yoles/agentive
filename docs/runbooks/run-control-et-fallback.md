# Runbook — Contrôle de run & fallback LLM (Story 4.6)

> Interrompre, relancer ou annuler un workflow en cours, et comprendre ce qui
> se passe quand un provider LLM tombe.
>
> Défers fermés par cette story : **D12** (`provider_chain` runtime) et
> **D13** (dispatcher `error_policy`), tous deux ouverts par Story 2.2.

## 1. Quand l'utiliser

- Un run part en vrille (mauvais input, coût qui grimpe) et il faut l'arrêter.
- Un run doit être suspendu le temps de corriger une config, puis repris.
- Un run est passé `error` avec `LLMAllProvidersFailedError` et il faut savoir
  quel provider a échoué, comment, et combien de fois.
- Un opérateur constate qu'un agent n'utilise pas le provider qu'il a déclaré.

## 2. Prérequis

- `AGENTIVE_API_TOKEN` (header `Authorization: Bearer …`).
- Le `run_id` (retourné par `POST /workflows/{id}/runs`, ou lu en base).
- Accès `psql` en lecture pour les vérifications.

## 3. Matrice des transitions

C'est la **seule** source de vérité — elle vit dans
`features/workflow_engine/domain/run_control.py::_TRANSITIONS`, et tout 409
de cette feature en sort.

| Endpoint | Statut de départ | Effet immédiat | Statut après | Event publié | HTTP |
|---|---|---|---|---|---|
| `pause` | `running` | `control_signal = "pause"` | `running` (inchangé) | `…workflow_run.pause_requested` | **202** |
| `cancel` | `running` | `control_signal = "cancel"` — **écrase un `pause` en attente** | `running` (inchangé) | `…workflow_run.cancel_requested` | **202** |
| `cancel` | `paused` | statut → `cancelled`, `ended_at = now()` | `cancelled` | `…workflow_run.cancelled` | **200** |
| `resume` | `paused` | statut → `running`, signal effacé, tâche relancée | `running` | `…workflow_run.resumed` | **202** |

Tout autre couple → **409** (`/errors/conflict`), avec `run_id`,
`current_status`, `allowed_from`, `action` et `pending_control_signal` en
membres d'extension RFC 7807. `run_id` inconnu → **404**.

> **`pending_control_signal` est le champ qui décide de la suite.** Un 409
> « une demande est déjà en attente » sans lui laissait l'opérateur sans
> moyen de savoir *laquelle* — donc sans moyen de savoir si escalader en
> `cancel` passerait. `null` est une **réponse**, pas une omission : le run
> refuse pour son statut seul, aucune escalade ne le débloquera.
>
> Deux chemins mènent au 409 et le champ est renseigné sur les deux, mais
> il ne vient pas de la même lecture :
>
> - **statut illégal** (le couple n'est pas dans la matrice) — lu sur la
>   ligne chargée à l'entrée ;
> - **course perdue** (le compare-and-set renvoie 0) — **relu en base** au
>   moment de répondre, parce que dans la seule course qu'il sert à
>   expliquer, l'état lu avant l'écriture n'est déjà plus vrai.

> **Escalade `pause` → `cancel`, dans ce sens uniquement.** La garde
> d'écriture est sinon `control_signal IS NULL` (premier arrivé, premier
> servi), ce qui est juste entre deux demandes de même poids et faux pour la
> seule séquence opérateur qui compte : « mets en pause… non, tue-le ». Un
> `pause` ne peut pas révoquer un `cancel` déjà confirmé à quelqu'un.

> **202 vs 200 — le code EST le message.** 202 = « demande enregistrée, pas
> encore appliquée ». 200 = « c'est fait ». Le seul chemin immédiat est
> `cancel` sur un run déjà `paused`, parce qu'aucun driver n'est vivant pour
> observer quoi que ce soit.

### Commandes

```bash
BASE=https://localhost:8443/api/v1
AUTH="Authorization: Bearer $AGENTIVE_API_TOKEN"

curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/pause"  -H "$AUTH" | jq
curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/resume" -H "$AUTH" | jq
curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/cancel" -H "$AUTH" | jq
```

### `resume` repasse la Mise en Place

Un run pausé lundi et repris jeudi ne reprend pas dans l'environnement qu'il
a quitté : une clé peut avoir été tournée, un serveur MCP décommissionné, un
namespace mémoire supprimé. `resume` rejoue donc les quatre checks de la
Mise en Place (Story 4.5) avant de relancer quoi que ce soit, et **laisse le
run `paused`** si un check bloque — au lieu de le laisser mourir `error` sur
son premier node, checkpoint détruit, sans reprise possible.

En conséquence `resume` répond **422** — ou **503** si tous les checks en
échec sont `retryable` (un timeout DB, un serveur MCP momentanément
injoignable) — là où `pause` et `cancel` ne le font jamais. **Un 503 ici ne
veut pas dire « l'API est tombée »** : il dit « le pré-vol a refusé, et un
réessai identique a des chances d'aboutir tout seul ». Le bypass est le même
que celui de `POST /workflows/{id}/runs` :

```bash
curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/resume" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"force": true, "reason": "clé OpenAI restaurée, check en retard"}'
```

`reason` sans `force` est refusé, et `force` sans `reason` aussi : un bypass
qui ne dit pas pourquoi n'est pas traçable (l'event
`…workflow_run.mise_en_place_bypassed` le porte).

> Le worker de recovery, lui, n'est **pas** gaté : il reprend un run orphelin
> sans repasser les checks. Il ne répond à personne et ne peut pas demander
> un `force` — le gater transformerait une panne d'environnement en runs
> abandonnés silencieusement.

## 4. Latence attendue : ce n'est PAS instantané

`pause` et `cancel` sur un run vivant prennent effet **à la fin du node en
cours**, pas immédiatement. Compter jusqu'à :

```
NODE_TIMEOUT_S (60 s) × longueur de la chaîne de providers
  + les retries éventuels de error_policy
```

soit **~2 minutes** pour un template qui déclare `on_timeout: fail_fast`.

> ⚠️ **Ce n'est PAS le cas nominal, et cette ligne disait le contraire
> jusqu'au 2026-09-12.** `retry_with_backoff` est le **défaut** : un template
> sans `error_policy` hérite de `retry_with_backoff` / `max_retries: 3` /
> `exponential` (`domain/error_policy.py`), ce que l'AC3 de la 4.6 énonce
> explicitement. Pour la forme de template la plus répandue — aucune
> `error_policy` déclarée — le pire cas nominal est donc
> `(60 + 15) × 2 × 4 + 7 ≈ **607 s**, soit ~10 minutes`, pas deux.
>
> Et si le process pilote est **mort**, aucune de ces bornes ne s'applique :
> la demande attend le balayage de recovery, soit ~25 minutes aux réglages
> par défaut (cf § 5).

**Pourquoi c'est voulu.** L'interruption est *coopérative* : le driver
observe le signal à la frontière d'un superstep, le seul instant où
LangGraph a committé son checkpoint. S'arrêter ailleurs perdrait le travail
du node en cours **et** couperait un appel LLM déjà facturé par le provider,
dont la réponse n'atteindrait jamais le checkpoint.

`asyncio.CancelledError` n'est **pas** utilisable ici : sa sémantique est
déjà réservée à l'arrêt du process (`cancel_inflight_runs`), où elle traverse
délibérément le `except Exception` de `_execute` pour laisser la row sur
`running` et la faire reprendre par le recovery worker au boot suivant.
L'utiliser pour une annulation utilisateur donnerait : run annulé → relancé
automatiquement quelques minutes plus tard.

## 5. Lire `control_signal` en prod

```sql
SELECT id, status, control_signal, control_requested_at, ended_at,
       checkpoint->>'last_node_id' AS last_node
FROM workflow_runs
WHERE control_signal IS NOT NULL
   OR status IN ('paused', 'cancelled')
ORDER BY started_at DESC
LIMIT 20;
```

Lecture des combinaisons :

| `status` | `control_signal` | Interprétation |
|---|---|---|
| `running` | `NULL` | run nominal |
| `running` | `pause` / `cancel` | **demande en attente** — le driver ne l'a pas encore observée |
| `paused` | `NULL` | suspendu proprement, reprenable |
| `cancelled` | `NULL` | terminal |
| `completed` / `error` | `NULL` | terminal |
| Tout statut non-`running` | non-`NULL` | **anormal** — ne devrait jamais arriver : la transition et l'effacement du signal sont atomiques (`update_status(clear_control=True)`, sur les trois statuts terminaux `completed`/`error`/`cancelled` ET sur `paused`) |

Un `running` + signal qui ne bouge pas depuis longtemps signifie que le
process qui pilotait le run est mort. Ce n'est **pas** bloqué : la row reste
`running`, donc `claim_stale_running` la revendiquera, et le contrôle
pré-boucle du driver réglera le signal **sans exécuter un seul node**.

## 6. Interaction avec le recovery worker

Le design tient sur un point : **le signal est une colonne, pas un statut.**

- `claim_stale_running` filtre `status = 'running'` → un run `paused` n'est
  **jamais** repris automatiquement. Gratuit, sans une ligne de code en plus.
- Un run `running` porteur d'un signal non observé **est** repris — c'est
  voulu, c'est ce qui rend une demande perdue auto-réparante.
- `_abandon_one` écrit `only_if_status="running"` → un run passé `paused`
  entre la revendication et l'abandon est protégé.

Si `pausing`/`cancelling` avaient été des statuts, ces trois comportements
auraient dû être ré-écrits un par un.

## 7. Chaîne de providers per-agent (défer D12)

Un template peut déclarer sa propre chaîne :

```json
{ "provider_chain": ["anthropic", "openai"] }
```

Depuis cette story, `execute_agent_node` la passe réellement à
`LLMRouter.complete(provider_chain=…)` — **après intersection avec les
providers effectivement enregistrés dans le process**.

> ⚠️ **L'intersection n'est pas une précaution.** `LLMRouter.complete()` lève
> un `ValueError` sec sur un nom de provider inconnu, et
> `_build_llm_router` n'enregistre que les providers dont la clé API existe
> (en dev/CI, seul `mock`). Sans intersection, un template portant la valeur
> par défaut des archétypes ferait mourir **tous** les runs.

Comportement :

L'intersection ne suffit pas : le routeur traite **l'index 0 comme le
propriétaire du modèle** (`LLMRouter._resolve_model` renvoie le modèle
primaire tel quel pour l'index 0 et ne consulte la table de fallback qu'à
partir de l'index 1). Une chaîne dont la tête ne sert pas `llm_model`
envoie donc, par exemple, `claude-sonnet-4-6` à OpenAI : un 400 classé
`fatal`, sans fallback ni retry. La résolution **fait tourner** la chaîne
pour mettre le propriétaire du modèle en tête plutôt que de refuser.

| Chaîne déclarée (`llm_model` Anthropic) | Providers enregistrés | Chaîne effective | Log |
|---|---|---|---|
| `["anthropic","openai"]` | les deux | telle quelle | — |
| `["openai","anthropic"]` | les deux | `["anthropic","openai"]` — **rotation** | `provider_chain_reordered` |
| `["anthropic","openai"]` | `openai` seul | `None` (défaut process) — aucun ordre ne sauve la chaîne | `provider_chain_unavailable` |
| `["anthropic","openai"]` | `mock` seul | `None` (défaut process) | `provider_chain_unavailable` |
| `["mistral"]` (nom inconnu) | — | `None` (défaut process) | `provider_chain_unavailable` |
| mal typée / vide (`provider_chain: "anthropic"`) | — | `None` (défaut process) | `provider_chain_unavailable` |
| absente | — | `None` (défaut process) | — (personne n'a rien demandé) |

```bash
# Un node qui ne tourne pas sur la chaîne voulue :
docker compose logs backend | grep provider_chain_unavailable
```

Le check de pré-vol `llm_providers_configured` (Mise en Place, Story 4.5)
regarde désormais la **chaîne** et pas seulement le provider déduit de
`llm_model` — il appelle la même fonction que le node, donc il prédit ce que
le runtime *fait* et non ce que la config *a l'air* de dire. Ce qu'il
distingue, parce que les remédiations n'ont rien à voir entre elles :

| Constat | Verdict | Où est le correctif |
|---|---|---|
| Clé API absente pour un provider **de secours** de la chaîne (celui du `llm_model` en a une) | **passe**, mention `Degraded:` | nulle part — le run tourne, sans sa jambe de repli |
| Clé API absente pour le provider **qui possède `llm_model`** | **échoue** | l'environnement |
| Clé API absente pour **tous** les providers de la chaîne | **échoue** | l'environnement |
| Chaîne valide mais aucun de ses providers ne sert `llm_model` | **échoue** | le template |
| `provider_chain` illisible (chaîne nue, liste vide, élément non-`str`) | **échoue** | le template |
| Nom de provider inconnu (`"mistral"`, une typo) | **échoue** | le template |

> ⚠️ Le cas le plus courant est le **deuxième**, pas le premier :
> `provider_chain: ["anthropic","openai"]` avec un `llm_model` Anthropic et
> pas de clé Anthropic. Aucune rotation ne sauve cette chaîne (le routeur
> traite l'index 0 comme le propriétaire du modèle), elle est donc
> abandonnée et le pré-vol **bloque**. Une version antérieure de ce tableau
> annonçait l'inverse en confondant les deux lignes.
>
> Une clé absente est un état d'environnement **légitime** — staging tourne
> sans clé OpenAI exprès —, d'où la dégradation qui passe quand elle ne
> touche qu'une jambe de secours. Un nom de
> provider inconnu ne l'est dans aucun environnement : aucune clé ne le
> fera exister, et le template revendique une jambe de repli qui n'a jamais
> existé. `force` reste la sortie dans tous les cas.

## 8. Politique d'erreur per-agent (défer D13)

```json
{ "error_policy": { "on_timeout": "retry_with_backoff",
                    "max_retries": 3,
                    "backoff_strategy": "exponential" } }
```

| `on_timeout` | Retries de node | Chaîne de fallback |
|---|---|---|
| `retry_with_backoff` | jusqu'à `max_retries` | **oui** |
| `fail_fast` | 0 | **oui** |
| `fallback_provider` | 0 | **oui** |

> **`fail_fast` veut dire « zéro retry de NODE », jamais « pas de fallback ».**
> La chaîne relève de NFR12 et n'est pas négociable par template ; un agent
> qui veut vraiment un seul provider l'exprime par `provider_chain: ["x"]`.

Un retry n'a lieu **que** sur `LLMAllProvidersFailedError`, c'est-à-dire
quand la chaîne *entière* a échoué de façon retriable. Jamais sur une erreur
fatale (`LLMProviderAuthError`, `LLMProviderBadRequestError`,
`LLMNoFallbackModelError`) ni sur une `ValidationError` de config : les
réessayer masquerait un bug au lieu de le montrer.

> ⚠️ **Le piège : une erreur fatale peut arriver *emballée*.** Si elle
> survient au milieu de la chaîne, `LLMRouter` l'enveloppe dans un
> `LLMAllProvidersFailedError` — dont le type dit « retriable » alors que la
> cause ne l'est pas. Un `llm_model` absent de la table de fallback se
> retrouvait ainsi réessayé trois fois avec backoff, pour un échec
> rigoureusement identique à chaque tour. `execute_agent_node` lit donc
> `error_class` de la **dernière tentative** du contexte de l'exception, pas
> seulement son type, et s'arrête sur `fatal`.

Délais : `exponential` = `base × 2ⁿ`, `linear` = `base × (n+1)`,
`constant` = `base`, chacun plafonné par
`AGENTIVE_WORKFLOW_RETRY_MAX_DELAY_S`.

> ⚠️ **`max_retries` est plafonné à 3 au runtime**, alors que le schéma
> Story 2.2 valide jusqu'à 10. Divergence assumée : le seuil de détection des
> runs orphelins (`recovery.derive_stale_threshold_s`) est *dérivé* de la durée
> pire-cas d'un node ; le dériver contre 10 le porterait à plus d'une heure,
> soit un run réellement planté qui reste ignoré pendant une heure. Un
> dépassement est loggé (`error_policy_retries_capped`), jamais silencieux.

## 9. Déboguer un run mort sur épuisement de chaîne

L'event `workflow_engine.workflow_run.failed` porte désormais `error_type` —
filtrable sans parser de la prose :

```sql
SELECT payload->>'run_id', payload->>'failed_node_id', payload->>'error_type'
FROM outbox_events
WHERE event_type = 'workflow_engine.workflow_run.failed'
  AND payload->>'error_type' = 'LLMAllProvidersFailedError'
ORDER BY created_at DESC LIMIT 20;
```

Le détail par tentative — jusqu'ici intégralement perdu — est persisté,
**redacté** (NFR9) et borné à 5 entrées :

```sql
SELECT jsonb_pretty(checkpoint->'last_error_attempts')
FROM workflow_runs WHERE id = :run_id;
```

Chaque entrée : `provider`, `model_attempted`, `error_type`, `error_detail`
(tronqué à 500 car.), `error_class`.

Le nombre de traversées de chaîne payées par un node :

```sql
SELECT jsonb_object_agg(k, v->'llm_attempts')
FROM workflow_runs, jsonb_each(metrics->'per_node') AS t(k, v)
WHERE id = :run_id;
```

Pour le débogage du fallback lui-même (métriques routeur, classification des
erreurs, `DEFAULT_MODEL_FALLBACK_MAP`), voir
[`llm-usage.md` § Debugging fallback issues](./llm-usage.md) — non dupliqué ici.

## 10. Trois niveaux de reprise, à ne pas confondre

| | Quoi | Qui | Statut |
|---|---|---|---|
| **Fallback de chaîne** | provider A KO → provider B, modèle équivalent | `LLMRouter.complete()` — Story 1.6 | ✅ livré |
| **Retry de node** | la chaîne ENTIÈRE a échoué → tout rejouer, avec backoff | `execute_agent_node` — Story 4.6 (D13) | ✅ livré |
| **Retry intra-provider** | 429 chez A → réessayer A | bucket `retriable_same_provider`, **vide par décision** | ⏭ Story 9.5 |

Détail : [`docs/decisions/llm-fallback-policy.md`](../decisions/llm-fallback-policy.md).

## 11. Rollback

La migration est additive à l'aller (deux colonnes nullable). Le retour ne
l'est pas — il perd la distinction `cancelled`/`error`, voir le tableau
ci-dessous. Le `downgrade` de
`20260912_000001_workflow_runs_control_signal` **réconcilie les statuts
avant** de supprimer les deux colonnes :

```bash
docker compose run --rm backend uv run alembic downgrade 20260911000001
```

| Statut avant downgrade | Devient | Pourquoi |
|---|---|---|
| `paused` | `running` | Le code pré-4.6 n'a pas d'endpoint `resume`, ne considère pas `paused` comme terminal et ne le revendique pas non plus (`claim_stale_running` filtre `running`) : la ligne serait **immobile à vie**. En `running`, elle garde son checkpoint et son `ended_at` NULL, donc le sweep de recovery la reprend dès qu'elle devient stale et la mène à terme. |
| `cancelled` | `error` | `error` est le seul statut terminal non-succès que connaît le code restauré. **Perte assumée** : l'intention de l'opérateur survit dans les events et les métriques, pas dans `status`. |
| `control_signal` en attente | disparaît | Le run va au bout — exactement ce que le code restauré en aurait fait. |

Aucune action manuelle préalable n'est donc requise. Pour savoir ce que le
downgrade va déplacer :

```sql
SELECT status, count(*) FROM workflow_runs
WHERE status IN ('paused', 'cancelled') GROUP BY status;
```

## 12. Vérifications

```sql
-- Aucune row incohérente (signal résiduel sur un run non-running)
SELECT count(*) FROM workflow_runs
WHERE control_signal IS NOT NULL AND status <> 'running';  -- doit valoir 0

-- Les events de contrôle arrivent bien dans l'outbox
SELECT event_type, count(*) FROM outbox_events
WHERE event_type LIKE 'workflow_engine.workflow_run.%cancel%'
   OR event_type LIKE 'workflow_engine.workflow_run.paus%'
GROUP BY event_type;
```

Côté flux SSE : `GET /workflows/runs/{id}/events` **se ferme** sur
`cancelled` (comme sur `completed`/`error`), mais **reste ouvert** sur
`paused` — un run en pause peut reprendre, le client doit continuer à
écouter. Le plafond `_MAX_STREAM_DURATION_S` (1 h) borne le cas où il ne
reprend jamais.

Les events de cycle de vie sont **droppables** par conception (file bornée
par client : un consommateur lent ne doit pas bloquer la diffusion pour tous
les autres). Le flux se réconcilie donc sur la ligne toutes les 15 s dès
qu'il est inactif, et émet une frame `state` portant `reason:
"status_repoll"` à **tout** écart constaté — pas seulement à la
terminaison. C'est ce qui rattrape un `paused` perdu : un run en pause
n'émet plus rien, donc aucun autre mécanisme ne réveillerait le client avant
le plafond d'une heure. Une demande de pause encore en attente est visible
de la même façon, via `control_signal` dans la frame.
