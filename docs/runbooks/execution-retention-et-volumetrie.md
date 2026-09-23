# Runbook — Rétention et volumétrie du chemin d'exécution (Story 4.10)

> Ce que le moteur d'exécution accumule sans jamais purger (blobs de
> checkpoint LangGraph), ce qui reste immortel par construction (les runs
> `paused`), et ce qui coûtait plus cher à mesure que l'historique grossissait
> (`routing-stats`).

## 1. Quand l'utiliser

- Le volume de `checkpoints`/`checkpoint_writes`/`checkpoint_blobs` inquiète
  (taille de la base, temps de sauvegarde).
- Un opérateur veut savoir si des runs `paused` ont été oubliés.
- `GET /workflows/{id}/routing-stats` répond lentement sur un workflow à
  fort volume, ou son chiffre a changé de sens après une mise à jour.
- Le balayage de recovery (`claim_stale_running`) est suspecté de ralentir
  à mesure que `workflow_runs` grossit.

## 2. Prérequis

- Accès `psql` (rôle `agentive_owner` pour l'introspection de plan).
- `AGENTIVE_API_TOKEN` pour les endpoints HTTP.

## 3. Rétention des checkpoints LangGraph (AC1)

`checkpoints`/`checkpoint_writes`/`checkpoint_blobs` (migration
`20260910_000001`) n'étaient jamais purgés par aucun code de ce dépôt — un
run terminé garde sa trace complète (chaque superstep, chaque output de
node non tronqué) indéfiniment. Le dimensionnement qui a motivé cette
story : 10 000 runs × 8 supersteps × 40 Ko ≈ 3 Go que plus aucun chemin de
code ne relit une fois le run terminé.

`CheckpointRetentionWorker` (`features/workflow_engine/retention.py`)
tourne une fois par jour (`AGENTIVE_WORKFLOW_CHECKPOINT_RETENTION_INTERVAL_S`,
défaut 86400s) et purge, via `AsyncPostgresSaver.adelete_thread(str(run.id))`
(le `thread_id` LangGraph EST l'UUID du run — `service.py`'s
`config["configurable"]["thread_id"] = str(run_id)`), tout run terminal
(`completed`/`error`/`cancelled`) dont la date de fin dépasse
`AGENTIVE_WORKFLOW_CHECKPOINT_RETENTION_DAYS` (défaut **90 jours**, même
précédent que `audit_events`).

« Date de fin » = `COALESCE(ended_at, last_checkpoint_at, started_at)` depuis
la **Story 4.15 AC3**, et non `ended_at` seul. Un run terminal dont `ended_at`
n'a jamais été estampillé était auparavant invisible pour la purge **à
jamais**, sans que rien ne le signale. Aucun site d'écriture applicatif ne
produit ce cas (vérifié) : c'est un trou défensif, désormais fermé. Quand le
repli est effectivement exercé, la passe émet
`workflow_engine.retention_terminal_run_without_ended_at` avec le `run_id` —
si vous le voyez, quelque chose a écrit un statut terminal en dehors des
chemins applicatifs.

`workflow_runs.checkpoint_purged_at` marque le travail fait — sans lui,
chaque passe re-sélectionnerait pour toujours tout run déjà purgé (une
`DELETE` sans effet, mais un `SELECT` qui grossit avec le temps). Jamais
remis à `NULL` : une purge n'est pas réversible.

**Décision T1.3 : worker dédié, pas un cron.** Ce dépôt n'a AUCUN mécanisme
de cron — `apscheduler` est présent au `pyproject.toml` mais réservé à
l'Epic 7 M11 Scheduler (décision G2, cf `ttl.py`). Un worker `asyncio` au
cycle quotidien (mirroir `MemoryArchivalWorker`, Story 3.3) est exactement
ce que ce dépôt sait déjà faire — introduire un cron système aurait ajouté
un second mécanisme d'ordonnancement pour un seul job.

```bash
# Combien de runs restent à purger, et depuis quand le plus vieux attend :
docker compose exec db psql -U agentive_owner -d agentive -c "
SELECT count(*), min(COALESCE(ended_at, last_checkpoint_at, started_at))
FROM workflow_runs
WHERE status IN ('completed','error','cancelled')
  AND COALESCE(ended_at, last_checkpoint_at, started_at) < now() - interval '90 days'
  AND checkpoint_purged_at IS NULL;"

# Threads orphelins en attente (Story 4.15 AC2) — ceux dont la ligne de run
# a disparu par CASCADE, qu'aucun autre chemin ne peut plus atteindre :
docker compose exec db psql -U agentive_owner -d agentive -c "
SELECT count(DISTINCT c.thread_id) FROM checkpoints c
WHERE NOT EXISTS (SELECT 1 FROM workflow_runs wr WHERE wr.id::text = c.thread_id);"

# Suivre les passes de purge en prod :
docker compose logs backend | grep workflow_engine.retention_run_completed
```

Le champ n'est PAS une preuve que les tables LangGraph sont vides pour ce
run — seulement que ce worker a tenté et réussi la purge. Pour vérifier
réellement :

```sql
SELECT count(*) FROM checkpoints WHERE thread_id = :run_id;  -- doit valoir 0
```

## 3bis. Plusieurs réplicas : le bail de purge

Le `CheckpointRetentionWorker` est démarré **dans chaque process** par
`app/lifespan.py`. Sans exclusion, N réplicas sélectionneraient les mêmes
lignes et lanceraient les mêmes `adelete_thread` — travail dupliqué et
contention sur `checkpoint_writes`/`checkpoint_blobs`.

La purge prend donc un **verrou consultatif Postgres de session**
(`pg_try_advisory_lock`) pour la durée de la passe. Un seul réplica purge ;
les autres journalisent `retention_purge_skipped_not_leader` et passent leur
tour. Rien à configurer, et le verrou est relâché par Postgres si le process
meurt — un réplica qui tombe ne peut pas bloquer la purge.

L'**alerte** sur les runs `paused` périmés est délibérément **hors du bail** :
c'est une lecture plus une ligne de log, donc l'émettre N fois est du bruit,
alors que la mettre sous bail signifierait qu'un réplica qui ne gagne jamais
le bail ne signale jamais rien.

```bash
# Qui purge réellement, sur un déploiement multi-réplicas :
docker compose logs backend | grep -E 'retention_run_completed|retention_purge_skipped_not_leader'
```

Pourquoi un verrou plutôt qu'une revendication de lignes : le marqueur
`checkpoint_purged_at` est écrit **après** la suppression, pour qu'un crash
entre les deux laisse le run éligible à un nouveau passage plutôt que marqué
purgé avec des blobs toujours là. Revendiquer les lignes d'avance échangerait
cette garantie contre l'exclusion ; le bail achète l'exclusion sans y toucher.

## 4. Runs `paused` immortels (AC2)

**Aucun mécanisme n'expire un run `paused`** — décision assumée (T2.1), pas
un oubli. `claim_stale_running` l'exclut délibérément (`status = 'running'`
seul), `ended_at` reste `NULL`, et rien d'autre dans ce dépôt n'y touche.

