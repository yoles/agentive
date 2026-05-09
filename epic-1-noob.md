# Epic 1 expliqué (version noob) — Foundation & Spike Validation

> **Pour qui ce document ?**
> Toi qui découvres Agentive et qui veux comprendre ce qui se passe **avant** que la plateforme produise son premier truc utile. On va parler simplement, avec des analogies, et t'expliquer chaque brique sans présumer que tu connais Postgres, FastAPI ou MCP.
>
> **Spoiler** : à la fin de l'Epic 1, Agentive ne fait encore **rien** de visible côté agents IA. Mais sans cet epic, tout le reste s'effondre. C'est les fondations.

---

## État au moment d'écrire ce doc

**Les 9 stories de l'Epic 1 sont livrées.** Tu peux le vérifier avec `git log --oneline | grep "Story 1\."` :

```
0ea1665  Story 1.9 — Observability + Caddy + CI
a83a346  Story 1.8 — Design system foundation (shadcn + Geist + ModeToggle)
34ef300  Story 1.7 — Core auth token statique
057aeb3  Story 1.6 — Core LLM abstraction (multi-provider)
6a4e824  Story 1.5 — Core Repositories + RLS tenant binding
051f128  Story 1.4 — Core Event Bus (LISTEN/NOTIFY + outbox)
b708774  Story 1.3 — benchmark M4 pgvector HNSW
998ab27  Story 1.2 — spike M3 LangGraph
34bfecf  Story 1.1 — Docker-first scaffolding
```

Ce que tu liras au futur ("le système peut…") est en fait au passé — **tout ça tourne déjà**. Le doc est rédigé en mode "spec" pour que tu comprennes le **pourquoi** des décisions.

