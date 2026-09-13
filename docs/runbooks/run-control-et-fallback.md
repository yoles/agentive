# Runbook — Contrôle de run & fallback LLM (Story 4.6, étendu par Story 4.9 AC6)

> Interrompre, relancer, annuler ou **rétracter** une demande sur un workflow
> en cours, et comprendre ce qui se passe quand un provider LLM tombe.
>
> Défers fermés par Story 4.6 : **D12** (`provider_chain` runtime) et
> **D13** (dispatcher `error_policy`), tous deux ouverts par Story 2.2.
>
> Défers fermés par Story 4.9 AC6 (re-passe adversariale sur cette même
> surface) : délai réel de pause/cancel sans driver vivant documenté et
> observable (§4bis), demande abandonnée à `END` (§4ter), précédence
> opérateur vs plafond anti-poison (§6), rejeu de `cancel` idempotent (§3),
> rétractation d'une demande (§3bis), cohérence du plafond SSE avec le seuil
> d'orphelin réglable (§12), budget d'arrêt du recovery worker recompté et
> `stop_grace_period` posé (§13).

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
| `cancel` | `running` | `control_signal = "cancel"` — **écrase un `pause` en attente, ou un `cancel` déjà en attente (rejeu idempotent, Story 4.9 T6.4)** | `running` (inchangé) | `…workflow_run.cancel_requested` | **202** |
| `cancel` | `paused` | statut → `cancelled`, `ended_at = now()` | `cancelled` | `…workflow_run.cancelled` | **200** |
| `resume` | `paused` | statut → `running`, signal effacé, tâche relancée | `running` | `…workflow_run.resumed` | **202** |
| `retract` (Story 4.9 T6.5) | `running`, **avec** `control_signal` en attente | `control_signal`/`control_requested_at` → `NULL` | `running` (inchangé) | `…workflow_run.control_retracted` | **200** |

Tout autre couple → **409** (`/errors/conflict`), avec `run_id`,
`current_status`, `allowed_from`, `action`, `pending_control_signal` **et**
`pending_control_requested_at` (Story 4.9 T6.1) en membres d'extension RFC
7807. `run_id` inconnu → **404**.

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