Trois symptômes concrets d'un run `paused` oublié :

1. Il épingle son thread LangGraph — `checkpoints` le garde indéfiniment
   tant qu'il n'est pas terminal (et `CheckpointRetentionWorker` ci-dessus
   ne purge que les runs TERMINAUX, jamais un `paused`).
2. Il compte dans `aggregate_routing_modes`/`GET /routing-stats` (§5) tant
   qu'il reste dans la fenêtre de temps interrogée.
3. Il retient un abonnement SSE jusqu'au plafond `_max_stream_duration_s()`
   (Story 4.9 AC6/T6.6, ~1h par défaut).

**Pourquoi une ALERTE et pas un TTL/une annulation automatique.** Un run
`paused` est un état légitime — "je corrige un namespace, je reprendrai la
semaine prochaine" — et l'annuler silencieusement serait une action
difficile à défaire que ce dépôt ne prend nulle part ailleurs sans
confirmation explicite d'un opérateur (cf la discipline `force`+`reason` de
la Story 4.5, ou le choix similaire de la Story 4.9 AC6 T6.3 de ne PAS
étendre la précédence "cancel opérateur" à `pause`). `CheckpointRetentionWorker`
loggue une alerte (`workflow_engine.stale_paused_runs_detected`, niveau
WARNING) quand au moins un run `paused` dépasse
`AGENTIVE_WORKFLOW_PAUSED_RUN_ALERT_AFTER_DAYS` (défaut **7 jours**) sans
activité — mesurée sur `last_checkpoint_at` (le meilleur proxy disponible :
il n'existe pas de colonne `paused_at`, et le dernier checkpoint réel d'un
run précède immédiatement le moment où le driver règle une pause en
attente).

