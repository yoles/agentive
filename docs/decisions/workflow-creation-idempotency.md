# Idempotence de `POST /api/v1/workflows` (Story 4.8)

**Status:** Accepted (Sprint 2)
**Date:** 2026-09-12
**Story:** 4.8 — Durcissement de la création de workflow
**Origine:** `defer` #2 de la revue de code de la Story 4.1

L'AC1 de la Story 4.8 laissait le mécanisme explicitement ouvert
(« à trancher en Tech Spec — clé côté client, contrainte d'unicité, ou
déduplication serveur »). Cet ADR est ce document.

## Problème

`POST /api/v1/workflows` n'avait aucune idempotence. Un client dont la
requête a abouti côté serveur mais dont la réponse s'est perdue (timeout
réseau, coupure du proxy) n'avait pour tout recours que de rejouer — et
créait alors un second workflow, avec un second event
`workflow_engine.workflow.created` publié dans l'outbox. Rien dans le
schéma ne l'en empêchait : `workflows` n'a que sa clé primaire.

## Options

### A. Clé côté client (`Idempotency-Key`) — **rejetée**

La norme de l'industrie, et la plus expressive : elle distingue « le même
appel » de « le même contenu », et permet une fenêtre de déduplication
bornée dans le temps.

Rejetée parce que le scénario que l'AC décrit est un client qui **rejoue
son corps** après un timeout, sans mention d'une clé. Un mécanisme à clé ne
protège que les clients qui en émettent une : celui-là, non. Livrer une
garde qui ne couvre pas le cas qui l'a motivée serait fermer le `defer` sur
le papier seulement. S'y ajoute le coût : une table (clé → réponse) et sa
politique de rétention, que la Story 4.10 devrait ensuite balayer.

### B. Déduplication serveur par pré-`SELECT` — **rejetée**

Chercher une row d'empreinte identique avant d'insérer. Simple, et correct
en séquentiel.