> **`cancel` sur `cancel` : rejeu idempotent, pas un 409 (Story 4.9 T6.4).**
> Un `POST /cancel` rejoué (timeout réseau, double-clic, retry de gateway)
> pendant que le premier attend toujours d'être observé répond **202** —
> jamais un 409 pour une annulation qui se déroule pourtant correctement.
> `control_requested_at` ne bouge **pas** : un rejeu est la même demande
> réessayée, pas une nouvelle, et re-tamponner épinglerait le champ à « à
> l'instant » précisément pour le client qui réessaie — détruisant le signal
> de driver mort (§4bis) dans le seul scénario où il sert. Une **escalade**
> `pause` → `cancel` est une vraie nouvelle demande et déplace l'horloge. Sûr précisément parce que
> l'effet de `cancel` est idempotent : ré-écrire le MÊME signal ne change
> rien à ce que le driver fera à sa prochaine frontière de superstep. `pause`
> reste premier-arrivé-premier-servi (aucune entrée ne laisse `pause`
> s'auto-écraser) — ce raisonnement ne s'étend pas à deux actions
> DIFFÉRENTES qui se disputent.

> **202 vs 200 — le code EST le message.** 202 = « demande enregistrée, pas
> encore appliquée ». 200 = « c'est fait ». Le seul chemin immédiat est
> `cancel` sur un run déjà `paused`, parce qu'aucun driver n'est vivant pour
> observer quoi que ce soit.

### Commandes

```bash
BASE=https://localhost:8443/api/v1
AUTH="Authorization: Bearer $AGENTIVE_API_TOKEN"

curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/pause"   -H "$AUTH" | jq
curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/resume"  -H "$AUTH" | jq
curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/cancel"  -H "$AUTH" | jq
curl -sS -X POST "$BASE/workflows/runs/$RUN_ID/retract" -H "$AUTH" | jq
```

### §3bis — Rétracter une demande (Story 4.9 AC6/T6.5)

« Je me suis trompé de run, j'annule ma propre demande avant qu'elle ne
s'applique. » Légal **uniquement** depuis `running` et **uniquement** tant
qu'un `pause`/`cancel` est encore en attente (`control_signal IS NOT NULL`) —
une fois observée par le driver (ou le run terminé), il n'y a plus rien à
rétracter et l'appel répond **409**, avec le même `pending_control_signal`
que les trois autres routes.

`retract` publie `…workflow_run.control_retracted`, portant
`retracted_signal` (ce qui était en attente). La demande qu'il défait est
elle-même une ligne d'outbox durable : sans contrepartie, un auditeur
rejouant le bus voit une annulation demandée sur un run qui s'est ensuite
terminé normalement, sans rien qui dise qu'elle a été retirée. La
disparition de `control_signal` de la frame SSE `state` (§5) n'atteint qu'un
client connecté à cet instant — ce n'est pas une piste d'audit.

L'event n'est publié que si l'écriture a matché : elle est gardée sur
`control_signal IS NOT NULL`, donc un signal qui a bougé entre-temps ne
produit ni écriture ni event.

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

> **Désactiver un workflow n'empêche pas de reprendre ses runs déjà lancés.**
> La désactivation gouverne le **lancement** : `POST /runs` refuse, `resume`
> non. Trois raisons. (1) Le worker de recovery reprend ces runs de toute
> façon — refuser ici ne contraignait que l'opérateur, c'est-à-dire
> l'appelant le mieux informé. (2) Un refus n'avait **aucune sortie** : un run
> `paused` est exclu de `claim_stale_running` ET de la purge (terminaux
> seulement), il serait donc resté irréprenable et impurgeable à vie, signalé
> chaque jour par `stale_paused_runs_detected`. (3) Tuer le run reste
> disponible, explicitement et traçablement : `POST /cancel`.
>
> Le resume d'un workflow non actif est journalisé
> (`workflow_engine.resume_of_inactive_workflow`) — autorisé, pas invisible.

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
> la demande attend le balayage de recovery, soit **~1548 s (~25,8 minutes)**
> aux réglages par défaut (`recovery.derive_stale_threshold_s` + l'intervalle
> de balayage, `interval_s=30s`) — chiffre porté de 375 s à 1518 s par la
> Story 4.6 elle-même sans qu'aucune AC ni aucun runbook ne l'énonce avant
> Story 4.9 AC6/T6.1. Ce délai n'est **pas réduit** par cette story (un
> mécanisme de vivacité — un bail plutôt que l'heuristique
> `last_checkpoint_at` — a été jugé hors périmètre) : il est documenté tel
> quel, et rendu **observable** — `control_requested_at` apparaît désormais
> dans la frame SSE `state` (§5) et dans le contexte de tout 409 sur cette
> surface (`pending_control_requested_at`), là où il n'était auparavant
> écrit nulle part et lu par rien.

### §4bis — Une demande abandonnée à `END` (Story 4.9 AC6/T6.2)

Une demande écrite pendant que le dernier superstep est encore en vol peut
perdre la course : le run atteint `END` avant que le driver n'observe le
signal. `_mark_completed`, `_mark_failed` et `recovery._abandon_one`
l'effacent tous trois silencieusement (`clear_control=True` sur leur
transition terminale) — **décision assumée : NO-OP documenté, pas un nouvel
event `control_request_dropped`.** Faire grossir le contrat public pour un
cas qu'un client attentif peut déjà déduire (le `control_signal` qu'il
suivait sur le flux SSE disparaît simplement au prochain frame `state`,
remplacé par le statut terminal) a été jugé ne pas valoir la surface
supplémentaire — d'autant qu'un nouvel event serait exposé à la même
perte-en-vol que celle qu'il existerait pour signaler. Si l'expérience
opérateur montre que c'est insuffisant, rouvrir ce point plutôt que de
supposer que l'absence de plainte vaut validation.

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

`control_requested_at` (colonne ci-dessus) répond à « depuis quand ? » sans
recalcul mental contre le §4bis : un écart de quelques secondes est nominal,
un écart proche de ~1548 s signale un process mort en attente du sweep. Ce
champ est désormais aussi lu, pas seulement écrit (Story 4.9 T6.1) : la
frame SSE `state` le porte dès qu'un `control_signal` est présent, et le
contexte RFC 7807 de tout 409 sur cette surface aussi
(`pending_control_requested_at`).

> **Formats de date sur les deux surfaces.** Le contexte 409 et la frame SSE
> émettent tous deux de l'**ISO-8601** (`2026-09-13T12:00:00+00:00`), et rien
> d'autre : un parseur ISO strict suffit pour les deux.