```bash
docker compose logs backend | grep workflow_engine.stale_paused_runs_detected

# Lister les runs concernés :
docker compose exec db psql -U agentive_owner -d agentive -c "
SELECT id, workflow_id, COALESCE(last_checkpoint_at, started_at) AS stale_since
FROM workflow_runs WHERE status = 'paused'
  AND COALESCE(last_checkpoint_at, started_at) < now() - interval '7 days'
ORDER BY stale_since;"
```

Reprendre (`POST /workflows/runs/{id}/resume`) ou annuler
(`POST /workflows/runs/{id}/cancel`) manuellement — ce runbook ne fait
qu'alerter, jamais agir à la place de l'opérateur.

> **« Alerte » = une ligne de log, pas une métrique.** Cette application
> n'expose aucun endpoint `/metrics`, donc il n'y a pas de compteur à
> brancher sur une supervision et rien ne remontera tout seul : la détection
> passe par le `grep` ci-dessus, ou par l'expédition de logs si le
> déploiement en a une. Dit autrement, un run `paused` oublié reste
> **immortel par conception** — il devient seulement *visible*.

## 5. Bornage de `routing-stats` (AC4)

`GET /workflows/{id}/routing-stats` agrégeait `metrics` sur **tous** les
runs d'un workflow, sans fenêtre — un coût proportionnel à un nombre que
l'APPELANT contrôle (combien de fois il a lancé `POST /runs`), pas
l'opérateur. Un workflow à 200 000 runs payait un scan complet à chaque
appel.

**Décision T4.1 : fenêtre temporelle glissante, pas un rollup matérialisé.**
Plus simple, et honnête sur ce que change la mesure : le ratio n'est plus
une proportion depuis toujours, mais sur les `window_days` derniers jours
— la réponse le dit explicitement (`window_days` dans le corps), pour
qu'un consommateur n'ait pas à deviner.

```bash
# Fenêtre par défaut (AGENTIVE_WORKFLOW_ROUTING_STATS_WINDOW_DAYS, 90j) :
curl -sS "$BASE/workflows/$WORKFLOW_ID/routing-stats" -H "$AUTH" | jq

# Fenêtre explicite (1-3650 jours, 422 hors bornes) :
curl -sS "$BASE/workflows/$WORKFLOW_ID/routing-stats?window_days=30" -H "$AUTH" | jq
```

`GET /workflows/{id}/handoff-stats` (Story 4.7 AC3) a la même forme
d'agrégation non bornée et n'a PAS été touché par cette story — l'AC ne
nommait que `routing-stats`. Défer implicite, à traiter si le même symptôme
apparaît là.

## 6. Index du balayage de recovery (AC3)

`claim_stale_running` (Story 4.2 AC3) filtre `status = 'running'` et trie
sur `COALESCE(last_checkpoint_at, started_at)`, toutes les 30s, par
réplique. Avant cette story, `workflow_runs` n'avait qu'un index sur
`workflow_id` — chaque tick payait un seq scan + tri complet une fois la
table non triviale.

`ix_workflow_runs_stale_running` (migration `20260913_000001`, procédure
`CREATE INDEX CONCURRENTLY` documentée dans
[`concurrent-index-migrations.md`](./concurrent-index-migrations.md)) est
PARTIEL (`WHERE status = 'running'`) et D'EXPRESSION (sur le `COALESCE`
exact que la requête trie) — déclaré aussi dans
`infra/db/models.py::WorkflowRun.__table_args__`, pour que `Base.metadata`
décrive le schéma attendu.
⚠️ Cette déclaration ne protège **pas** d'un `DROP INDEX` émis par un futur
`alembic revision --autogenerate` — revendication retirée par la Story 4.15
AC4 : Alembic ne compare de façon fiable ni les index d'expression ni les
prédicats partiels, et c'est précisément ce qu'est cet index. La protection
est **procédurale** : relire à la main toute migration autogénérée qui le
touche (cf `concurrent-index-migrations.md`, point 2).