Rejetée parce qu'elle échoue exactement sous la charge qui la rend
nécessaire : sous l'isolation `READ COMMITTED` de Postgres, deux
transactions concurrentes ne voient pas la row non committée l'une de
l'autre, et insèrent toutes les deux. C'est la même classe de course que la
note P-02 de `WorkflowRunRepo.get_by_id_in_session` décrit déjà — et cette
story ne pouvait pas en ouvrir une nouvelle dans le geste qui en ferme une
(voir l'AC2 et le verrou `FOR SHARE`).

### C. Contrainte d'unicité sur une empreinte de contenu — **retenue**

Le discriminant est le corps (ce que l'AC demande), la garantie est posée
par Postgres (donc vraie en concurrence), et le motif « INSERT d'abord,
collision ensuite » est déjà celui d'`AgentTemplateRepo.create_in_session`.

## Décision

`workflows.request_fingerprint` porte le SHA-256 hex de
`{name, dag}` (`WorkflowService._request_fingerprint`), sous l'index

```sql
CREATE UNIQUE INDEX uq_workflow_request_fingerprint
ON workflows (request_fingerprint, tenant_id) NULLS NOT DISTINCT
WHERE request_fingerprint IS NOT NULL
```

* `NULLS NOT DISTINCT` — sans lui, `tenant_id IS NULL` (mono-tenant MVP)
  rend l'index inopérant. C'est le bug que la migration `20260508000000` a
  dû corriger a posteriori sur `uq_agent_template`.
* `WHERE request_fingerprint IS NOT NULL` — les rows antérieures à la story
  n'ont pas d'empreinte, et sous `NULLS NOT DISTINCT` deux `NULL` sont
  **égaux** : un index total échouerait sur toute base portant **au moins
  deux** rows sans empreinte (une base à une seule row legacy passerait — la
  borne est à deux, pas à « non vide »). Vérifié en conditions réelles : sur
  une base portant deux rows legacy, la création d'un index total échoue
  (`could not create unique index / Duplicate keys exist`) là où l'index
  partiel passe.

L'index est également **déclaré** dans `Workflow.__table_args__`, en plus
d'être créé en SQL brut par la migration. Ce n'est pas une redondance
décorative : `alembic/env.py` pointe `target_metadata` sur `Base.metadata`,
donc un index présent en base et absent des modèles est émis en `DROP INDEX`
par le prochain `alembic revision --autogenerate` — ce qui désactiverait
l'idempotence sans le moindre signal. Une revue de code a par ailleurs
établi que l'argument initial (« SQLAlchemy ne sait pas exprimer
`NULLS NOT DISTINCT` ») était faux pour la version épinglée : 2.0.49 compile
`Index(..., postgresql_nulls_not_distinct=True, postgresql_where=...)` vers
exactement la chaîne ci-dessus, et un test unitaire compare les deux formes
caractère pour caractère.

**Sans `CONCURRENTLY` — mesuré, pas supposé (Story 4.14 AC3).** Un
`CREATE UNIQUE INDEX` ordinaire prend `ACCESS EXCLUSIVE` sur `workflows`
pour toute sa durée. Acceptable ici parce que le prédicat partiel ne matche
**aucune** row au moment de la construction (colonne tout juste ajoutée,
entièrement `NULL`) : le coût est celui d'un balayage de table, pas d'une
écriture d'index par ligne. Mesuré en conditions réelles sur ce dépôt :
13,6 ms sur 100 000 lignes, 46,7 ms sur 1 000 000 — négligeable. ⚠️ Ces
chiffres bornent la **détention** du verrou, pas son **acquisition** : le DDL
attend d'abord la fin de toute transaction conflictuelle sur `workflows`, en
bloquant tout ce qui arrive derrière lui — durée bornée par le bloqueur, pas
par ces 46,7 ms (revue 4.14, finding 10). Ce n'est
pas une propriété générale de toute migration d'index : un prédicat qui
matche une fraction significative des lignes existantes n'a pas cette
propriété et doit passer par `CREATE INDEX CONCURRENTLY`, confirmé
utilisable sans modification du harnais Alembic de ce dépôt (`autocommit_block()`,
cf `docs/runbooks/concurrent-index-migrations.md`).

Le service tente l'INSERT, le repo traduit l'`IntegrityError` en
`ConflictError` (les features n'importent pas `sqlalchemy` —
`import-linter` Contract 3), et la branche de rejeu relit la row par
empreinte dans une transaction neuve.

**L'absence de second event est structurelle, pas gardée** : `publish`
s'exécute après le `flush` de l'INSERT dans la même transaction, que la
violation d'unicité avorte entièrement. Aucune ligne de code ne « décide »
de ne pas publier — il n'y a rien à supprimer par inadvertance.

**Le rejeu répond `200 OK`, pas `201 Created`**, avec
`idempotent_replay: true` et le `workflow_id` de la row d'origine. Un `201`
annoncerait une création qui n'a pas eu lieu, et le client ne pourrait pas
distinguer « j'ai créé un workflow » de « j'ai retrouvé le mien ». C'est le
raisonnement déjà écrit dans `router.py` pour le `202`/`200` de
`cancel_workflow_run` : le code de statut est le message sur ce qui s'est
réellement passé.

## Conséquences assumées

1. **Ce n'est pas une fenêtre d'idempotence.** Un index unique permanent
   interdit **pour toujours** deux workflows de même `(name, dag)`, là où
   une clé d'idempotence n'aurait dédupliqué que pendant 24 h. Créer
   sciemment un doublon exige de changer le nom. Acceptable ici : le nom est
   déjà le discriminant humain d'un workflow, `agent_templates` vit avec le
   même choix, et deux workflows au nom **et** au DAG identiques ne sont
   distinguables par aucun humain ni aucune UI.
2. **L'empreinte porte sur la forme soumise, pas sur une forme normale.**
   `dag_payload` préserve l'ordre des `nodes`/`edges` du corps : deux corps
   sémantiquement équivalents mais ordonnés différemment sont deux
   workflows. Normaliser le DAG avant de hacher rendrait la déduplication
   plus forte et changerait la règle produit — « deux DAG isomorphes sont le
   même workflow » — ce que personne n'a demandé.
3. **`version` n'entre pas dans l'empreinte.** Elle vaut `1` pour toutes les
   rows (aucun chemin ne l'incrémente : il n'existe ni `PUT` ni `PATCH` sur
   `/workflows`). L'y mettre reviendrait à ajouter une constante au
   hachage. Le jour où le versioning devient réel, c'est **l'index**
   `(request_fingerprint, tenant_id)` qu'il faudra reconsidérer — pas la
   fonction de hachage.
4. **L'empreinte n'est jamais tronquée** (64 caractères), contrairement à
   `_template_fingerprints` qui coupe à `[:16]`. Ce voisin est un
   comparateur de diagnostic ; celui-ci est une clé d'unicité, et tronquer
   un hash utilisé comme clé fabrique des collisions — ici, rendre à un
   appelant le workflow de quelqu'un d'autre.
5. **Les workflows antérieurs à la migration sont exemptés à vie.** C'est le
   revers direct du prédicat partiel : une row à `request_fingerprint NULL`
   est hors index, donc elle ne peut **jamais** provoquer de collision. Un
   client qui rejoue la création d'un workflow d'avant la migration obtient
   un **second workflow et un second event**, avec `201` et
   `idempotent_replay: false` — rien ne le distingue d'une création
   légitime. C'est précisément l'échec que cette ADR existe pour empêcher,
   et il reste ouvert sur cette population. Assumé plutôt que corrigé parce
   que le backfill supposerait de reproduire en SQL, octet pour octet, le
   `json.dumps(sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
   de Python : une empreinte backfillée fausse serait pire que pas
   d'empreinte du tout, puisqu'elle *entrerait* dans l'index et pourrait y
   collisionner avec autre chose. La population ne grandit pas, et le trou
   se referme de lui-même à mesure que ces rows sortent d'usage. Si un jour
   il faut le fermer vraiment, la voie est un backfill **en Python** via le
   modèle ORM, pas une expression SQL.

## Vérification

- `tests/integration/workflow_engine/test_create_workflow_e2e.py` —
  `test_create_workflow_replayed_body_creates_one_row_and_one_event`
  (201 puis 200, un seul row, un seul event) et
  `test_create_workflow_two_concurrent_identical_requests_converge`, le seul
  test qui distingue le mécanisme retenu de l'option B rejetée : un rejeu
  séquentiel passerait aussi avec un pré-`SELECT`.
- `tests/unit/repositories/test_workflow_repo.py` — traduction
  `IntegrityError` → `ConflictError`, persistance de l'empreinte.
- `tests/unit/workflow_engine/test_service.py` — branche de rejeu, 409
  quand la row d'origine a disparu, stabilité et non-normalisation de
  l'empreinte.