### Types de frame émis par `GET /workflows/runs/{id}/events`

| `event:` | Quand | Contenu |
|---|---|---|
| `state` | à chaque tick, et à la connexion | `status`, `control_signal`, `control_requested_at`, `reason` |
| `workflow_engine.workflow_run.*` | à chaque event de cycle de vie du run | le payload de l'event (`started`, `node_completed`, `paused`, `resumed`, `cancelled`, `completed`, `failed`, …) |
| `workflow_engine.workflow_run.resume_mise_en_place_evaluated` | **à chaque `resume`** | le rapport Mise en Place complet qui a autorisé (ou refusé) la reprise |

> **⚠️ `resume_mise_en_place_evaluated` est un type de frame ajouté par la
> Story 4.12 AC3** et non documenté à l'époque . Le
> filtre du flux est `workflow_engine\.workflow_run\.\w+`, donc tout client
> déjà branché le reçoit sans avoir rien demandé, et il consomme un slot de
> la file bornée `_EVENT_QUEUE_MAXSIZE`. Un consommateur qui énumère les
> types de frame attendus doit l'ajouter ; un consommateur qui ignore les
> types inconnus n'a rien à faire.

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

### Précédence : `cancel` en attente vs plafond anti-poison (Story 4.9 AC6/T6.3)

Un run qui dépasse `MAX_RECOVERY_ATTEMPTS` (3 reprises consécutives sans
progrès) est abandonné par `_abandon_one` — **sauf** s'il porte un
`control_signal = "cancel"` déjà accusé réception (**202**) et jamais
observé. Dans ce cas **l'annulation opérateur l'emporte** : le run se règle
`cancelled`, pas `error`, et l'event publié est
`…workflow_run.cancelled` (pas `…workflow_run.failed`) — la promesse du
`202` initial est honorée même si aucun driver vivant n'a jamais pu
l'observer.

Un `pause` en attente, lui, **ne** bénéficie **pas** de cette précédence : le
run reste abandonné en `error`, exactement comme sans signal du tout. Un
`pause` implique « reprenable plus tard », ce qu'un run empoisonné n'est
pas — honorer un `pause` ici reviendrait à prétendre qu'un `resume` futur a
un sens.

```sql
-- Runs abandonnés par le plafond anti-poison cette semaine.
--
-- Discriminant pour la moitié `cancelled` (qui ne porte pas
-- d'`error_type`, ce champ n'étant déclaré que sur WorkflowRunFailedEvent) :
-- un abandon honoré par le recovery worker a forcément franchi le plafond
-- MAX_RECOVERY_ATTEMPTS (3), donc son run porte `recovery_attempts > 3` dans
-- son checkpoint. Une annulation opérateur directe, non.
SELECT e.event_type, count(*)
FROM outbox_events e
LEFT JOIN workflow_runs r ON r.id = (e.payload->>'run_id')::uuid
WHERE e.created_at >= now() - interval '7 days'
  AND (
        (e.event_type = 'workflow_engine.workflow_run.failed'
         AND e.payload->>'error_type' = 'RecoveryAbandoned')
     OR (e.event_type = 'workflow_engine.workflow_run.cancelled'
         AND coalesce((r.checkpoint->>'recovery_attempts')::int, 0) > 3)
      )
GROUP BY e.event_type;
```

> Le `LEFT JOIN` peut ne rien trouver si le run a depuis été supprimé
> (`ON DELETE CASCADE` depuis `workflows`) : `coalesce(..., 0)` exclut alors
> la ligne, ce qui est le bon défaut — sans le run on ne peut pas affirmer
> qu'il s'agissait d'un abandon.

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

Pour savoir ce que le downgrade va déplacer :

```sql
SELECT status, count(*) FROM workflow_runs
WHERE status IN ('paused', 'cancelled') GROUP BY status;
```

### ⚠️ Contre du trafic vivant (Story 4.11 AC7)

**Ce downgrade n'est PAS sans risque tant qu'un backend de la version
PRÉCÉDENTE tourne encore.** `infra/scripts/deploy-staging.sh` applique les
migrations AVANT de basculer les containers backend — un driver 4.6 encore
en vol peut écrire `status = 'paused'` sur une ligne **entre** la
réconciliation ci-dessus et le `DROP COLUMN` qui la suit dans la même
transaction, reproduisant exactement la ligne immobile à vie que la
réconciliation existe pour éviter.