```sql
-- Confirmer que le planificateur choisit bien l'index (pas seulement qu'il existe) :
EXPLAIN SELECT id FROM workflow_runs
WHERE status = 'running'
  AND COALESCE(last_checkpoint_at, started_at) < now() - interval '1500 seconds'
ORDER BY COALESCE(last_checkpoint_at, started_at) LIMIT 50;
-- Attendu : "Index Scan using ix_workflow_runs_stale_running"
```

## 7. Rollback

Les deux migrations de cette story sont additives et symétriques :

```bash
docker compose run --rm backend uv run alembic downgrade -1  # retire l'index (CONCURRENTLY)
docker compose run --rm backend uv run alembic downgrade -1  # retire checkpoint_purged_at
```

Retirer `checkpoint_purged_at` ne perd aucune information reconstructible :
la colonne ne fait que mémoriser un travail déjà effectué (les blobs
correspondants sont, eux, réellement supprimés et non récupérables — ce
n'est pas la colonne qui les protège).

## 8. Vérifications

```sql
-- Volume de checkpoints déjà purgé vs restant :
SELECT
  count(*) FILTER (WHERE checkpoint_purged_at IS NOT NULL) AS purged,
  count(*) FILTER (
    WHERE checkpoint_purged_at IS NULL
      AND status IN ('completed','error','cancelled')
      AND ended_at < now() - interval '90 days'
  ) AS pending
FROM workflow_runs;
```

```bash
# Les deux passes du worker dans les logs, chaque jour :
docker compose logs backend | grep workflow_engine.retention_run_completed
```

---

## Troisième passe — threads orphelins (Story 4.15 AC2)

`list_purgeable` joint sur `workflow_runs`, et `workflow_runs` cascade depuis
`workflows` (`ON DELETE CASCADE`). **Supprimer un workflow emporte donc ses
runs, et laisse leurs blobs LangGraph inatteignables par tout chemin de
découverte** — exactement la croissance non bornée que la passe AC1 existe
pour fermer, atteinte par une porte que sa conception ne regarde pas.

Une troisième passe balaie ces threads, **sous le même bail consultatif** que
la purge nominale (elle supprime aussi, donc N réplicas se contendraient sur
`checkpoint_writes`/`checkpoint_blobs`).

Trois propriétés qu'un opérateur doit connaître **avant** de supprimer une
ligne `workflows` à la main :

1. **Aucune fenêtre de rétention ne s'applique à cette passe.** Un thread
   orphelin n'a plus de run contre lequel lire sa trace : il est mort dès que
   sa ligne disparaît, et il est purgé au prochain passage — pas 90 jours
   après. Si vous voulez garder la trace d'un run, ne supprimez pas son
   workflow.
2. **La découverte couvre les trois tables** (`checkpoints`, `checkpoint_blobs`,
   `checkpoint_writes`), pas seulement la première : une purge interrompue en
   cours de route laisserait sinon des résidus qu'aucune requête ne verrait
   plus.
3. **Un plafond de rayon de souffle refuse la passe entière** au-delà de 500
   candidats, avec un ERROR. Cette passe agit sur un prédicat **négatif** —
   « aucune ligne de run visible pour ce thread » — et supprime : si le
   prédicat perd sa référence (RLS masquant des lignes au passage
   multi-tenant, snapshot de réplica en retard), *tout* paraît orphelin. Le
   plafond transforme ce cas en refus et en ligne de log.

```bash
# Les cinq events de cette passe :
docker compose logs backend | grep -E "retention_orphan_(threads_purged|no_progress|saturated|failure_cap_reached|refused_blast_radius)"
```

> ⚠️ **Prérequis multi-tenant.** Tant que toutes les lignes portent
> `tenant_id IS NULL`, le prédicat est exact. Dès qu'un `tenant_id` non nul
> existe, la politique RLS `tenant_isolation` rend la ligne invisible à la
> session `with_tenant(None)` de cette passe, et le thread d'un run **vivant**
> paraît orphelin. Le plafond borne les dégâts ; il ne rend pas la passe
> correcte. Fermer ce point est un prérequis de la **Story 4.9 AC1**, où il
> est enregistré à côté de l'inventaire des sites.