**Où trouver les livrables** :
- **ADRs** (Architecture Decision Records, les décisions actées) → `docs/decisions/` (9 fichiers)
- **Runbooks** (modes d'emploi opérationnels) → `docs/runbooks/` (7 fichiers)
- **Code de spike** (Story 1.2) → `backend/spike/`
- **Bench artifacts** (Story 1.3) → `backend/_bench_artifacts/`
- **Migration initiale** → `backend/alembic/versions/20260419_000000_initial.py`
- **CI workflows** → `.github/workflows/` (ci, deploy-staging, sbom, security)

---

## Important — Spec vs Implémentation : "Docker-first"

La PRD/Archi parlait de `mise` (gestion versions) et `just` (task runner). **Au moment du scaffolding, le choix a été fait d'aller 100% Docker-first** :

| Spec d'origine | Choix fait | Pourquoi |
|---|---|---|
| `mise.toml` pour pinner Python 3.13 / Node 22 / uv | Versions pinnées dans les Dockerfiles | Zéro install sur la machine hôte, reproductibilité absolue |
| `just dev` / `just test` / `just bench` | `make dev` / `make test` / `make bench` | `make` est universel, déjà installé partout |
| Scaffolding mixte (host + Docker) | `make init` lance des containers éphémères qui scaffoldent backend + frontend | Tu peux bootstrap le projet sans Python ni Node sur ta machine |

Conséquence pratique : **toutes les commandes du Makefile passent par Docker**. Tu ne lances jamais `pytest` directement, mais `make test-backend` (qui lance `pytest` dans le container backend). Idem pour `npm`, `uv`, `alembic`.

`make help` te liste toutes les commandes disponibles.

---

## 1. C'est quoi un "Epic" déjà ?

Un **Epic** = un gros morceau de produit, qui regroupe plusieurs **stories** (petits bouts de boulot). Une story = un truc qu'on peut livrer en 1 à quelques jours, avec des **critères d'acceptation** clairs ("c'est terminé quand X, Y, Z").

L'Epic 1 contient **9 stories**. Tu les valides une par une. Quand les 9 sont vertes, l'epic est fini.

---

## 2. À quoi sert l'Epic 1 — l'analogie de la maison

Imagine que tu construis une maison.

- **Epic 1** = creuser les fondations, couler la dalle, faire arriver l'eau/électricité, monter les murs porteurs, vérifier que le sol porte le poids prévu.
- **Epic 2** = installer les pièces (chambres, cuisine).
- **Epic 3+** = mettre les meubles (les agents IA), la déco, etc.

Si tu sautes l'Epic 1 et que tu poses directement les meubles, tu te retrouves à devoir tout déplacer dans 3 mois quand tu réalises que le sol s'affaisse. C'est ce qu'on veut **éviter** ici.

L'Epic 1 a deux missions :

1. **Construire le squelette technique** (le scaffolding, les services partagés, le design system, la sécurité de base).
2. **Valider deux risques techniques majeurs** via des "spikes" (tests grandeur nature) — parce qu'on a basé toute l'archi sur deux paris technologiques, et qu'il faut les confirmer **avant** de bâtir dessus.

Les deux paris :
- **Pari A** : *LangGraph* (le moteur d'orchestration des agents) gère bien les 3 trucs critiques dont on a besoin (checkpointing, scatter-gather, human-in-the-loop).
- **Pari B** : *pgvector* (la techno qui stocke la mémoire vectorielle dans Postgres) tient bien les performances qu'on a promises (< 200ms pour retrouver un truc parmi 10 000).

Si un des deux paris échoue → on **ne lance pas le Sprint 1**, on pivote. C'est ce qu'on appelle un **gate**.

---

## 3. Vue d'ensemble — les 9 stories rangées en 3 groupes

```
┌─────────────────────────────────────────────────────────────────┐
│  GROUPE 1 — LA STRUCTURE DE BASE                                │
│  (le terrain et les murs porteurs)                              │
│                                                                 │
│  Story 1.1  Scaffolding minimal viable                          │
│  Story 1.5  Core repositories + migrations + RLS                │
│  Story 1.4  Core event bus                                      │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  GROUPE 2 — LES VALIDATIONS DE PARI (les "spikes")              │
│  (on teste que le sol porte le poids prévu)                     │
│                                                                 │
│  Story 1.2  Spike LangGraph (M3) — pari A                       │
│  Story 1.3  Benchmark pgvector (M4) — pari B                    │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  GROUPE 3 — LES SERVICES TRANSVERSES                            │
│  (eau, électricité, serrures, peinture)                         │
│                                                                 │
│  Story 1.6  Core LLM abstraction (interrupteur universel)       │
│  Story 1.7  Core auth (la serrure)                              │
│  Story 1.8  Design system (la déco baseline)                    │
│  Story 1.9  Observability + Caddy + CI (caméras, portier, QC)   │
└─────────────────────────────────────────────────────────────────┘
```

**Sprint cible** : Sprint 0. Tout l'Epic 1 doit être bouclé avant de démarrer le Sprint 1.

---

## 4. Story par story

### Story 1.1 — Scaffolding minimal viable des 3 stacks

**En une phrase :** monter le squelette du projet (frontend + backend + base de données) pour qu'on puisse lancer toute l'application avec **une seule commande**.

#### Pourquoi c'est important

Avant d'écrire du code "métier" (les agents, la mémoire, etc.), il faut un **chantier prêt** où chaque outil sait où aller. Sinon, chaque dev (et chaque agent IA qui contribuera plus tard !) va organiser les fichiers à sa sauce, et au bout de 3 mois c'est l'enfer.

#### Comment ça marche concrètement

3 stacks démarrent ensemble via Docker Compose :

| Stack | Rôle | Techno |
|---|---|---|
| **Frontend** | L'interface web (4 espaces : Dashboard, Chat, Trace, Config) | React 19 + TypeScript + Vite + Tailwind v4 + shadcn/ui v4 |
| **Backend** | L'API qui fait tourner les agents | FastAPI (Python 3.14) avec `uv` pour gérer les dépendances |
| **Base de données** | Stockage de tout (mémoire, logs, configs) | PostgreSQL 17 + extension `pgvector` (pour stocker des embeddings) |
| **Reverse proxy** | HTTPS local + security headers (Story 1.9) | Caddy (`https://localhost:8443`) |

Tu clones le repo, tu tapes `make init` (une fois) puis `make dev`, et **en moins de 60 secondes** :
- Le frontend tourne sur `localhost:5173` (et `https://localhost:8443` via Caddy)
- Le backend répond sur ses endpoints `/health` et `/ready`
- La base Postgres est créée avec ses extensions (`pgvector`, `pgcrypto`) et ses index
- Caddy est up avec un cert self-signed en dev

Variante : `make dev-host` expose le frontend sur le LAN (0.0.0.0:5173) — pratique pour tester depuis ton tel.

#### La structure de dossiers (importante !)

L'organisation est **feature-based** : chaque module (M2, M3, M4...) aura son propre dossier dans `backend/src/agentive_backend/features/`. Pour l'instant ce dossier est **vide**, mais prêt :

```
backend/
├── src/
│   └── agentive_backend/          ← le package Python (src layout)
│       ├── shared/                ← code commun
│       │   ├── auth/              ← Story 1.7 (token + bcrypt)
│       │   ├── event_bus/         ← Story 1.4 (LISTEN/NOTIFY + outbox)
│       │   ├── repositories/      ← Story 1.5 (Repository Pattern)
│       │   ├── llm/               ← Story 1.6 (LLMProvider abstraction)
│       │   ├── logging/           ← Story 1.9 (structlog + redaction)
│       │   ├── metrics/           ← Story 1.9 (OTel + Prometheus)
│       │   ├── correlation.py     ← propagation X-Correlation-ID
│       │   ├── exceptions.py      ← RFC 7807 Problem Details
│       │   └── feature_flags/
│       ├── features/              ← VIDE — accueille M2 à M12 (Sprints 1-3)
│       ├── api/                   ← routers FastAPI (admin/, deps.py)
│       ├── infra/                 ← adapters DB, LLM, MCP
│       │   ├── db/
│       │   ├── llm/               ← AnthropicProvider, OpenAIProvider
│       │   └── mcp/               ← (sera rempli en Sprint 1)
│       └── app/                   ← application factory FastAPI
├── alembic/versions/              ← migrations DB
├── spike/                         ← Story 1.2 — code du spike LangGraph
├── tests/
├── _bench_artifacts/              ← Story 1.3 — résultats benchmarks (CSV)
└── pyproject.toml                 ← géré par uv

frontend/
└── src/
    ├── app/
    │   ├── routes/                ← TanStack Router file-based
    │   │   ├── __root.tsx
    │   │   ├── index.tsx
    │   │   ├── dashboard/
    │   │   ├── chat/
    │   │   ├── trace/
    │   │   └── config/
    │   ├── routeTree.gen.ts       ← généré, ne pas éditer
    │   ├── ErrorBoundary.tsx
    │   └── providers.tsx
    ├── shared/
    │   ├── components/
    │   │   ├── ui/                ← shadcn primitives copiées
    │   │   └── layouts/           ← AppLayout, sidebar, ModeToggle
    │   ├── api/                   ← types générés (openapi-typescript)
    │   ├── hooks/
    │   ├── lib/
    │   └── state/
    ├── features/                  ← (theme uniquement pour l'instant)
    ├── styles/globals.css         ← tokens CSS
    └── main.tsx
```

#### Trucs annexes mais utiles

- **Versions pinnées dans les Dockerfiles** (Python 3.14 backend, Node 24 frontend). Pas de `mise.toml` — choix Docker-first.
- **Pre-commit hooks** : avant chaque commit, on lance automatiquement `gitleaks` (cherche des secrets oubliés), `ruff` (lint Python) et `eslint` (lint JS). Si ça détecte un truc, **commit refusé**.
- **`.import-linter`** (~9 KB de règles) : empêche les imports croisés interdits entre modules — appliqué en CI.

#### Comment vérifier que c'est bon

- `make dev` démarre tout en < 60s
- `make ps` montre tous les services healthy
- Tu push une fausse clé API en commit → gitleaks bloque
- `make help` te liste toutes les commandes Makefile

---

### Story 1.2 — Spike M3 LangGraph (gating critique, pari A)

**En une phrase :** prouver que LangGraph (le moteur d'orchestration choisi) tient ses promesses sur **3 fonctionnalités critiques** avant de l'adopter.

#### Pourquoi c'est important

Toute l'architecture d'Agentive repose sur le fait que **LangGraph** sait faire 3 choses :

1. **Checkpointing** : si le serveur crashe au milieu d'un workflow, on doit pouvoir **reprendre où on en était**, pas tout recommencer. C'est non négociable quand un workflow tourne 10 minutes et coûte 5€ en appels LLM.
2. **Scatter-gather** : envoyer une tâche à plusieurs agents en parallèle puis consolider les résultats. C'est la base du "recrutement dynamique" en Growth.
3. **Human-in-the-loop (HITL)** : un agent fait un truc, **suspendre** l'exécution, attendre que John valide dans le Chat, puis **reprendre** avec sa décision (`approve` ou `reject + feedback`).

Si LangGraph ne sait **pas** faire l'un de ces trois trucs nativement, on devra coder une couche custom → semaines de boulot supplémentaires → tout le planning explose.

#### Le concept de "spike"

Un **spike** = un mini-prototype dont le seul but est de répondre à une question technique : "est-ce que ça marche comme prévu ?". On le code vite, on le teste, on le jette ou on le garde comme référence. Pas de polish, pas de tests exhaustifs.

Ici le spike = un workflow minimal **2 agents (Producer + Reviewer) + 1 quality gate** :

```
[ Agent Producer ] → produit un truc → [ Quality Gate ] → si OK → terminé
                                                       ↘ si HITL → suspend → John valide → reprend
```

#### Comment vérifier (les 9 modes du spike)

Le code vit dans `backend/spike/` et s'invoque via Make. **9 modes** disponibles, chacun teste un aspect :

| Commande | Ce que ça teste |
|---|---|
| `make spike-m3` | Workflow basique Producer → QualityGate → Reviewer (auto-bascule MockLLM si pas de clé Anthropic) |
| `make spike-m3-mock` | Force MockLLM (zéro coût, déterministe) |
| `make spike-m3-real` | Force Anthropic réel (échoue si pas de clé) |
| `make spike-m3-crash` | Crash post-Producer (`kill -9`) — persiste le `thread_id` pour le resume |
| `make spike-m3-resume` | Reprise du thread crashé — **re-exécute pas** le Producer (preuve du checkpointing) |
| `make spike-m3-scatter` | Scatter-gather : 3 summarizers en parallèle via `Send` LangGraph |
| `make spike-m3-inspect` | Inspecte le checkpoint Postgres pour un `thread_id` donné |
| `make spike-m3-test` | Suite pytest dédiée au spike |

Critères validés au passage :
- Workflow termine en < 30s en mode mock
- Crash + resume = pas de double exécution du Producer
- Quality gate human-in-the-loop : `resume(approval=True)` reprend, `resume(approval=False, feedback=...)` renvoie au Producer
- Checkpoints **persistés dans Postgres**, format JSON structuré, lisible et migrable

#### Le résultat actuel : pari A confirmé ✓

Documenté dans **`docs/decisions/m3-spike-result.md`** (l'ADR qui acte le go).

> Si un des 3 critères avait échoué → ADR documentant le pivot (abstraction custom, autre framework). Sprint 1 ne démarre pas tant que le pivot n'est pas validé.

Runbook pratique pour inspecter un checkpoint en cours de dev : **`docs/runbooks/m3-checkpoint-inspect.md`**.

---

### Story 1.3 — Benchmark M4 pgvector HNSW (gating NFR5, pari B)

**En une phrase :** prouver que la recherche vectorielle dans Postgres est suffisamment **rapide** pour la mémoire des agents.

#### Pourquoi c'est important

Les agents Agentive ont une **mémoire** : pour répondre à une question, ils consultent des chunks (morceaux) de mémoire pertinents. C'est ce qu'on appelle du **RAG** (Retrieval-Augmented Generation).

Pour retrouver les chunks pertinents, on calcule la **similarité vectorielle** entre la requête et chaque chunk stocké. Avec 10 000 chunks et une logique naïve, ça prend des secondes — inutilisable.

La solution : un **index HNSW** (Hierarchical Navigable Small World), qui transforme la recherche en O(log n). En théorie, c'est rapide. En pratique, ça dépend de **3 paramètres** (`m`, `ef_construction`, `ef_search`) qu'il faut **tuner**.

L'objectif (NFR5) : **< 200ms p95** sur 10k chunks avec reranking. Si on n'y arrive pas avec pgvector, faut envisager un autre moteur (Qdrant, Weaviate...) → décision lourde.

#### Petit lexique

- **Embedding** : transformation d'un texte en vecteur de N dimensions (384, 1536...). Deux textes "proches" sémantiquement ont des vecteurs proches.
- **Reranking** : étape qui **affine** les résultats du HNSW avec un modèle plus précis (mais plus lent), appliqué uniquement aux top-K.
- **Recall@K** : sur un set de tests, % des résultats vraiment pertinents qui apparaissent dans les K premiers résultats. On veut > 90% sur K=5.
- **p95 latence** : 95% des requêtes sont plus rapides que cette valeur.

#### Comment ça marche concrètement

5 commandes Make orchestrent le bench. Les résultats CSV atterrissent dans `backend/_bench_artifacts/` ; le rapport final est généré dans `docs/decisions/hnsw-tuning.md`.

| Commande | Durée | Ce que ça fait |
|---|---|---|
| `make bench` | rapide | Baseline : 10 000 chunks synthétiques + ground truth + mesure de référence |
| `make bench-hnsw` | ~25 min | **Sweep complet HNSW** : 27 combos `m × ef_construction × ef_search` |
| `make bench-hnsw-fast` | ~10 min | Sweep réduit (8 combos) — version utilisée en CI |
| `make bench-ivfflat` | ~10 min | Comparaison `ivfflat` (9 combos `lists × probes`) |
| `make bench-aggregate` | rapide | Agrège les CSV HNSW + ivfflat → `comparison_hnsw_vs_ivfflat.csv` |
| `make bench-report` | rapide | Agrège **et** génère l'ADR `docs/decisions/hnsw-tuning.md` |

Paramètres testés :
- `m ∈ {8, 16, 32}` — connectivité des nœuds
- `ef_construction ∈ {64, 128, 256}` — qualité de construction (lent, 1 fois)
- `ef_search ∈ {40, 100, 200}` — qualité de recherche (à chaque requête)

Métriques mesurées : recall@5, recall@10, latence p50/p95, build time, taille index sur disque.

Embeddings testés en deux dimensions : 384 (modèles locaux type all-MiniLM) et 1536 (cloud type text-embedding-3-small).

#### Le résultat actuel : pari B confirmé ✓

- **ADR** : `docs/decisions/hnsw-tuning.md` documente les paramètres finaux retenus avec tradeoffs (recall vs latence vs build time).
- **Bench result** : `docs/decisions/m4-bench-result.md` documente le go/no-go.
- **Runbook** : `docs/runbooks/m4-bench-rerun.md` explique comment re-jouer un bench si tu changes de modèle d'embedding ou de version Postgres.

> Si la latence p95 avait dépassé 200ms même après tuning → ADR documentant l'ajustement (`shared_buffers` / `work_mem` Postgres) ou changement d'indexation (Qdrant, Weaviate), **avant** Sprint 1.

---

### Story 1.4 — Core event bus (le système nerveux)

**En une phrase :** mettre en place le canal de communication interne entre tous les modules, qui ne perd **jamais** un message même si un service crashe.

#### Pourquoi c'est important

Les modules d'Agentive (M2, M3, M4...) ne s'appellent **pas** directement les uns les autres (`from m4 import ...`). À la place, ils s'envoient des **événements** sur un bus :

```
M3 (workflow engine) ──"workflow_completed"──┐
                                              ├──→ M6 (Dashboard) met à jour
                                              ├──→ M9 (audit) log l'event
                                              └──→ M11 (Scheduler) déclenche la suite
```

**Avantage** : tu peux brancher/débrancher des modules sans casser le reste. C'est ce qui permet la modularité du Company Builder.

**Le piège** : si un consommateur crashe au moment où un événement arrive, l'événement est perdu. C'est bloquant pour des trucs critiques (un audit log perdu = problème de conformité).

#### La solution : Outbox Pattern + LISTEN/NOTIFY

- **Outbox Pattern** : quand un module veut publier un événement, il l'**INSERT dans une table `outbox_events`** dans la **même transaction** que sa modification métier. Soit les deux sont commitées, soit aucune. Pas de risque qu'un event soit publié sans que la modif soit en base.
- **`NOTIFY` Postgres** : juste après le commit, Postgres envoie un signal aux workers connectés via `LISTEN`. Ils traitent les events de l'outbox un par un.
- Si un worker crashe avant de traiter → au redémarrage, il lit `WHERE processed_at IS NULL` et **rejoue** les events manqués.
- Quand traité → il marque `processed_at = NOW()`.

#### Le `correlation_id` (très important)

Chaque chaîne d'événements partage un `correlation_id` unique (UUID v7 ou ULID). Ce ID se propage à travers :
- Tous les events publiés
- Les logs structurés (structlog)
- Les spans OpenTelemetry

Ça veut dire que dans le **Trace Explorer (M12)**, tu peux **suivre une requête utilisateur** à travers 12 modules en une seule recherche. C'est la base du debug.

#### Comment vérifier

- Module A publie un event → table `outbox_events` contient une nouvelle ligne dans la même transaction.
- Worker crashe → relance → events `processed_at IS NULL` rejoués.
- Module B consomme un event publié par A **sans import direct** de A (vérifié en CI par `import-linter`).

#### Pour aller plus loin

- **ADR** `docs/decisions/event-bus-naming.md` : la convention de nommage des events (verbes au passé : `workflow_completed`, pas `complete_workflow`).
- **ADR** `docs/decisions/event-bus-migration-trigger.md` : à quel moment basculer de `LISTEN/NOTIFY` vers Redis Streams (seuils chiffrés : nb consumers, latence, débit).
- **Runbook** `docs/runbooks/event-bus-debug.md` : comment inspecter l'outbox, debugger un event coincé, replay manuel.

---

### Story 1.5 — Core repositories + migrations tenant-ready + RLS

**En une phrase :** centraliser tous les accès à la base dans un seul endroit (les "repositories"), et préparer dès maintenant la **multi-tenancy** future à coût quasi-nul.

#### Pourquoi c'est important

**Problème classique** : chaque module fait ses propres requêtes SQL. Au bout de 6 mois, t'as 47 requêtes différentes pour récupérer un user, certaines bugguées, certaines lentes, et changer le schéma de la table `users` devient un cauchemar.

**Solution** : Repository Pattern. Une seule classe `UserRepo` qui expose des méthodes claires (`get_by_id`, `create`, etc.). Tous les modules passent par elle.

#### Les entités gérées dès Sprint 0

| Table | Rôle |
|---|---|
| `users` | Liste des utilisateurs (1 ligne au MVP : John) |
| `workflows` | Définitions de workflows |
| `workflow_runs` | Exécutions individuelles de workflows |
| `agent_templates` | Templates des 8 archétypes |
| `agent_instances` | Agents instanciés (Code Producer du Pôle Dev, etc.) |
| `memory_chunks` | Chunks de mémoire (texte) |
| `chunk_embeddings` | Vecteurs des chunks |
| `audit_events` | Log immutable de toutes les actions |

#### Le truc clé : `tenant_id` nullable partout

Toutes ces tables ont une colonne `tenant_id UUID NULL` **dès maintenant**, alors que le MVP est mono-utilisateur (single-tenant).

**Pourquoi ?** Parce que la PRD prévoit potentiellement un pivot SaaS plus tard. Si on ajoute `tenant_id` aujourd'hui (coût : ~30 lignes), on évite une réécriture massive si jamais ce pivot arrive. Si jamais il n'arrive pas → on perd 30 lignes, c'est pas grave.

#### Row Level Security (RLS)

Postgres a une fonctionnalité native : **Row Level Security**. Tu lui dis "pour cette table, ne retourne que les lignes où tenant_id = celui de l'utilisateur actuel". C'est appliqué **au niveau du moteur SQL**, donc même un bug applicatif qui oublie un `WHERE tenant_id = ...` est rattrapé.

```sql
CREATE POLICY tenant_isolation ON memory_chunks
  USING (tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id', true)::uuid);
```

À chaque transaction, le repository fait `SET LOCAL app.tenant_id = :tenant_id` et le RLS s'applique tout seul.

#### Async only

Tous les repos sont **async** (Python `asyncio`). Pourquoi ? Parce que FastAPI est async natif, et les agents font plein d'I/O (DB, LLM, MCP) en parallèle.

#### Les 3 rôles Postgres (principe du moindre privilège)

La migration crée **3 rôles distincts**, pas un seul :

| Rôle | Usage | Permissions |
|---|---|---|
| `agentive_owner` | Migrations Alembic, propriétaire des objets | ALL sur tables applicatives |
| `agentive_app` | L'application FastAPI au runtime | SELECT/INSERT/UPDATE/DELETE sur tables métier ; **aucun droit sur `audit_events`** |
| `agentive_audit_admin` | Écritures audit + lecture | INSERT-only sur `audit_events` parent ; SELECT sur partitions |

**Pourquoi cette séparation ?** Si un attaquant compromet l'application (`agentive_app`), il **ne peut pas** modifier ou supprimer l'audit trail. C'est de la défense-en-profondeur : l'audit log devient un témoin fiable même en cas de compromission applicative.

#### `audit_events` : partitionnée et immutable

La table `audit_events` est :
- **Partitionnée par mois** (Postgres native partitioning) → rétention ≥ 90 jours, archivage facile
- **Immutable** : `REVOKE DELETE, UPDATE` au niveau Postgres → personne (pas même `agentive_owner` au runtime) ne peut altérer un audit log

#### Comment vérifier

- Toutes les tables existent avec `tenant_id NULL`.
- `import-linter` bloque en CI tout module qui importerait directement `AsyncSession` (ils doivent passer par les repos).
- 1 ligne `users` (John) est insérée par la migration.
- Les 3 rôles PG existent (`\du` en `psql`).
- Tentative `DELETE FROM audit_events` échoue avec un permission denied.

#### Pour aller plus loin

- **ADR** `docs/decisions/repository-pattern.md` : pourquoi Repository Pattern, comment l'étendre.
- **Runbook** `docs/runbooks/repositories-usage.md` : exemples concrets pour les futurs modules.

---

### Story 1.6 — Core LLM abstraction (multi-provider)

**En une phrase :** wrapper Anthropic et OpenAI derrière une interface unique, avec **fallback automatique** si l'un est down.

#### Pourquoi c'est important

Les agents font des appels LLM. Si on importe directement `anthropic.Anthropic()` partout, et que dans 6 mois on veut tester Cohere ou Mistral, on doit modifier **chaque fichier**. Pire : si Anthropic a une panne, tout Agentive est down.

**Solution** : une interface `LLMProvider` (un "protocol" Python — un genre d'interface) avec deux implémentations concrètes (`AnthropicProvider`, `OpenAIProvider`). Les agents appellent **toujours** l'interface, jamais le SDK directement.

#### L'API

```python
class LLMProvider(Protocol):
    async def complete(self, messages, **kwargs) -> Completion: ...
    async def raw_provider_call(self, **provider_specific_kwargs): ...
```

- `complete()` : l'API standard, 95% des cas.
- `raw_provider_call()` : **escape hatch** quand t'as besoin d'une feature spécifique au provider (genre prompt caching Anthropic, ou structured outputs OpenAI). Tu sors de l'abstraction sciemment, mais c'est tracé.

#### Le fallback automatique

Chaque agent peut configurer une `provider_chain = ["anthropic", "openai"]`. Si Anthropic retourne une 5xx ou timeout → bascule automatique sur OpenAI. Un event `llm_fallback_triggered` est publié sur le bus → le Dashboard peut alerter.

C'est le NFR12 : "graceful degradation".

#### Sécurité

- Aucune clé API dans les logs/traces (NFR9). Tous les logs filtrent les patterns API key.
- Les erreurs sont transformées en **RFC 7807 Problem Details** (un format standard d'erreur HTTP) avec `correlation_id`.

#### Comment vérifier

- Provider Anthropic down → fallback OpenAI fonctionne.
- Clé API invalide → réponse RFC 7807 sans leak de la clé.
- Test : `raw_provider_call(cache_control={"type": "ephemeral"})` passe sans modification de l'interface standard.

#### Pour aller plus loin

- **ADR** `docs/decisions/llm-abstraction.md` : pourquoi `Protocol` plutôt qu'`ABC`, où placer l'escape hatch.
- **ADR** `docs/decisions/llm-fallback-policy.md` : les conditions exactes de bascule (5xx, timeout, rate-limit, etc.) et les cas où on ne fallback **pas** (ex: erreur 4xx applicative).
- **Runbook** `docs/runbooks/llm-usage.md` : comment ajouter un nouveau provider, comment forcer un provider spécifique en debug.

---

### Story 1.7 — Core auth (token statique MVP + rotation)

**En une phrase :** la serrure de base — un token unique pour entrer, avec possibilité de le changer.

#### Pourquoi c'est important

Le MVP est mono-utilisateur (John). Pas besoin d'un système d'auth complet (login/password/sessions/RBAC) — ça viendra en Growth (Epic 12). Mais quand Agentive est exposé via Tailscale ou un VPN, il **faut** un minimum de protection.

**Solution MVP** : un token statique stocké en variable d'environnement (`AGENTIVE_API_TOKEN`). Toutes les requêtes API doivent fournir un header `Authorization: Bearer <token>`.

#### Le flux

```
Requête → Middleware Auth → Compare bcrypt(token_user) vs hash stocké
                          ├── match → continue → audit event "token_used"
                          └── pas match → 401 RFC 7807 (sans révéler le token)
```

#### Rotation du token

`POST /api/v1/admin/rotate-token` avec le token courant :
1. Génère un nouveau token (32 bytes random base64).
2. Remplace le hash bcrypt en store.
3. Renvoie le nouveau token **une seule fois** (à toi de le sauvegarder).
4. Audit event `token_rotated`.

#### Détection d'usage anormal

Si la fréquence d'usage du token explose → alerte dans le Dashboard (mais ça, c'est Epic 7).

#### Pourquoi pas juste comparer les strings ?

Parce que comparer deux strings prend un temps qui dépend de la longueur du préfixe commun → vulnérable aux **timing attacks**. Avec bcrypt, le temps de comparaison est constant.

---

### Story 1.8 — Design system foundation (shadcn + tokens + routing)

**En une phrase :** poser les bases visuelles cohérentes (composants UI, palette de couleurs, dark mode, navigation 4 espaces) **avant** de construire les écrans complexes.

#### Pourquoi c'est important

Sans design system, chaque écran est codé à la main → couleurs incohérentes, espacements à la louche, accessibilité ignorée. L'expérience utilisateur devient médiocre. Pire, tu te retrouves à réécrire 50 boutons dans 50 styles différents.

**Solution** : `shadcn/ui` v4. C'est **pas** une lib classique : tu **copies** les composants dans ton projet (`npx shadcn@latest add button`), tu en deviens propriétaire, tu peux les modifier librement. Les 17 composants de base copiés = button, input, textarea, card, dialog, sheet, command, tabs, select, tooltip, badge, avatar, separator, skeleton, toast, accordion, scroll-area.

#### Les tokens CSS (palette unifiée)

Les couleurs ne sont **pas** hard-codées dans les composants. Elles sont déclarées comme variables CSS :

```
--background    (fond)
--card          (cartes)
--muted         (textes secondaires)
--primary       (#8B5CF6 — violet 500, accent Agentive)
--destructive   (rouge erreur)
--ring          (focus visible)
```

Switcher dark/light = changer le mapping de ces variables. Aucun composant à modifier.

#### Dark mode par défaut

Cohérent avec UX-DR6 (la spec UX). `next-themes` gère le switch sans **FOUT** (Flash Of Unstyled Theme — l'effet de clignotement quand la page charge).

#### Navigation TanStack Router (4 espaces)

Les 4 espaces UI (Dashboard, Chat, Trace, Config) sont 4 routes file-based dans `frontend/src/routes/`. Switch entre espaces avec `Cmd+1/2/3/4`, transition < 100ms.

La sidebar :
- Fixe à 240px en mode étendu
- Collapsable à 56px (icônes only)
- 4 icônes d'espaces avec tooltip

#### Accessibilité (a11y)

- Focus visible : ring violet 2px + offset 2px
- Tab order suit le flow visuel
- Geist Sans + Geist Mono comme typographie

---

### Story 1.9 — Observability + Caddy + CI

**En une phrase :** mettre en place les caméras (logs/traces), le portier (Caddy = reverse proxy + HTTPS), et le contrôle qualité automatique (CI) **dès le Sprint 0**, pas en bout de course.

C'est trois sujets distincts groupés en une story parce qu'ils sont **transverses** et faciles à oublier si on les remet à plus tard.

#### Partie 1 — Logs structurés + correlation + redaction

**Logs structurés** : au lieu de `print("user 42 logged in")`, tu fais `logger.info("user_logged_in", user_id=42)`. Ça produit du JSON parsable, indexable, requêtable.

**Correlation ID** : généré ou propagé dès l'entrée HTTP, présent dans :
- Header `X-Correlation-ID`
- Tous les logs structlog
- Tous les spans OpenTelemetry

Résultat : tu peux suivre **une seule requête utilisateur** à travers tout le système.

**Redaction `structlog` à 3 étages** (NFR9 — pas de leak de secret) :
1. **Key-name based** : tout champ dont la clé matche `*api_key*`, `*token*`, `*password*`, `*secret*` → masqué.
2. **PII patterns** : email, n° téléphone, n° carte — masqués via regex.
3. **API keys patterns** : préfixes connus (`sk-`, `pk-`, `xoxb-`, etc.) — masqués même s'ils apparaissent dans une string libre.

Tu peux ajouter des patterns custom — voir `docs/runbooks/observability.md`.

**Sentry** : le plugin Sentry MCP est activé dans `.claude/settings.json` (workspace). Quand tu seras en prod, branche le DSN en env var pour shipper les exceptions à Sentry — l'instrumentation est déjà prête.

#### Partie 2 — Erreurs RFC 7807

Quand une exception remonte au handler FastAPI, la réponse est **toujours** au format `application/problem+json` :

```json
{
  "type": "https://agentive.lan/errors/llm-provider-down",
  "title": "LLM provider unavailable",
  "status": 503,
  "correlation_id": "01HX2K...",
  "agent_id": "code-producer-v1",
  "module": "m2_agent_registry",
  "tenant_id": null
}
```

C'est parsable, traçable, et ça ne leak **jamais** de stack trace en prod.

#### Partie 3 — Caddy reverse proxy

**Caddy** = un reverse proxy moderne qui fait HTTPS automatiquement (Let's Encrypt en prod, self-signed en dev, Tailscale MagicDNS si exposition VPN).

Caddy applique aussi les **security headers** :
- HSTS (force HTTPS)
- X-Content-Type-Options (anti MIME sniffing)
- X-Frame-Options (anti clickjacking)
- Referrer-Policy
- CSP avec nonces (anti XSS — bloque tout `<script>` injecté sans le bon nonce)
- CORS whitelist explicite (pas de `*` qui ouvre la porte à tout le web)

#### Partie 4 — CI GitHub Actions (4 workflows distincts)

Le doc Sprint 0 livre **4 workflows**, pas un seul :

| Workflow | Quand | Ce qu'il fait |
|---|---|---|
| `ci.yml` | Chaque push & PR | `lint` (ruff + eslint + jsx-a11y) + `test` (pytest + vitest) + `build` (image backend + bundle frontend) + `import-linter` |
| `security.yml` | Push & PR + cron | `gitleaks` (secrets), `trivy` (vulnérabilités images), `bandit` (code Python sensible) |
| `sbom.yml` | Sur tag de release | Génère un SBOM (Software Bill of Materials, CycloneDX) — liste exhaustive des dépendances pour audit/compliance |
| `deploy-staging.yml` | Push sur `main` | Build + push image + déploie sur le serveur staging via SSH (`docker-compose.staging.yml`) |

**Si un job échoue, le merge est bloqué.** Pas négociable.

> Le staging est déjà déployé derrière Traefik (le reverse proxy de l'infra Idem). En local tu utilises Caddy ; en staging/prod, c'est Traefik. Les deux comprennent le même ACME / security headers.

#### Pourquoi dès Sprint 0

Si tu ajoutes l'observabilité au Sprint 3, t'as 3 mois de code à instrumenter rétroactivement. Si t'ajoutes la CI au Sprint 3, t'as 3 mois de code potentiellement cassé. **Baseline by design** = bien moins de dette technique.

#### Pour aller plus loin

- **Runbook** `docs/runbooks/observability.md` : guide pratique (lecture des logs JSON, ajout d'un pattern de redaction, lien correlation_id ↔ trace OTel ↔ audit log).
- **`SECURITY.md`** à la racine : récap des features sécu Sprint 0.

---

## 5. Schéma — l'archi à l'arrivée du Sprint 0

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (Vite + React 19 + shadcn v4)                         │
│  ├── /dashboard  /chat  /trace  /config (routes vides)          │
│  └── Sidebar 4 espaces, dark mode, Cmd+1/2/3/4                  │
└────────────────────┬────────────────────────────────────────────┘
                     │ HTTPS (Caddy + auto-cert)
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  Caddy reverse proxy                                            │
│  ├── HTTPS auto (Let's Encrypt / Tailscale / self-signed)       │
│  └── Security headers + CORS whitelist                          │
└────────────────────┬────────────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  Backend FastAPI (Python 3.14 + uv) — backend/src/agentive_backend/│
│  ├── Middleware auth      (Bearer token bcrypt)                 │
│  ├── Middleware correlation (X-Correlation-ID)                  │
│  ├── Middleware exception → RFC 7807 Problem Details            │
│  ├── shared/                                                    │
│  │   ├── event_bus/       (LISTEN/NOTIFY + Outbox)              │
│  │   ├── repositories/    (UserRepo, MemoryChunkRepo, ...)      │
│  │   ├── llm/             (LLMProvider + Anthropic + OpenAI)    │
│  │   ├── auth/            (token bcrypt + rotation)             │
│  │   ├── logging/         (structlog + redaction 3-stage)       │
│  │   └── metrics/         (OpenTelemetry + Prometheus exporter) │
│  ├── features/            (vide — accueille M2-M12)             │
│  ├── infra/{db,llm,mcp}/  (adapters — db prêt, llm prêt, mcp à venir)│
│  └── api/                 (4 routers + admin/ pour rotate-token)│
└────────────────────┬────────────────────────────────────────────┘
                     ▼
┌─────────────────────────────────────────────────────────────────┐
│  PostgreSQL 17 + pgvector                                       │
│  ├── 8 tables (users, workflows, agents, memory_chunks, ...)    │
│  ├── tenant_id NULL partout                                     │
│  ├── RLS activée (tenant_isolation)                             │
│  ├── Index HNSW sur chunk_embeddings (paramètres tunés Story 1.3)│
│  └── outbox_events (event bus)                                  │
└─────────────────────────────────────────────────────────────────┘

[Spike validés (1.2 + 1.3) → on peut démarrer Sprint 1]
```

---

## 5.5. Le process BMAD code review (un truc qu'on voit dans chaque commit)

Si tu regardes l'historique git, tu verras que **chaque commit story** se finit par `+ code review fix-batch` :

```
Story 1.9 — Observability + Caddy + CI + code review fix-batch P-01
Story 1.8 — Design system foundation [...] + code review fix-batch
Story 1.7 — Core auth token statique + code review fix-batch
Story 1.6 — Core LLM abstraction (multi-provider) + code review fix-batch
...
```

C'est le **process BMAD** appliqué à Agentive.

### Comment ça marche

Après l'implémentation initiale d'une story, **avant** le merge final :
1. **Le dev** (toi, ou un agent IA en mode dev) implémente la story selon les Acceptance Criteria.
2. **Une review adversariale** est lancée en parallèle, avec 3 angles :
   - **Blind Hunter** : cherche les bugs cachés sans présupposer la qualité du code (qu'est-ce qui peut casser ?)
   - **Edge Case Hunter** : explore systématiquement chaque branche / boundary (que se passe-t-il avec input vide / négatif / unicode / …)
   - **Acceptance Auditor** : vérifie que **chaque** Acceptance Criterion de la story est *réellement* satisfait par le code, pas juste "ça compile"
3. Les findings sont triés en **catégories de gravité** (Critical / High / Medium / Low / Informational).
4. **Fix-batch** : tu patches Critical + High obligatoirement, Medium au cas par cas, Low/Info en backlog.
5. Commit final avec mention `+ code review fix-batch`.

### Pourquoi c'est précieux

- Tu ne livres pas une story sur "ça passe les tests que **j'ai** écrits" — tu livres sur "un reviewer hostile n'a pas trouvé de critique majeure".
- Les Acceptance Criteria deviennent contractuels (l'Auditor vérifie chacun).
- Le process scale : tu peux le faire jouer par un agent IA (skill `bmad-code-review`).

### Effet sur le code

Les commits "fix-batch" sont **gros** (souvent 15-25 patches d'un coup). Pour le plus dense :
- Story 1.1 : 19 patches scaffolding + 25 patches root/orchestration + 25 High+Critical chunk 2/3/5
- Story 1.2 : 10 patches P1-P10 post-bmad review

C'est normal. C'est le prix d'une fondation solide.

---

## 6. Les "gates" — les points de blocage durs

Si ces critères ne sont pas atteints, **on n'avance pas** :

| Gate | Story | Critère |
|---|---|---|
| **Démarrage rapide** | 1.1 | `make dev` < 60s, tous services healthy |
| **LangGraph valide** | 1.2 | Checkpointing + Scatter-gather + HITL fonctionnent natif |
| **Latence vectorielle** | 1.3 | < 200ms p95 sur 10k chunks avec reranking |
| **Pas de secret leaké** | 1.1, 1.6 | gitleaks bloque les secrets, NFR9 pas de leak en log |
| **CI verte** | 1.9 | Lint + test + build passent |

---

## 6.5. Carte des livrables — où trouver quoi

Tu cherches le code, les décisions, les modes d'emploi ? Voici la carte.

### ADRs — `docs/decisions/` (les décisions actées avec justification)

| Fichier | Lié à |
|---|---|
| `m3-spike-result.md` | Story 1.2 — confirmation pari LangGraph |
| `m4-bench-result.md` | Story 1.3 — confirmation pari pgvector |
| `hnsw-tuning.md` | Story 1.3 — paramètres HNSW retenus + tableau comparatif HNSW vs ivfflat |
| `event-bus-naming.md` | Story 1.4 — convention de nommage des events |
| `event-bus-migration-trigger.md` | Story 1.4 — seuils pour basculer LISTEN/NOTIFY → Redis Streams |
| `repository-pattern.md` | Story 1.5 — design du Repository Pattern |
| `llm-abstraction.md` | Story 1.6 — design de l'interface `LLMProvider` |
| `llm-fallback-policy.md` | Story 1.6 — règles de bascule entre providers |

### Runbooks — `docs/runbooks/` (modes d'emploi opérationnels)

| Fichier | Sert à |
|---|---|
| `m3-checkpoint-inspect.md` | Inspecter un checkpoint LangGraph en cours de debug |
| `m4-bench-rerun.md` | Re-jouer un benchmark M4 (ex: changement modèle d'embedding) |
| `event-bus-debug.md` | Inspecter l'outbox, replay manuel d'un event coincé |
| `repositories-usage.md` | Exemples d'usage du Repository Pattern pour les modules futurs |
| `llm-usage.md` | Ajouter un nouveau provider, forcer un provider en debug |
| `observability.md` | Lire les logs JSON, ajouter un pattern de redaction, corréler logs/traces/audit |

### Code de référence

| Endroit | Quoi |
|---|---|
| `backend/spike/` | Spike LangGraph (Story 1.2) |
| `backend/_bench_artifacts/` | Résultats CSV des benchs (Story 1.3) |
| `backend/alembic/versions/20260419_000000_initial.py` | La migration initiale qui crée toutes les tables, RLS, rôles |
| `backend/src/agentive_backend/shared/` | Tous les services partagés (auth, event_bus, llm, logging, metrics, repositories) |
| `infra/caddy/` | Config Caddy (security headers, ACME, reverse proxy) |
| `infra/postgres/` | Init scripts Postgres |
| `.github/workflows/` | Les 4 workflows CI |
| `Makefile` | **Toutes** les commandes utilisables — fais `make help` |
| `SECURITY.md` | Synthèse des choix sécu Sprint 0 |
| `CONVENTIONS.md` | Pointeur vers les conventions de code |

### Configuration / secrets

| Fichier | Quoi |
|---|---|
| `.env.example` | Template des variables d'environnement |
| `.env.encrypted` | Secrets chiffrés via SOPS + age (commit-friendly) |
| `.sops.yaml` | Config SOPS (clé age publique) |
| `.gitleaks.toml` | Patterns gitleaks personnalisés |
| `.import-linter` | Règles d'isolation entre modules |
| `.pre-commit-config.yaml` | Hooks pre-commit (gitleaks, ruff, eslint) |

---

## 7. Le glossaire pour s'y retrouver

| Terme | Définition courte |
|---|---|
| **Epic** | Gros morceau de produit, regroupe plusieurs stories |
| **Story** | Petit incrément livrable avec critères d'acceptation |
| **Spike** | Mini-prototype technique pour valider/invalider un pari |
| **Gate** | Critère bloquant : pas atteint = on n'avance pas |
| **ADR** | Architecture Decision Record : doc qui acte une décision technique |
| **Scaffolding** | Le squelette de projet généré (dossiers, fichiers de config) |
| **NFR** | Non-Functional Requirement (perf, sécurité, fiabilité, maintenabilité) |
| **FR** | Functional Requirement (ce que le système fait) |
| **AR** | Architecture Requirement (contraintes architecturales) |
| **Embedding** | Vecteur numérique représentant un texte (RAG) |
| **HNSW** | Algo d'index pour recherche vectorielle approchée rapide |
| **RAG** | Retrieval-Augmented Generation : récupérer du contexte avant de générer |
| **Checkpoint** | Sauvegarde intermédiaire d'un workflow pour reprise après crash |
| **HITL** | Human-in-the-Loop : workflow qui s'arrête pour validation humaine |
| **Scatter-gather** | Pattern : envoyer en parallèle puis consolider |
| **Outbox Pattern** | Garantir qu'un event publié = transaction commitée (pas de perte) |
| **`LISTEN/NOTIFY`** | Mécanisme natif Postgres de signal pub/sub |
| **`correlation_id`** | ID unique propagé dans une chaîne pour tout corréler |
| **Repository Pattern** | Classe centralisée pour tous les accès à une entité DB |
| **RLS** | Row Level Security : isolation au niveau ligne par Postgres |
| **`tenant_id`** | ID du locataire en multi-tenancy (nullable au MVP) |
| **`shadcn/ui`** | Bibliothèque de composants UI où tu copies le code (pas une dépendance) |
| **TanStack Router** | Router React file-based avec gestion fine de l'URL state |
| **FOUT** | Flash Of Unstyled Theme : éclair visuel au chargement |
| **RFC 7807** | Format standard d'erreur HTTP (`application/problem+json`) |
| **Caddy** | Reverse proxy moderne avec HTTPS auto |
| **HSTS / CSP / CORS** | Trois headers de sécurité HTTP |
| **gitleaks** | Outil qui détecte les secrets accidentellement commit |
| **`uv`** | Gestionnaire de paquets Python moderne (alternative à pip/poetry) |
| **`mise`** | Gestionnaire de versions multi-langages (alternative à asdf) |
| **`just`** | Task runner (alternative à make) |
| **OpenTelemetry (OTel)** | Standard d'instrumentation observability (logs/métriques/traces) |
| **SBOM** | Software Bill of Materials — liste exhaustive des dépendances (compliance, audit) |
| **CycloneDX** | Format standard de SBOM (JSON/XML) |
| **Sentry** | Service de tracking d'exceptions/erreurs en prod (plugin MCP activé dans le workspace) |
| **Traefik** | Reverse proxy (équivalent Caddy) — utilisé en staging/prod sur l'infra Idem |
| **`slowapi`** | Lib FastAPI pour rate limiting (queues + backoff exponentiel) |
| **SOPS + age** | Outils de chiffrement de fichiers `.env` commit-friendly (Mozilla SOPS, FiloSottile age) |
| **BMAD** | Méthodologie agile utilisée pour Agentive (PM/SM/Dev/QA agents + skills + adversarial review) |
| **Adversarial review** | Revue de code par un reviewer délibérément hostile — Blind Hunter / Edge Case Hunter / Acceptance Auditor |
| **Spike** | (déjà cité) — mini-proto technique exploratoire |

---

## 8. Et après l'Epic 1 ?

Une fois l'Epic 1 fini, tu démarres le **Sprint 1** avec :

- **Epic 2** — Agent Platform (M2 Agent Registry + M5 Tool Hub + M8 Configurator + Playground) : tu peux créer des agents, leur assigner des outils MCP, les tester en isolation.
- **Epic 3** — Memory & Knowledge System (M4 Memory Manager) : la mémoire vectorielle complète avec ses 4 namespaces.

Puis Sprint 2 : Epics 4 et 5 (Workflow Engine + Pôle Dev). Sprint 3 : Epics 6, 7, 8 (Chat, Dashboard, Trace Explorer). Et là, tu as un MVP utilisable au quotidien.

---

## 9. TL;DR

> L'Epic 1 ne livre **aucun agent IA**. Il livre une **fondation technique** validée :
>
> - 3 stacks démarrent en < 60s avec une commande
> - LangGraph est confirmé comme moteur d'orchestration
> - pgvector est confirmé comme moteur de mémoire vectorielle
> - Event bus, repos, LLM abstraction, auth, design system, observabilité, CI : **tous baseline**
>
> **Sans cet epic, tout le reste s'écroule.** C'est le moment où tu mesures deux fois et coupes une fois.