Depuis cette story, le `downgrade()` pose `LOCK TABLE workflow_runs IN
EXCLUSIVE MODE` (après un `SET LOCAL lock_timeout = '5s'`) avant la
réconciliation : aucune autre transaction ne peut plus écrire une nouvelle
ligne `paused` une fois le verrou obtenu, et **si un writer vivant le
détient déjà, la migration échoue proprement au bout de 5s** (erreur
typée, `lock_timeout` PostgreSQL) plutôt que de parquer indéfiniment tout
lecteur de `workflow_runs` derrière elle. Ce n'est pas une garantie
d'innocuité pour autant — un downgrade contre du trafic vivant reste refusé
par construction (l'échec au bout de 5s), pas rendu sûr.

**La procédure sûre reste de drainer le trafic avant tout downgrade
touchant `workflow_runs` :**

```bash
# 1. Arrêter le backend (aucun driver ne peut plus écrire pendant la migration)
docker compose -f docker-compose.yml -f docker-compose.staging.yml stop backend

# 2. Downgrade
docker compose run --rm backend uv run alembic downgrade 20260911000001

# 3. Redémarrer (avec le code compatible avec le schéma downgradé)
docker compose -f docker-compose.yml -f docker-compose.staging.yml up -d backend
```

`deploy-staging.sh` lui-même n'appelle jamais `downgrade` (seulement
`upgrade head`, avant de basculer les containers) — ce risque n'existe
aujourd'hui que pour un rollback MANUEL, d'où une procédure documentée ici
plutôt qu'un changement du script automatisé.

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
écouter. Le plafond, ~1 h par défaut, borne le cas où il ne reprend jamais
(cf §13 — ce n'est plus une constante figée depuis Story 4.9 T6.6).

Les events de cycle de vie sont **droppables** par conception (file bornée
par client : un consommateur lent ne doit pas bloquer la diffusion pour tous
les autres). Le flux se réconcilie donc sur la ligne toutes les 15 s dès
qu'il est inactif, et émet une frame `state` portant `reason:
"status_repoll"` à **tout** écart constaté — pas seulement à la
terminaison. C'est ce qui rattrape un `paused` perdu : un run en pause
n'émet plus rien, donc aucun autre mécanisme ne réveillerait le client avant
le plafond. Une demande de pause encore en attente est visible de la même
façon, via `control_signal` **et** `control_requested_at` dans la frame
(Story 4.9 T6.1).

## 13. Le plafond SSE, réconcilié avec le seuil d'orphelin (Story 4.9 AC6/T6.6)

`_MAX_STREAM_DURATION_S` (le nom de la constante avant cette story) était
**figé à 3600 s**, sans rapport avec `recovery.derive_stale_threshold_s` — le
seuil, RÉGLABLE par variables d'environnement, au-delà duquel le recovery
worker considère un run orphelin. Aux plafonds `le` de ces variables
(`AGENTIVE_WORKFLOW_RETRY_BASE_DELAY_S<=60`, `...MAX_DELAY_S<=300`,
`AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S<=60`,
`AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_TIMEOUT_S<=45`), le seuil dérivé atteint
**~3675 s** — au-delà du plafond SSE. Un déploiement poussant ces réglages
vers leurs limites légales pouvait donc voir un client recevoir
`stream_timeout` (`status: "running"`) sur un run que le recovery worker ne
considérait même pas encore orphelin.

Corrigé en **dérivant** le plafond plutôt qu'en le figeant :
`_max_stream_duration_s()` (désormais une fonction, pas une constante) vaut
`max(3600s, derive_stale_threshold_s(réglages actuels) + 120s)` — 120 s de
marge absorbant la cadence de balayage du recovery worker (`interval_s`,
30 s par défaut) et l'aléa d'ordonnancement. Aux réglages par défaut, rien
ne change (le plancher de 1 h s'applique toujours) ; aux plafonds légaux, le
plafond SSE suit désormais le seuil d'orphelin au lieu de lui rester en
dessous.

## 14. Budget d'arrêt du recovery worker (Story 4.9 AC6/T6.7)

`WorkflowRecoveryWorker.stop()` prétendait rester « confortablement sous le
budget de 5 s » du lifespan. Faux : il dépense `_STOP_CANCEL_TIMEOUT_S`
(3 s, annulation des reprises en vol) **puis** `_STOP_GRACE_S` (2 s) **puis**,
si besoin, un second `_STOP_CANCEL_TIMEOUT_S` (3 s) pour la boucle
principale — soit **8 s au pire cas**, pas 5, et ce n'est qu'UN des workers
que `app.lifespan` arrête à la suite des autres.

`docker-compose.yml` fixe désormais `stop_grace_period: 30s` sur le service
`backend` (auparavant absent — SIGKILL Docker par défaut à 10 s). Vérifier
après tout changement aux constantes `_STOP_GRACE_S`/`_STOP_CANCEL_TIMEOUT_S`
que cette marge reste généreuse, pas seulement suffisante.

## 15. Bornes de charge sur la création et la reprise de run (Story 4.9 AC2)

`POST /workflows/{id}/runs` et `POST /workflows/runs/{id}/resume` refusent
désormais **429** (`/errors/rate-limit`) au-delà de
`AGENTIVE_WORKFLOW_MAX_CONCURRENT_RUNS` (20 par défaut) runs `running`
simultanés du MÊME workflow. Deux vérifications, pas une : une première,
rapide, **avant** la Mise en Place (évite de payer jusqu'à 8 pings MCP
concurrents et un Dry Run complet pour une requête qui sera de toute façon
refusée), puis une seconde dans la MÊME transaction que l'écriture
(`INSERT`/`UPDATE`), qui réduit la fenêtre entre le comptage et l'écriture
au minimum que les transactions de ce repo permettent.

> **⚠️ Ce plafond n'est pas un quota linéarisable** (ce runbook affirmait auparavant que seule la première
> vérification était racée, ce que le code contredit explicitement). Sous
> `READ COMMITTED`, **les deux** vérifications peuvent être racées : deux
> appelants concurrents lisent le même compte avant que l'un ou l'autre ne
> commite. 50 `POST /runs` simultanés peuvent donc tous lire `count = 0` et
> tous insérer. Fermer réellement la course demanderait un verrou sérialisé
> (verrou consultatif keyé sur `workflow_id`), que l'AC2 n'a jamais demandé.
> C'est un **plafond mécanique contre les rafales**, pas une garantie.
> Voir le docstring de `WorkflowRunRepo.count_running_in_session`, qui fait
> foi.

> **Les runs orphelins ne comptent pas.** Un run dont le process pilote est
> mort reste `running` jusqu'à ce que le balayage recovery le récupère —
> **≈ 1548 s** aux réglages par défaut (cf §4bis). Le compte exclut donc les
> runs silencieux au-delà de ce même seuil : le plafond et le balayage
> partagent une seule définition de « vivant » (`COALESCE(last_checkpoint_at,
> started_at)`), au lieu de deux qui se contredisent. Sans ça, un crash
> backend avec N runs en vol bloquait le workflow en 429 pendant ~26 minutes.
>
> Conséquence assumée : entre le seuil et le tick de balayage suivant, un run
> n'est plus compté mais est toujours `running`. Si le balayage le reprend, il
> recompte — le plafond peut donc être brièvement dépassé. C'est cohérent avec
> ce qu'il est (une borne mécanique, pas un quota), jamais avec ce qu'il n'est
> pas.

`StartRunRequest.input` est par ailleurs borné à 256 KiB sérialisé JSON
(`/errors/validation`, 422) — un `task_input` sans limite était porté tel
quel dans le prompt de chaque node du workflow. La mesure porte sur le JSON
**tel qu'il transite** (UTF-8, `ensure_ascii=False`) : un corps en français,
en cyrillique ou en CJK est compté à sa taille réelle, pas à celle de ses
échappements `\uXXXX` (200 Ko de texte accentué
mesuraient auparavant 600 Ko et étaient refusés).

La politique de coût (budget caps, rate limiting multi-provider) reste hors
périmètre — Story 9.4/9.5. Ceci est une borne **mécanique** : un nombre de
runs, une taille de payload, rien de plus.

```bash
# Runs actuellement running par workflow, pour vérifier la marge sous le plafond :
psql -c "SELECT workflow_id, count(*) FROM workflow_runs WHERE status = 'running' GROUP BY workflow_id ORDER BY 2 DESC LIMIT 10;"
```
