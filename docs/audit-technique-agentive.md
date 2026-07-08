# Audit technique — Agentive (backend Python / FastAPI)

> **Rapport d'audit Clean Code · SOLID · DRY · DDD · Sécurité · TDD**
> Consultant : revue senior architecture Python moderne (FastAPI / Pydantic).
> Cible : `/home/yoles/Projects/agentive/backend` — code réel, aucune supposition non sourcée.
> Méthode : **une étape à la fois, validée avant la suivante**. Document consolidé, incrémental.

---

## Table des matières

- [Conventions du rapport](#conventions-du-rapport)
- [Étape 0 — Cartographie & baseline](#étape-0--cartographie--baseline)
  - [0.1 Périmètre & méthode](#01-périmètre--méthode)
  - [0.2 Arborescence & organisation](#02-arborescence--organisation)
  - [0.3 Cartographie des couches (+ lecture DDD)](#03-cartographie-des-couches--lecture-ddd)
  - [0.4 Baseline métrique](#04-baseline-métrique)
  - [0.5 Couplage & contrats d'architecture](#05-couplage--contrats-darchitecture)
  - [0.6 État d'implémentation (implémenté vs stubs)](#06-état-dimplémentation-implémenté-vs-stubs)
  - [0.7 Constats préliminaires (Étape 0)](#07-constats-préliminaires-étape-0)
  - [0.8 Points à trancher avant l'Étape 1](#08-points-à-trancher-avant-létape-1)
- [Étape 1 — Naming & lisibilité](#étape-1--naming--lisibilité)
  - [1.1 Conventions de nommage — verdict](#11-conventions-de-nommage--verdict)
  - [1.2 Cohérence FR/EN](#12-cohérence-fren)
  - [1.3 Nommage des DTO (suffixes)](#13-nommage-des-dto-suffixes)
  - [1.4 Valeurs magiques & défauts non nommés](#14-valeurs-magiques--défauts-non-nommés)
  - [1.5 Niveaux d'abstraction & fonctions longues](#15-niveaux-dabstraction--fonctions-longues)
  - [1.6 Imports en milieu de fonction](#16-imports-en-milieu-de-fonction)
  - [1.7 Commentaires « archéologiques »](#17-commentaires-archéologiques)
  - [1.8 Liste priorisée (Étape 1)](#18-liste-priorisée-étape-1)
- [Étape 2 — SOLID](#étape-2--solid)
  - [2.1 SRP — Single Responsibility](#21-srp--single-responsibility)
  - [2.2 OCP — Open/Closed](#22-ocp--openclosed)
  - [2.3 LSP — Liskov Substitution](#23-lsp--liskov-substitution)
  - [2.4 ISP — Interface Segregation](#24-isp--interface-segregation)
  - [2.5 DIP — Dependency Inversion](#25-dip--dependency-inversion)
  - [2.6 Synthèse SOLID](#26-synthèse-solid)
- [Étape 3 — DRY & refactoring](#étape-3--dry--refactoring)
  - [3.1 Duplications](#31-duplications)
  - [3.2 Abstractions manquantes](#32-abstractions-manquantes)
  - [3.3 Code mort & spéculatif](#33-code-mort--spéculatif)
  - [3.4 Liste priorisée (Étape 3)](#34-liste-priorisée-étape-3)
- [Étape 4 — Architecture & DDD](#étape-4--architecture--ddd)
  - [4.1 Verdict & lecture stratégique](#41-verdict--lecture-stratégique)
  - [4.2 Carte des contextes bornés](#42-carte-des-contextes-bornés)
  - [4.3 Modélisation tactique actuelle (domaine anémique)](#43-modélisation-tactique-actuelle-domaine-anémique)
  - [4.4 Value Objects manquants](#44-value-objects-manquants)
  - [4.5 Agrégats & où vivent les invariants](#45-agrégats--où-vivent-les-invariants)
  - [4.6 Refactoring illustré — agrégat AgentTemplate](#46-refactoring-illustré--agrégat-agenttemplate)
  - [4.7 Recommandation par paliers](#47-recommandation-par-paliers)
  - [4.8 Synthèse (Étape 4)](#48-synthèse-étape-4)
- [Étape 5 — Sécurité](#étape-5--sécurité)
  - [5.1 Verdict & threat model](#51-verdict--threat-model)
  - [5.2 Authentification & session](#52-authentification--session)
  - [5.3 Exécution d'outils MCP — RCE & sandbox dégradé](#53-exécution-doutils-mcp--rce--sandbox-dégradé)
  - [5.4 SSRF (transport SSE)](#54-ssrf-transport-sse)
  - [5.5 Injection de prompt (format_map)](#55-injection-de-prompt-format_map)
  - [5.6 Fuite de redaction & secrets at-rest](#56-fuite-de-redaction--secrets-at-rest)
  - [5.7 Rate limiting absent](#57-rate-limiting-absent)
  - [5.8 Secrets & logs](#58-secrets--logs)
  - [5.9 Synthèse (Étape 5)](#59-synthèse-étape-5)
- [Étape 6 — Tests (TDD/FIRST)](#étape-6--tests-tddfirst)
  - [6.1 Verdict & principes FIRST](#61-verdict--principes-first)
  - [6.2 Pyramide & isolation](#62-pyramide--isolation)
  - [6.3 Qualité des tests (AAA, mocks)](#63-qualité-des-tests-aaa-mocks)
  - [6.4 Couverture des cas limites](#64-couverture-des-cas-limites)
  - [6.5 Couverture mesurée (run réel)](#65-couverture-mesurée-run-réel)
  - [6.6 Synthèse (Étape 6)](#66-synthèse-étape-6)
- [Étape 7 — Synthèse & roadmap](#étape-7--synthèse--roadmap)
  - [7.1 Verdict global & scorecard](#71-verdict-global--scorecard)
  - [7.2 Tableau consolidé des constats](#72-tableau-consolidé-des-constats)
  - [7.3 Le chantier structurant unique](#73-le-chantier-structurant-unique)
  - [7.4 Roadmap de remédiation (4 vagues)](#74-roadmap-de-remédiation-4-vagues)
  - [7.5 Gaps outillage & CI](#75-gaps-outillage--ci)
  - [7.6 Ce qu'il faut créditer](#76-ce-quil-faut-créditer)
  - [7.7 Conclusion](#77-conclusion)

---

## Conventions du rapport

**Échelle de sévérité** :
- 🔴 **Critique** — risque de correction/sécurité/régression, ou dette bloquante. À traiter en priorité.
- 🟠 **Majeur** — défaut structurel notable (maintenabilité, testabilité, couplage).
- 🟡 **Mineur** — écart de qualité localisé, faible risque.
- 🟢 **Suggestion** — amélioration/optimisation, non bloquante.

**Priorisation** : chaque reco reçoit un couple **Impact** × **Effort** → un ratio (Quick win / Chantier de fond).

**Traçabilité** : chaque constat cite `chemin:ligne` du code réel. Le code AVANT/APRÈS est fourni aux étapes de refactoring (1→4).

**Périmètre** : le **backend Python** (FastAPI/Pydantic). Le frontend React est **hors périmètre** de cet audit DDD/Clean Code Python (mentionné seulement si couplage transverse). ➜ *à confirmer §0.8.*

---

## Étape 0 — Cartographie & baseline

### 0.1 Périmètre & méthode

- **Racine analysée** : `backend/src/agentive_backend/` (+ `backend/tests/` pour la baseline de tests).
- **Toolchain projet** : Python **3.14**, gestion `uv`, `ruff` (lint+format), `mypy --strict`, `import-linter` (contrats d'archi), `pytest` + `testcontainers`. Ces outils tournent **dans Docker** (parité CI via `Makefile`) — ils ne sont **pas installés sur l'hôte**, ce qui a une conséquence directe sur la baseline (voir 0.4 : complexité cyclomatique et couverture non mesurées automatiquement).
- **Nature du projet** : PoC en développement actif (méthodologie BMAD, 1 développeur). Sur 13 modules fonctionnels prévus, **3 sont implémentés** (m2, m5, m7_playground), les 10 autres sont des **stubs** (voir 0.6). L'audit porte donc **essentiellement sur m2, m5, m7_playground, `infra/`, `shared/`, `app/`, `api/`**.

> ⚠️ **Écart template ↔ réalité.** Le brief d'audit contient des exemples d'une application d'authentification (`Email`, `Password`, `ConfirmationCode`, code à 4 chiffres, expiration 1 min, Basic Auth). **Aucun de ces concepts n'existe dans Agentive.** J'ai adapté les étapes au domaine réel (agents, templates, archétypes, tool hub MCP, LLM router, event bus/outbox, sandbox). Confirmation demandée en §0.8.

### 0.2 Arborescence & organisation

Organisation en **modules techniques + feature modules** (pattern « package by feature » côté `features/`, « package by layer » ailleurs) :

```
src/agentive_backend/
├── app/            # Composition root : create_app(), lifespan, middlewares         (4 fichiers, 841 LOC)
│   └── main.py, lifespan.py, middleware.py
├── api/            # Endpoints transverses (admin : health LLM, rotation token)      (5 fichiers, 206 LOC)
│   └── admin/, deps.py
├── features/       # Feature modules m1..m12 (barrel: __init__/router/service/schemas)(24 fichiers, 3453 LOC)
│   ├── m2_agent_registry/   → router, service (827), schemas (379), archetypes, templates/
│   ├── m5_tool_hub/         → router, service (693), schemas
│   ├── m7_playground/       → router, service (312), schemas
│   └── m1,m3,m4,m6,m7_chat,m8,m9,m10,m11,m12 → stubs (6 LOC chacun)
├── infra/          # Adapters techniques                                            (10 fichiers, 2076 LOC)
│   ├── db/         → models.py (451, ORM SQLAlchemy), session
│   ├── llm/        → anthropic_adapter (305), openai_adapter (283)
│   └── mcp/        → client (477), sandbox (465)
└── shared/         # Kernel transverse                                              (51 fichiers, 4892 LOC)
    ├── config.py, exceptions.py, correlation.py, utils.py
    ├── auth/, feature_flags/, logging/, metrics/
    ├── contracts/  → contracts + events (DTO/events inter-features)
    ├── event_bus/  → outbox.py (490), publisher, subscriber, naming (API publique)
    ├── llm/        → router.py (405) + interface, error_classifier, redaction, security, metrics
    └── repositories/ → 12 repos (Repository Pattern) : agent_repo (293), tool_hub_repo (279), …
```

**Structure d'un feature module** (barrel cohérent) : `__init__.py` (API publique) · `router.py` (FastAPI) · `service.py` (logique applicative) · `schemas.py` (DTO Pydantic). ✅ **Cohérence structurelle forte** — la même signature de module est répétée, ce qui facilite la navigation.

### 0.3 Cartographie des couches (+ lecture DDD)

Le projet se déclare en **4 couches techniques** (contrat `layered` : `app → features → infra → shared`). Traduction dans le vocabulaire hexagonal/DDD :

| Couche projet | Rôle | Équivalent DDD/hexagonal | Observation |
|---|---|---|---|
| `app` + `api` | Composition root, endpoints transverses, middlewares | **Presentation / Composition** | OK |
| `features/*/router.py` | Entrée HTTP par feature | **Presentation (adapters entrants)** | OK |
| `features/*/service.py` | Orchestration métier | **Application + (Domaine mélangé)** | ⚠️ le domaine vit *dans* les services |
| `features/*/schemas.py` | DTO d'API | **DTO / contrats de présentation** | OK (Pydantic) |
| `infra/` (db, llm, mcp) | Adapters techniques | **Infrastructure (adapters sortants)** | OK, bien isolé |
| `shared/repositories` | Accès données | **Infrastructure (persistence)** | OK (pattern repo) |
| `shared/llm`, `shared/event_bus`, `shared/contracts` | Abstractions transverses | **Ports + kernel partagé** | Fort |
| `infra/db/models.py` | Modèles ORM | **Modèle de persistance** | ⚠️ **unique modèle « métier », anémique** |

**Constat DDD structurant (à approfondir en Étape 4)** :
- ❌ **Pas de couche `domain` explicite** (aucun répertoire `domain/`, `entities/`, `value_objects/` — vérifié). Il n'y a pas de **Value Objects** (ex. un `TemplateId`, un `Version`, un `Cost`, un `SandboxProfile` typés) ni d'**entités riches** : les invariants métier sont portés soit par les **schémas Pydantic** (validation d'entrée), soit par la **logique impérative des `service.py`**, soit par l'**ORM** (`models.py`).
- Le modèle est de type **« Anemic Domain Model »** (Fowler) : données dans l'ORM/Pydantic, comportement dans les services. C'est un choix *défendable* pour un PoC/CRUD, mais c'est **le principal axe de discussion DDD** de cet audit (Étape 4).
- ✅ **Points forts** déjà présents et rares : **ports/adapters** propres pour le LLM (`shared/llm` port ↔ `infra/llm` adapters) et le MCP, **contrats inter-contexte** explicites (`shared/contracts`), **event bus** avec API publique stable.

### 0.4 Baseline métrique

**Volumétrie (code de production, `src/`, hors tests)**

| Indicateur | Valeur |
|---|---|
| Fichiers `.py` | **95** |
| LOC totales | **11 474** |
| Fonctions `async def` | 139 |
| Classes Pydantic (`BaseModel`) | 44 |
| `raise NotImplementedError` | 5 (stubs / points d'extension) |
| Marqueurs `TODO/FIXME/HACK` | 12 (dont 8× « audit-event bypass cleanup » → Story 9.1) |

**Répartition par couche**

| Couche | Fichiers | LOC | % |
|---|---:|---:|---:|
| `shared` | 51 | 4 892 | 43 % |
| `features` | 24 | 3 453 | 30 % |
| `infra` | 10 | 2 076 | 18 % |
| `app` | 4 | 841 | 7 % |
| `api` | 5 | 206 | 2 % |

> Lecture : le poids de `shared` (43 %) reflète un investissement fort dans le **kernel transverse** (LLM router, event bus, repositories, contrats) — cohérent avec une plateforme d'infrastructure, mais à surveiller (un `shared` obèse peut devenir un fourre-tout : à vérifier en Étape 2 SRP / Étape 3 DRY).

**Baseline tests**

| Indicateur | Valeur |
|---|---|
| Fichiers `test_*.py` | 89 |
| LOC de test | **13 289** |
| Fonctions `def test_*` | **533** |
| Ratio LOC test / LOC prod | **≈ 1,16 : 1** ✅ |
| Répartition | unit 59 · integration 52 · spike 7 · scripts 3 (fichiers) |

> Le ratio test/prod > 1 et la présence de tests d'**intégration sur vraie base** (`testcontainers`) et de **spikes** sont des signaux de maturité au-dessus de la moyenne d'un PoC.

**Complexité & couverture — non mesurées automatiquement (🟠 finding baseline)**

- `radon`/`xenon` (complexité cyclomatique) et `pytest-cov` (couverture) **ne sont pas exécutés en CI** (vérifié : `ci.yml` lance `pytest` sans `--cov` ; aucun gate de complexité). Impossible de donner un chiffre de complexité/couverture *fiable* sans lancer le toolchain Docker.
- **Proxy de complexité** (mesuré manuellement — longueur de fonctions & densité de branchement) :

| Fonction | Fichier | LOC | Densité branchement (fichier) |
|---|---|---:|---|
| `invoke_tool` | `m5_tool_hub/service.py:` | **224** | 66 mots-clés if/for/except/and/or |
| `run` | `m7_playground/service.py:` | **185** | — |
| `connect_server` | `m5_tool_hub/service.py:` | **180** | — |
| `update_template` | `m2_agent_registry/service.py:` | **140** | 63 mots-clés |
| `instantiate_from_template` | `m2_agent_registry/service.py:` | **129** | — |
| `replace_template_tools` | `m2_agent_registry/service.py:` | **128** | — |
| `create_template` | `m2_agent_registry/service.py:` | 92 | — |

> Ces fonctions de 120–224 LOC sont des **candidates prioritaires** aux Étapes 1 (lisibilité), 2 (SRP), 3 (extraction). Une fonction applicative de 224 LOC concentre presque certainement plusieurs responsabilités (validation, orchestration, persistance, audit, gestion d'erreurs).

**Top fichiers (candidats refactoring)** : `m2/service.py` (827) · `m5/service.py` (693) · `event_bus/outbox.py` (490) · `infra/mcp/client.py` (477) · `infra/mcp/sandbox.py` (465) · `infra/db/models.py` (451) · `shared/llm/router.py` (405).

### 0.5 Couplage & contrats d'architecture

Le couplage inter-couches n'est pas laissé au hasard : **5 contrats `import-linter` vérifiés en CI** (source : `/.import-linter`). C'est un **atout majeur et rare** — le couplage est *contraint mécaniquement*, pas seulement documenté.

1. `features-isolated` — indépendance : aucun feature n'importe un autre feature (communication via `shared.event_bus` + `shared.contracts`).
2. `layered` — `app → features → infra → shared` (couches hautes → basses uniquement).
3. `no-direct-db-access-from-features` — pas de `sqlalchemy`/`psycopg` direct dans les features (passage par `shared.repositories`).
4. `event-bus-only-public-api` — les features n'importent que l'API publique du bus (permet le refactor interne / migration Redis).
5. `no-direct-llm-sdk-from-features` — pas de SDK LLM (LangChain/anthropic/openai) direct : passage forcé par `shared.llm.LLMRouter`.

**Exception whitelistée & documentée** : seul `shared.repositories.*_repo` peut importer `infra.db.models` (10 edges listés) — Repository Pattern assumé (ADR `docs/decisions/repository-pattern.md`).

➜ Verdict couplage **structurel** : 🟢 **excellent**. Le couplage *à l'intérieur* des gros services (couplage fonctionnel/temporel) reste à auditer (Étapes 2-3).

### 0.6 État d'implémentation (implémenté vs stubs)

Cadrer le périmètre réel de l'audit :

| Module | LOC | État | Dans le scope audit ? |
|---|---:|---|---|
| `m2_agent_registry` | 1 787 | **Implémenté** (Stories 2.1–2.4) | ✅ **Cœur** |
| `m5_tool_hub` | 1 040 | **Implémenté** (Stories 2.5–2.6) | ✅ **Cœur** |
| `m7_playground` | 537 | **Implémenté** (Story 2.7, en review) | ✅ **Cœur** |
| `infra/` (db, llm, mcp) | 2 076 | **Implémenté** | ✅ |
| `shared/` (kernel) | 4 892 | **Implémenté** | ✅ |
| `app/` + `api/` | 1 047 | **Implémenté** | ✅ |
| `m1,m3,m4,m6,m7_chat,m8,m9,m10,m11,m12` | 6 ×10 | **Stubs** (`__init__` seul) | ⏭️ hors scope (rien à auditer) |

### 0.7 Constats préliminaires (Étape 0)

Constats *de niveau baseline* (les détails de code viennent aux étapes dédiées) :

- **B-01 🟠 Complexité cyclomatique non gouvernée.** Aucun `radon`/`xenon` ni seuil en CI ; plusieurs fonctions de 120–224 LOC. *Impact* : dérive de complexité invisible. *Piste* : ajouter `radon cc`/`xenon` en CI avec un seuil (ex. bloque si grade > C). — *Quick win.*
- **B-02 🟠 Couverture de tests non mesurée.** `pytest-cov` est configuré mais non exécuté en CI (pas de `--cov`, pas de `--cov-fail-under`). *Impact* : le taux réel est inconnu ; risque de zones mortes. *Piste* : activer `--cov` + seuil. — *Quick win.*
- **B-03 🟠 Domaine anémique / pas de couche `domain`.** Invariants métier dispersés entre Pydantic, services et ORM ; pas de Value Objects. *Impact* : logique métier difficile à tester unitairement et à réutiliser. *(Chantier — développé en Étape 4.)*
- **B-04 🟠 Fonctions applicatives surdimensionnées.** `invoke_tool` (224), `run` (185), `update_template` (140)… concentrent validation + orchestration + persistance + audit. *(Développé en Étapes 2/3.)*
- **B-05 🟡 `shared` volumineux (43 %).** À surveiller comme possible fourre-tout transverse. *(Étapes 2-3.)*
- **B-06 🟡 Dette tracée mais présente.** 8 TODO « audit-event bypass » (contournement d'`AuditEventRepo` jusqu'à Story 9.1), `tenant_id` mono-tenant, `fastembed` désactivé (pas de wheel Py 3.14). *Dette assumée et documentée — bon réflexe*, mais à inventorier en Étape 7.
- **Points forts confirmés dès la baseline (à créditer)** : ✅ contrats d'archi en CI (couplage contraint), ✅ ports/adapters LLM & MCP, ✅ ratio tests > 1 avec `testcontainers`, ✅ structure de module homogène, ✅ dette tracée.

### 0.8 Points à trancher avant l'Étape 1

Réponds à ces points pour que la suite soit calibrée (à défaut, j'applique les valeurs par défaut proposées) :

1. **Langue du code** — le domaine mélange FR/EN (docstrings/commentaires FR, identifiants EN). *Question* : on tranche **anglais pour tout le code (identifiants, docstrings, commentaires)** et français réservé à la doc/ADR ? *(défaut proposé : oui — EN pour le code.)*
2. **Périmètre** — je **confirme le backend seul** (React hors scope) ? *(défaut : oui.)*
3. **Modules audités** — je concentre sur **m2, m5, m7_playground, infra, shared, app/api** et j'ignore les 10 stubs ? *(défaut : oui.)*
4. **Template auth** — je **retire définitivement** les exemples auth (Email/Password/code 4 chiffres/Basic Auth) et **remappe** l'Étape 4 (DDD) sur les vrais agrégats candidats (`AgentTemplate`, `ToolServer`, `PlaygroundRun`, `Prompt`/`Version`) et l'Étape 5 (Sécurité) sur les vrais sujets (sandbox bwrap, injection de prompt, secrets/redaction, auth par token+rotation, rate-limiting, RCE/SSRF MCP) ? *(défaut : oui.)*
5. **Profondeur** — veux-tu que je **lance réellement le toolchain Docker** (radon + coverage) pour chiffrer complexité/couverture, ou une analyse statique « à la lecture » suffit pour cet audit ? *(défaut : je te propose de lancer le toolchain à l'Étape 6 pour un chiffre de couverture réel ; complexité via proxy + radon si tu valides l'exécution.)*

---

> ✅ **Étape 0 validée** (défauts §0.8 retenus : code en anglais, backend seul, focus m2/m5/m7_playground/infra/shared/app, remapping domaine réel, coverage réelle prévue Étape 6).

---

## Étape 1 — Naming & lisibilité

**Résumé** : le code est **globalement bien nommé et bien documenté** (docstrings NumPy, PEP 8 respecté, identifiants anglais et parlants). Les axes d'amélioration sont **de cohérence** (suffixes de DTO, FR/EN dans les commentaires) et de **lisibilité par excès** : quelques fonctions très longues à niveaux d'abstraction mêlés, des valeurs magiques, des imports en milieu de fonction, et une forte densité de commentaires « archéologiques » (IDs de revue). Aucun constat 🔴 à cette étape — la lisibilité n'est jamais *bloquante*, mais elle pèse sur la vitesse de maintenance.

### 1.1 Conventions de nommage — verdict

🟢 **Respectées.** Vérifications automatiques :
- Classes en `PascalCase`, fonctions/variables en `snake_case`, modules en `snake_case` : **aucune violation** détectée (`grep` sur `^class [a-z]` et `def [a-z]*[A-Z]` → vide).
- Identifiants **en anglais**, descriptifs et intentionnels : `effective_backend`, `domain_exception_to_raise`, `args_redacted`, `assigned_tool_ids`, `tools_activated_count`. C'est du bon Clean Code — les noms révèlent l'intention.
- Docstrings **NumPy-style** (sections `Parameters` / `Raises`) systématiques sur les méthodes publiques (ex. `m5_tool_hub/service.py:426-460`). Rare et à créditer.

➜ **Rien à corriger sur les conventions elles-mêmes.** Les sous-sections suivantes portent sur la *cohérence* et la *lisibilité*.

### 1.2 Cohérence FR/EN

**🟡 Mineur — `docs de decisions`/effort faible.** Le **code et les docstrings sont en anglais**, mais **~40 lignes de commentaires `#` sont en français**, mélangeant les deux langues dans le même fichier.

**Extrait** — `features/m2_agent_registry/router.py:83-85` :
```python
# partagent la même session_factory (sinon `service.publish(session=…)`
# ouvrirait une transaction sur une autre connexion que le repo, les rendant
# différents, cassant silencieusement l'invariant "même transaction").
```
Idem `features/m2_agent_registry/service.py:93`, `shared/config.py:64`.

**Impact** : incohérence de lecture pour un contributeur anglophone ; friction à l'onboarding ; rend `grep`/revue moins homogène. Faible risque technique.

**Correction (AVANT/APRÈS)** — traduire les commentaires en anglais (décision §0.8 validée : EN pour tout le code) :
```python
# AVANT
# partagent la même session_factory (sinon `service.publish(session=…)`
# ouvrirait une transaction sur une autre connexion que le repo…

# APRÈS
# Both must share the same session_factory: otherwise
# `service.publish(session=…)` would open a transaction on a different
# connection than the repo, silently breaking the "same transaction" invariant.
```
> Réserver le français à la doc/ADR (`docs/`) et aux `sprint-status`. **Quick win** mécanique.

### 1.3 Nommage des DTO (suffixes)

**🟡 Mineur — cohérence de convention.** Les DTO Pydantic mélangent **trois suffixes** pour désigner des rôles qui se recoupent : `*Request` (7), `*Response` (8), `*View` (5), plus quelques classes **sans suffixe** (`ArchetypeSummary`, `ArchetypeDetail`).

**Preuve** — les DTO de *lecture* sont tantôt `*Response`, tantôt `*View`, tantôt sans suffixe :

| Feature | Entrée | Sortie top-level | Sortie imbriquée |
|---|---|---|---|
| `m2_agent_registry` | `CreateTemplateRequest` | `TemplateDetailResponse` | `AssignedToolView`, `ContractSkeletonView`, `ArchetypeSummary` |
| `m5_tool_hub` | `InvokeToolRequest` | `InvokeToolResponse` **et** `ToolServerDetailView` | `ToolView` |
| `m7_playground` | `RunPlaygroundRequest` | `RunPlaygroundResponse` | `ToolInvocationLog` |

Dans `m5`, une réponse HTTP top-level est nommée `InvokeToolResponse` **mais** une autre `ToolServerDetailView` — même rôle, deux suffixes.

**Impact** : le lecteur ne peut pas déduire le rôle d'un DTO de son nom (est-ce un corps de réponse HTTP ou une projection imbriquée ?). Faible risque, coût cognitif diffus.

**Correction — convention unique proposée** :
- `*Request` = corps de requête HTTP entrant.
- `*Response` = corps de réponse HTTP **top-level** (ce que le `router` retourne).
- `*View` = **projection de lecture imbriquée** (élément d'une liste, sous-objet) — jamais top-level.
- **Bannir** les DTO sans suffixe (`ArchetypeSummary` → `ArchetypeSummaryView`).

```python
# AVANT (m5_tool_hub/schemas.py) — deux conventions pour un même rôle top-level
class InvokeToolResponse(BaseModel): ...
class ToolServerDetailView(BaseModel): ...   # ← rendu top-level par le router

# APRÈS — top-level = Response, imbriqué = View
class InvokeToolResponse(BaseModel): ...
class ToolServerDetailResponse(BaseModel):    # renommé (top-level)
    tools: list[ToolView]                     # ToolView reste (imbriqué) ✅
```
> **Effort moyen** (renommages + adaptation front via `openapi-typescript`). À faire tant que la surface d'API est petite (3 features). Ratio impact/effort **moyen** — à planifier, pas urgent.

### 1.4 Valeurs magiques & défauts non nommés

**🟠 Majeur (lisibilité + duplication).** Des **littéraux de configuration** sont dispersés inline, sans constante nommée, et parfois **dupliqués** entre modules.

**Extrait 1 — défauts LLM en dur** (`features/m7_playground/service.py:148-150`) :
```python
model: str = str(llm_cfg.get("model", "claude-sonnet-4-6"))
max_tokens: int = int(llm_cfg.get("max_tokens", 4096))
temperature: float = float(llm_cfg.get("temperature", 0.7))
```
**Extrait 2 — timeout dupliqué** : `30.0` apparaît dans `m5_tool_hub/service.py:422` **et** `infra/mcp/client.py:201` ; `10.0` dans `m5_tool_hub/service.py:219` **et** `infra/mcp/client.py:72`.

**Impact** :
- La valeur par défaut du modèle (`"claude-sonnet-4-6"`) est un **choix produit** noyé dans une ligne de service — introuvable, non testable en un point, risque de divergence si un autre module a son propre défaut.
- Les timeouts dupliqués divergeront (on en change un, pas l'autre). C'est aussi un futur constat DRY (Étape 3).

**Correction (AVANT/APRÈS)** — centraliser en constantes/settings nommées :
```python
# APRÈS — en tête de module (ou mieux : dans shared.config.Settings)
DEFAULT_LLM_MODEL: Final = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS: Final = 4096
DEFAULT_TEMPERATURE: Final = 0.7

# usage
model = str(llm_cfg.get("model", DEFAULT_LLM_MODEL))
max_tokens = int(llm_cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
temperature = float(llm_cfg.get("temperature", DEFAULT_TEMPERATURE))
```
```python
# APRÈS — timeouts : une seule source de vérité
# infra/mcp/client.py
DEFAULT_DISCOVERY_TIMEOUT_S: Final = 10.0
DEFAULT_INVOKE_TIMEOUT_S: Final = 30.0
# m5_tool_hub/service.py importe ces constantes au lieu de re-littéraliser 10.0 / 30.0
```
> **Quick win** à fort ratio : faible effort, supprime un risque de divergence *et* remonte les choix produit (défaut modèle) à un endroit visible/testable.

### 1.5 Niveaux d'abstraction & fonctions longues

**🟠 Majeur (lisibilité) — pré-cadre l'Étape 2 (SRP).** Plusieurs fonctions applicatives mêlent, dans un même corps de 120–224 LOC, des **niveaux d'abstraction hétérogènes** : orchestration de haut niveau + détails bas niveau (`json.dumps`, casts `int()/float()`, `format_map`, timing `time.monotonic()`). Signe distinctif : elles se lisent comme une **procédure numérotée** (`# 1.`, `# 2.`, … `# 6.`).

**Extrait** — `features/m7_playground/service.py` (méthode `run`, 185 LOC) :
```python
# 1. Load template snapshot… (accès repo, 404)
# 2. Filter assigned tools by enabled_tool_ids…
# 3. Resolve prompt: str.format_map…
# 4. Call LLMRouter.complete…
# 5. Best-effort parse against output_contract (just JSON.loads…)
# 6. Audit event (success path).
```
Chacun de ces blocs numérotés est un **niveau d'abstraction distinct**. Le lecteur doit tout tenir en tête simultanément ; la règle « une fonction = un seul niveau d'abstraction » (Clean Code, ch. 3) n'est pas respectée. Idem `m5_tool_hub/service.py::invoke_tool` (224 LOC, blocs `# 1.` `# 2.` `# 3.`).

**Impact** : compréhension lente, tests unitaires difficiles à cibler (on ne peut pas tester « la résolution de prompt » isolément), surface de régression large.

**Correction (principe, détail refactoring en Étape 2)** — extraire chaque étape en méthode privée nommée, la fonction publique devenant une *table des matières* lisible d'un coup d'œil :
```python
# APRÈS (esquisse — développée en Étape 2)
async def run(self, cmd: RunPlaygroundCommand) -> RunPlaygroundResponse:
    snapshot = await self._load_template_snapshot(cmd.template_id, cmd.tenant_id)
    activated = self._resolve_activated_tools(snapshot, cmd.enabled_tool_ids)
    prompt = self._resolve_prompt(snapshot.system_prompt, cmd.arguments)
    completion = await self._complete(snapshot.llm, prompt, cmd.arguments)   # gère l'audit d'échec
    parsed = _best_effort_json(completion.text)
    await self._publish_success_audit(cmd.template_id, activated, completion)
    return _to_response(prompt, completion, parsed)
```
> 🔁 **Renvoi Étape 2** : le refactoring complet (avec diffs) est traité en **2.1 SRP**. Ici on acte le constat de *lisibilité*. Ratio impact/effort **élevé** mais **effort réel non trivial** → chantier planifié, pas quick win.

### 1.6 Imports en milieu de fonction

**🟡 Mineur.** Trois imports sont faits **dans le corps de fonctions**, au lieu du haut de module.

**Extrait** — `features/m5_tool_hub/service.py:506` :
```python
        from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend
        effective_backend = sandbox_backend or detect_sandbox_backend()
```
Idem `m5_tool_hub/service.py:205` (`ConflictError`) et `m5_tool_hub/router.py:171`.

**Impact** : masque les dépendances réelles du module (un lecteur qui scanne les imports du haut ne les voit pas), et c'est souvent le symptôme d'un **cycle d'import** contourné à la main. Faible risque fonctionnel.

**Correction** :
- Si **pas de cycle** : remonter l'import en tête de module (cas probable de `ConflictError`, qui vit dans `shared.exceptions`).
- Si **cycle réel** (`detect_sandbox_backend`) : le résoudre structurellement — injecter la fonction par le constructeur (le service fait déjà de la DI de repos), ou exposer `detect_sandbox_backend` via un port de `shared/`.
```python
# APRÈS (DI — cohérent avec le style d'injection existant)
class ToolHubService:
    def __init__(self, ..., detect_backend: Callable[[], SandboxBackend] = detect_sandbox_backend):
        self._detect_backend = detect_backend
    ...
    effective_backend = sandbox_backend or self._detect_backend()   # testable + import au top
```
> **Quick win** pour les 2 imports sans cycle ; le 3ᵉ (sandbox) est un mini-chantier lié au DIP (Étape 2.5).

### 1.7 Commentaires « archéologiques »

**🟡 Mineur — signal/bruit.** Le code porte une **forte densité de commentaires de traçabilité** référençant des IDs de revue et des dates (`P-05 (CR 2026-05-11)`, `P-08`, `P-17`, `P-20`, `D68`, `D80`…). L'**intention est bonne** (documenter le *pourquoi*), mais l'ID+date est du bruit pour le lecteur du code — cette information vit déjà dans `git blame`, les commits et le `sprint-status`.

**Extrait** — `features/m5_tool_hub/service.py:471-474` :
```python
                # P-20 (CR 2026-05-11) — uniform "Tool not found" message
                # in both branches (missing OR wrong server). Avoids leaking
                # cross-server tool_id existence to authenticated callers
                # (relevant Story 12 multi-tenant).
```

**Impact** : dilue le signal (le *pourquoi* sécurité, qui est précieux) sous des métadonnées de process. Sur les gros services, cela alourdit sensiblement la lecture.

**Correction — garder le POURQUOI, retirer l'ID/date** :
```python
# APRÈS
                # Uniform "not found" message whether the tool is missing or
                # belongs to another server: avoids leaking tool existence
                # across servers to authenticated callers.
```
> Le lien vers la revue reste récupérable via `git blame`. Règle d'équipe suggérée : *« un commentaire explique le pourquoi métier/technique, pas l'historique de process »*. **Quick win** (nettoyage mécanique, sans risque).

### 1.8 Liste priorisée (Étape 1)

Triée par **ratio impact/effort décroissant** (haut = à faire en premier).

| # | Constat | Sévérité | Impact | Effort | Ratio | Type |
|---|---|:--:|:--:|:--:|:--:|---|
| 1.4 | Valeurs magiques / défauts LLM + timeouts dupliqués | 🟠 | Élevé | Faible | ⭐⭐⭐ | **Quick win** |
| 1.2 | Commentaires FR → EN | 🟡 | Moyen | Faible | ⭐⭐⭐ | **Quick win** |
| 1.7 | Commentaires archéologiques (IDs de revue) | 🟡 | Moyen | Faible | ⭐⭐ | **Quick win** |
| 1.6 | Imports en milieu de fonction (les 2 sans cycle) | 🟡 | Moyen | Faible | ⭐⭐ | **Quick win** |
| 1.5 | Fonctions longues / niveaux d'abstraction | 🟠 | Élevé | Élevé | ⭐⭐ | **Chantier** → Étape 2 |
| 1.3 | Suffixes de DTO incohérents | 🟡 | Moyen | Moyen | ⭐ | Planifié |
| 1.6b | Import cyclique `detect_sandbox_backend` (DI) | 🟡 | Moyen | Moyen | ⭐ | → Étape 2.5 (DIP) |

**Bilan Étape 1** : qualité de nommage **au-dessus de la moyenne** ; les gains rapides (1.4, 1.2, 1.7, 1.6) sont **mécaniques et sans risque** et peuvent être groupés en un seul commit « chore(readability) ». Le seul vrai chantier (1.5) est traité au fond en Étape 2.

---

> ✅ **Étape 1 validée.**

---

## Étape 2 — SOLID

**Résumé exécutif.** Le code témoigne d'une **culture SOLID réelle et consciente** — le `LLMRouter` et son `Protocol` sont un cas d'école de **OCP + DIP**, la hiérarchie d'exceptions est **LSP-propre**, et l'injection par constructeur est systématique. Les deux axes à travailler sont : **SRP** (méthodes trop chargées + une classe de service multi-responsabilités) et **DIP côté persistance** (les services dépendent de classes de repository *concrètes*, pas de ports). Aucun 🔴 ; deux 🟠 structurants.

| Principe | Verdict | Sévérité max |
|---|---|:--:|
| 2.1 SRP | ⚠️ **Partiellement respecté** | 🟠 |
| 2.2 OCP | ✅ **Largement respecté** | 🟡 |
| 2.3 LSP | ✅ **Respecté** | 🟢 |
| 2.4 ISP | ✅ **Bon**, une piste d'affinage | 🟡 |
| 2.5 DIP | ✅ **Fort** (LLM) / ⚠️ **faible** (persistance) | 🟠 |

---

### 2.1 SRP — Single Responsibility

**Verdict : ⚠️ partiellement respecté.** Bonne granularité au niveau des *helpers* privés (SRP conscient), mais **violé** au niveau (a) des grosses méthodes publiques et (b) d'une classe de service.

#### Preuve (a) — méthodes multi-responsabilités
`ToolHubService.invoke_tool` (`m5_tool_hub/service.py:415-638`, **224 LOC**) assume, dans un seul corps, **au moins 7 responsabilités** :
1. résolution serveur+outil (+404), 2. vérification FK `agent_template_id`, 3. détection du sandbox backend, 4. exécution (`call_tool`), 5. mapping erreurs MCP → erreurs domaine, 6. audit (chemins succès **et** échec), 7. logging.

Même schéma pour `PlaygroundService.run` (185 LOC) et les méthodes `m2` `update_template` (140), `instantiate_from_template` (129), `replace_template_tools` (128). Signe objectif : elles se documentent en étapes numérotées (`# 1.` … `# 6.`).

**Impact** : une raison de changer par responsabilité → la méthode change pour des motifs orthogonaux (nouveau backend, nouvelle règle d'audit, nouveau mapping d'erreur). Tests unitaires non ciblables (impossible de tester « la résolution du prompt » isolément). Surface de régression maximale.

**Refactoring (AVANT/APRÈS)** — extraire une méthode par responsabilité ; la méthode publique devient une **orchestration lisible** :
```python
# APRÈS — ToolHubService.invoke_tool réduit à l'orchestration (~12 LOC)
async def invoke_tool(self, cmd: InvokeToolCommand) -> dict[str, Any]:
    server, tool = await self._resolve_server_and_tool(cmd.server_id, cmd.tool_id, cmd.tenant_id)
    await self._assert_template_exists(cmd.agent_template_id, cmd.tenant_id)
    backend = cmd.sandbox_backend or self._detect_backend()          # injecté (cf 2.5)
    outcome = await self._execute_with_timing(server, tool, cmd, backend)
    await self._audit_invocation(cmd, tool, backend, outcome)        # chemin d'audit UNIQUE (cf Étape 3 DRY)
    if outcome.error is not None:
        raise outcome.error
    return outcome.result
```
Chaque helper (`_resolve_server_and_tool`, `_execute_with_timing`, `_audit_invocation`) a **une** raison de changer et est unit-testable. Bonus : les deux blocs `try/except` d'audit dupliqués (succès/échec, ~25 LOC chacun) fusionnent dans `_audit_invocation` → **résout aussi un futur constat DRY (Étape 3)**.

**À créditer** : l'extraction de `_publish_invoked`, `_redact_arguments`, `_publish_audit_event` montre que le réflexe SRP existe — il faut le pousser d'un cran au niveau des méthodes publiques.

#### Preuve (b) — classe multi-responsabilités
`m2_agent_registry/service.py` injecte **7 repositories** (`registry, template_repo, prompt_repo, instance_repo, workflow_run_repo, tool_repo, assignment_repo`). Le nombre de collaborateurs est un indicateur de responsabilités : la classe gère **le CRUD de templates + le versioning de prompts + l'instanciation d'instances + l'assignation d'outils + les archétypes**. Ce sont ≥ 3 axes de changement.

**Refactoring proposé** — séparer par cas d'usage (autour des vrais agrégats — voir Étape 4) :
```
AgentTemplateService            → create/update/get template + prompts (template_repo, prompt_repo)
TemplateInstantiationService    → instantiate_from_template          (template_repo, instance_repo, workflow_run_repo)
TemplateToolAssignmentService   → assign/replace/list tools          (template_repo, tool_repo, assignment_repo)
ArchetypeCatalog                → archetypes (registry) — lecture seule
```
> **Sévérité 🟠 · Impact élevé · Effort élevé** → chantier planifié (à coupler avec Étape 4 DDD). Le gain : testabilité, contrats d'atomicité plus locaux, réduction du fan-in de repos par classe.

---

### 2.2 OCP — Open/Closed

**Verdict : ✅ largement respecté.**

**Preuve exemplaire (LLM)** — `LLMRouter` (`shared/llm/router.py`) est **ouvert à l'extension, fermé à la modification** :
- Les providers sont injectés en `dict[str, LLMProvider]` (`router.py:100-116`). **Ajouter un provider = écrire un adapter + l'enregistrer** ; le corps de `complete()` (la logique de fallback) ne change pas.
- Le seul point « données » à étendre est `DEFAULT_MODEL_FALLBACK_MAP` (une table, pas de la logique) — et il est **injectable par router** (`model_fallback_map=`), donc modifiable sans toucher la classe.

**Points 🟡 (conditionnels contenus)** :
- Dispatch de transport MCP — `infra/mcp/client.py:125` : `if transport == "stdio": … elif transport == "sse": … else: raise`. Ajouter un transport (websocket, HTTP streaming) impose de modifier ce dispatch.
- Dispatch de backend sandbox — `infra/mcp/sandbox.py` : `bwrap` vs `setrlimit`.

**Nuance (pourquoi ce n'est pas un défaut majeur)** : ces conditionnels sont **bornés par un `Literal` typé** (2 valeurs), **contraints par le SDK externe** (`mcp` n'expose que `stdio_client`/`sse_client`) et le backend est **déterminé par l'OS**. Passer à une stratégie/registry serait de la sur-ingénierie tant qu'il y a 2 cas.

**Suggestion 🟡 (si un 3ᵉ transport arrive)** — table de stratégies :
```python
# APRÈS — registry de transports (à n'introduire qu'au 3ᵉ transport)
TRANSPORT_CONNECTORS: dict[str, TransportConnector] = {"stdio": _stdio_connect, "sse": _sse_connect}
connect = TRANSPORT_CONNECTORS.get(transport)
if connect is None:
    raise ValueError(f"Unknown transport: {transport!r}")
async with connect(connection_config) as (read, write): ...
```
> Reco : **ne pas refactorer maintenant** (YAGNI) ; documenter le point d'extension. Sévérité 🟡 / suggestion.

---

### 2.3 LSP — Liskov Substitution

**Verdict : ✅ respecté.**

**Preuves** :
- **Hiérarchie d'exceptions** (`shared/llm/exceptions.py`) : `LLMError(AgentiveError)` avec 8 sous-classes (`LLMProviderUnavailableError`, `…TimeoutError`, `…RateLimitError`, `…AuthError`, `…BadRequestError`, `LLMNoFallbackModelError`, `LLMAllProvidersFailedError`). Toutes **substituables** partout où l'on attrape `LLMError` (ex. `m7 run` fait `except LLMError`) ou `AgentiveError` (handler RFC 7807). Hiérarchie propre, pas d'exception qui « ment » sur son type.
- **Adapters LLM** : `AnthropicAdapter`/`OpenAIAdapter` satisfont le `Protocol LLMProvider` structurellement et retournent un `Completion` **normalisé** — donc interchangeables dans le router (`providers[name].complete(...)`). Substituabilité comportementale respectée : le router n'a aucun `isinstance(provider, AnthropicAdapter)`.
- L'échappatoire `raw_provider_call` retourne du **SDK-natif non normalisé** — mais elle est **documentée comme telle** (le contrat *est* « format spécifique au provider »), donc pas de violation LSP : elle ne promet pas d'uniformité.

**Faux positif à écarter** : `RouterCall.complete(messages)` (`router.py:386`) a une signature réduite vs `LLMProvider.complete(messages, *, model, …)`. Ce **n'est pas** une violation LSP : `RouterCall` n'est pas déclaré sous-type de `LLMProvider` (c'est un wrapper pré-lié, façon `functools.partial` typé). Aucun code ne le substitue à un provider.

> Rien à corriger. Sévérité 🟢.

---

### 2.4 ISP — Interface Segregation

**Verdict : ✅ bon, avec une piste d'affinage.**

Le `Protocol LLMProvider` expose **2 méthodes** : `complete` (usage standard) + `raw_provider_call` (échappatoire). C'est déjà une segmentation *intentionnelle* (documentée dans `interface.py:1-23`). Mais **tout consommateur qui ne veut que `complete`** (ex. `PlaygroundService.run`, qui n'appelle jamais `raw_provider_call`) dépend malgré tout de la surface complète — via `LLMRouter` concret de surcroît (cf 2.5).

**Suggestion (🟡)** — scinder le port en deux protocoles orthogonaux ; les consommateurs dépendent du plus étroit :
```python
# APRÈS
class Completer(Protocol):                          # 90 % des consommateurs
    async def complete(self, messages, *, model, max_tokens, ...) -> Completion: ...

class RawProviderAccess(Protocol):                  # rares consommateurs "escape hatch"
    async def raw_provider_call(self, **kwargs) -> Any: ...

class LLMProvider(Completer, RawProviderAccess, Protocol):  # adapters implémentent les deux
    provider_name: ClassVar[str]

# PlaygroundService dépend de Completer, pas de LLMRouter concret
def __init__(self, *, completer: Completer, ...): ...
```
**Impact** : m7 (et tout futur agent runner) déclare exactement ce dont il a besoin ; mocking trivial ; couplage minimal. Faible risque.

**Côté repositories** : `BaseRepo` (`repositories/base.py`) est mince (`with_tenant` + helpers de session) et chaque repo est cohésif par agrégat → **pas de fat interface**. RAS.

> Sévérité 🟡 / suggestion. Ratio impact/effort **moyen** — à faire en même temps que 2.5.

---

### 2.5 DIP — Dependency Inversion

**Verdict : ✅ fort sur l'IA / ⚠️ faible sur la persistance.**

#### Ce qui est exemplaire (à créditer)
- `LLMRouter` dépend de l'**abstraction** `LLMProvider` (Protocol), jamais des SDK concrets (garanti *en plus* par le contrat import-linter `no-direct-llm-sdk`).
- **Inversion de la dépendance event-bus/DB** : plutôt que d'injecter une `AsyncSession` factory dans le router pour publier les events de fallback, le router reçoit un **callback** `on_fallback: FallbackCallback` (`router.py:5-10, 90, 306-326`). Le module reste self-contained ; le wiring DB vit dans `app.lifespan`. **C'est du DIP de manuel, appliqué consciemment** (la motivation est écrite dans la docstring).
- Services : **injection par constructeur** systématique, keyword-only.

#### La faiblesse (🟠) — dépendance aux repositories *concrets*
Les services dépendent de **classes concrètes** de persistance, pas d'abstractions :
```python
# m5_tool_hub/service.py — dépendances concrètes
def __init__(self, *, server_repo: ToolServerRepo, tool_repo: ToolRepo,
             template_repo: AgentTemplateRepo | None = None) -> None: ...
```
`ToolServerRepo`, `AgentTemplateRepo`… sont des classes concrètes héritant de `BaseRepo` (couplées à SQLAlchemy/`with_tenant`/`*_in_session`). La couche application connaît donc la **forme d'une implémentation de persistance précise**.

**Impact** : (1) les tests unitaires mockent des classes concrètes (le duck-typing sauve, mais **aucun contrat typé** ne garantit que le mock respecte la surface) ; (2) impossible de substituer une implémentation (in-memory, autre ORM) sans que l'application le « sache » ; (3) frein à l'émergence d'une couche domaine (Étape 4).

**Refactoring (AVANT/APRÈS)** — définir des **ports de persistance** (Protocols) dont dépend l'application ; les repos concrets les satisfont **structurellement, sans les modifier** :
```python
# APRÈS — port défini côté application/domaine
class ToolServerReader(Protocol):
    def with_tenant(self, tenant_id: UUID | None) -> AbstractAsyncContextManager[AsyncSession]: ...
    async def get_by_id_in_session(self, s: AsyncSession, server_id: UUID) -> ToolServer | None: ...

class ToolHubService:
    def __init__(self, *, server_repo: ToolServerReader, ...) -> None: ...   # dépend du PORT
# ToolServerRepo (concret, infra) satisfait ToolServerReader par typage structurel → 0 changement du repo
```
> **Nuance de senior** : `shared` étant un kernel stable, ce couplage est *pragmatique* et acceptable pour un PoC. Mais pour la cible « DDD propre » (Étape 4), les **ports de persistance** sont l'amélioration canonique — et le typage structurel Python permet de les introduire **sans toucher aux repos existants**. À arbitrer selon l'ambition (PoC vs socle durable).

#### Deux DIP violations ponctuelles (🟡, déjà vues Étape 1)
- `m5_tool_hub/service.py:506` — `from …infra.mcp.sandbox import detect_sandbox_backend` **importé et appelé dans la méthode** : l'application atteint une fonction *infra* concrète. Fix : l'injecter (`detect_backend: Callable[[], SandboxBackend]` au constructeur).
- Idem `router.py:171` côté route.

---

### 2.6 Synthèse SOLID

| # | Constat | Principe | Sévérité | Impact | Effort | Ratio |
|---|---|---|:--:|:--:|:--:|:--:|
| 2.5a | Services couplés aux **repositories concrets** (pas de ports de persistance) | DIP | 🟠 | Élevé | Moyen* | ⭐⭐ |
| 2.1a | Méthodes multi-responsabilités (`invoke_tool` 224, `run` 185, `update_template` 140) | SRP | 🟠 | Élevé | Élevé | ⭐⭐ |
| 2.1b | Classe `m2` multi-responsabilités (7 repos injectés) | SRP | 🟠 | Moyen | Élevé | ⭐ |
| 2.5b | Import/appel `detect_sandbox_backend` en dur (infra) | DIP | 🟡 | Moyen | Faible | ⭐⭐⭐ |
| 2.4 | `LLMProvider` → scinder `Completer`/`RawProviderAccess` ; dépendre de ports étroits | ISP | 🟡 | Moyen | Moyen | ⭐⭐ |
| 2.2 | Dispatch transport/backend par `if/elif` (contenu, 2 cas) | OCP | 🟡 | Faible | Faible | ⭐ (YAGNI) |

*\*« Moyen » grâce au typage structurel Python : les ports s'ajoutent sans réécrire les repos.*

**À créditer explicitement** : `LLMRouter` + `LLMProvider` Protocol + callback `on_fallback` = **référence SOLID (OCP+DIP)** ; hiérarchie d'exceptions LSP-propre ; injection par constructeur systématique ; helpers déjà extraits (SRP conscient). Le niveau SOLID est **nettement au-dessus de la moyenne** d'un PoC.

**Fil conducteur vers l'Étape 4** : les deux 🟠 (SRP méthodes/classe + DIP persistance) convergent vers un même manque — **l'absence d'une couche domaine/application explicite** avec ses ports. Le refactoring SRP (2.1) et les ports de persistance (2.5a) seront donc **traités ensemble** dans le remodelage DDD (Étape 4).

---

> ✅ **Étape 2 validée.**

---

## Étape 3 — DRY & refactoring

**Résumé.** Peu de copier-coller *littéral* (le code est trop soigné pour ça), mais plusieurs **abstractions manquantes** qui produisent une duplication *structurelle* répétée à l'identique sur 7–12 sites : le pattern « lookup-or-404 », le pattern « publish + notify best-effort », le double chemin d'audit succès/erreur, et surtout **une logique de redaction éclatée en 3 implémentations** avec une couverture de secrets incohérente (qui devient un point de sécurité, renvoyé à l'Étape 5). Un seul vrai code mort (spéculatif). Ces déduplications sont **majoritairement à faible risque** et à **fort ratio** — elles réduisent aussi la taille des méthodes SRP de l'Étape 2.

### 3.1 Duplications

#### 3.1.1 🟠 Pattern « lookup-or-404 » (≈ 12 sites)
Le triplet `with_tenant → get_by_id_in_session → if None: raise NotFoundError(...)` est répété **~12 fois** dans `m2/m5/m7` (`get_by_id_in_session` : 12 occurrences ; `raise NotFoundError` : 16).

**AVANT** (`m5_tool_hub/service.py:386-392`, et quasi à l'identique 462-468, `m7:97-103`…) :
```python
async with self._server_repo.with_tenant(tenant_id) as session:
    server = await self._server_repo.get_by_id_in_session(session, server_id)
    if server is None:
        raise NotFoundError(
            detail=f"Tool server '{server_id}' not found",
            context={"server_id": str(server_id)},
        )
```
**APRÈS** — un helper sur `BaseRepo` (deux variantes : standalone et in-session pour les lectures atomiques multi-entités) :
```python
# shared/repositories/base.py
async def require_by_id(self, entity_id: UUID, *, tenant_id: UUID | None, label: str) -> Any:
    async with self.with_tenant(tenant_id) as session:
        row = await self.get_by_id_in_session(session, entity_id)
    if row is None:
        raise NotFoundError(detail=f"{label} '{entity_id}' not found",
                            context={"id": str(entity_id)})
    return row

def require_in_session(self, session, row, entity_id: UUID, *, label: str):   # pour les lectures atomiques
    if row is None:
        raise NotFoundError(detail=f"{label} '{entity_id}' not found",
                            context={"id": str(entity_id)})
    return row
```
```python
# usage standalone
server = await self._server_repo.require_by_id(server_id, tenant_id=tenant_id, label="Tool server")
```
> ⚠️ **Nuance importante** : ne PAS collapser aveuglément les lookups qui vivent dans un **même `with_tenant` que d'autres opérations** (invariant d'atomicité P-02, cf docstring `m2` : template SELECT + instance INSERT + outbox publish = même session). Pour ceux-là, utiliser `require_in_session` (garde la session ouverte). **Sévérité 🟠 · effort moyen · ratio ⭐⭐.**

#### 3.1.2 🟠 Pattern « publish + notify best-effort » (7+ sites)
Le bloc « publier l'event puis `emit_notify` en best-effort avec `try/except` + `_log.warning("event_bus_notify_failed_will_be_polled")` » est dupliqué sur **7+ sites** : `m2` (`service.py:208, 371, 498, 694, 810`), `m5` (`315, 684`), `m7` (`303`).

**AVANT** (`m5_tool_hub/service.py:682-690`, repris partout) :
```python
event_id = await publish(ToolInvokedEvent.event_type, event, session=session)
try:
    await emit_notify(event_id, ToolInvokedEvent.event_type)
except Exception:
    _log.warning("event_bus_notify_failed_will_be_polled",
                 event_id=str(event_id), event_type=ToolInvokedEvent.event_type)
```
**APRÈS** — remonter le best-effort dans `shared/event_bus` (une source de vérité pour la politique « le notify peut échouer, l'outbox worker rattrapera ») :
```python
# shared/event_bus/publisher.py
async def notify_best_effort(event_id: UUID, event_type: str) -> None:
    try:
        await emit_notify(event_id, event_type)
    except Exception:
        _log.warning("event_bus_notify_failed_will_be_polled",
                     event_id=str(event_id), event_type=event_type)
```
```python
# usage
event_id = await publish(ToolInvokedEvent.event_type, event, session=session)
await notify_best_effort(event_id, ToolInvokedEvent.event_type)
```
> Supprime ~7 blocs `try/except` identiques et **centralise la politique de résilience** du bus. **Sévérité 🟠 · effort faible · ratio ⭐⭐⭐ (quick win).**

#### 3.1.3 🟠 Double chemin d'audit succès / erreur
Dans `invoke_tool`, `_publish_invoked` est appelé **deux fois** (`m5:570` chemin erreur, `m5:605` chemin succès), chacun enveloppé de son propre `try/except` d'audit (~25 LOC dupliquées). Idem `m7 run` avec `_publish_audit_event` (`183` erreur, `216` succès).

**Refactoring** — un chemin d'audit unique piloté par un `Outcome`, tel qu'esquissé en **2.1** :
```python
# APRÈS — l'audit est émis UNE fois, quel que soit le statut
outcome = await self._execute_with_timing(server, tool, cmd, backend)   # Outcome(status, result, error, duration_ms)
await self._audit_invocation(cmd, tool, backend, outcome)               # 1 seul try/except best-effort (P-08)
if outcome.error is not None:
    raise outcome.error
return outcome.result
```
> **Convergence SRP × DRY** : ce refactoring sert **à la fois** 2.1 (SRP) et 3.1.3 (DRY). À traiter en un seul chantier. **Sévérité 🟠 · effort moyen · ratio ⭐⭐.**

#### 3.1.4 🟠 Redaction éclatée en **3 implémentations** (+ incohérence de couverture)
Il existe **trois** logiques de masquage de secrets, sans source commune :
1. `shared/llm/redaction.py::redact_secrets` — masque par **préfixes** (`sk-ant-`, `sk-`, …) dans du texte (utilisé par le router).
2. `m5_tool_hub/service.py:73::_redact_connection_config` — masque par **liste de clés fermée** `{authorization, token, password, api_key, secret, auth}`.
3. `m5_tool_hub/service.py:131::_redact_arguments` — masque par **patterns de sous-chaîne** `_SECRET_KEY_PATTERNS` (12 motifs : `bearer`, `credential`, `passphrase`, `_key`, …), récursif.

**Problème double** :
- **DRY** : trois implémentations d'un même concept transverse (sécurité), dont deux dans un *feature module* alors que c'est un cross-cutting concern (le projet a d'ailleurs *déjà* tracé le defer `shared/redaction.py`).
- **🔴 latent — incohérence de couverture** : `_redact_connection_config` (2) reconnaît **6 clés** ; `_redact_arguments` (3) en couvre **12 motifs**. Donc une `connection_config` contenant `bearer`, `credential`, `passphrase`, `client_secret`, `access_key`… **n'est PAS masquée** alors que la même clé le serait dans des arguments d'outil. Fuite potentielle de secret dans la vue `ToolServerDetailView.connection_config`. → **remonté et instruit à l'Étape 5 (Sécurité)**.

**APRÈS** — un module unique `shared/security/redaction.py`, un seul prédicat de secret :
```python
# shared/security/redaction.py  (source unique)
SECRET_KEY_PATTERNS: Final = ("token","password","secret","api_key","apikey",
    "_key","key_","auth","bearer","credential","passphrase","client_secret")

def is_secret_key(key: str) -> bool:
    lk = key.lower(); return any(p in lk for p in SECRET_KEY_PATTERNS)

def redact_mapping(cfg: Mapping[str, Any]) -> dict[str, Any]: ...     # remplace _redact_connection_config
def redact_recursive(value: Any, *, max_depth: int = 8) -> Any: ...    # remplace _redact_arguments
def redact_text(s: str) -> str: ...                                    # enveloppe redact_secrets (préfixes)
```
Les deux fonctions `m5` deviennent des appels au module partagé, avec **le même** `is_secret_key` → l'incohérence disparaît. **Sévérité 🟠 (DRY) + entrée 🔴 en Étape 5 · effort moyen · ratio ⭐⭐⭐.**

#### 3.1.5 🟡 Littéraux de timeout dupliqués
Rappel de **1.4** : `10.0` / `30.0` dupliqués entre `m5_tool_hub/service.py` et `infra/mcp/client.py`. Même correction (constantes nommées, une source). Classé ici comme duplication. **🟡 · quick win.**

### 3.2 Abstractions manquantes

Les duplications ci-dessus révèlent **4 abstractions absentes** — les créer supprime la majorité de la duplication structurelle :

| Abstraction manquante | Résout | Emplacement cible |
|---|---|---|
| `BaseRepo.require_by_id` / `require_in_session` | 3.1.1 (lookup-or-404 ×12) | `shared/repositories/base.py` |
| `notify_best_effort(event_id, type)` | 3.1.2 (publish+notify ×7) | `shared/event_bus/publisher.py` |
| `Outcome` + `_audit_invocation` (chemin unique) | 3.1.3 + SRP 2.1 | `m5`/`m7` service |
| Module `shared/security/redaction.py` | 3.1.4 (redaction ×3 + sécurité) | `shared/security/` |

> Bonus lié à l'Étape 2 : les **Command DTOs** (`InvokeToolCommand`, `RunPlaygroundCommand`) réduisent les signatures à rallonge (7–8 params kw-only) répétées entre router → service.

### 3.3 Code mort & spéculatif

- **3.3.1 🟡 Généralité spéculative — `status` littéral `"tool_error"` jamais produit.** Dans `m7_playground/service.py`, `status: Literal["success","llm_error","tool_error"]` (l.157, 265) déclare `"tool_error"` mais **aucune affectation `status = "tool_error"`** n'existe (vérifié). C'est un état réservé pour le `tool_use` formel différé (D80, Story 4.x). *Reco* : soit le documenter explicitement comme réservé (`# reserved for Story 4.x tool_use`), soit le retirer du `Literal` jusqu'à son implémentation (YAGNI). Faible impact.
- **3.3.2 🟡 Pas de détection de code mort outillée.** Aucun `vulture`/`ruff --select F401,F841` orienté dead-code en CI (rappel **B-01** : ni complexité, ni couverture, ni dead-code gate). Les CR passées ont retiré du code mort *manuellement* (ex. `_setrlimit_preexec`) — un `vulture` en CI rendrait ça systématique. *Reco* : ajouter `vulture src/ --min-confidence 80` au job lint. **Quick win.**
- **RAS majeur** : pas de fonctions privées manifestement orphelines détectées dans les modules audités (les helpers `_reraise_domain_error_from_group`, `_sandbox_argv_prefix`, `_list_tools_via_session`… sont tous référencés).

### 3.4 Liste priorisée (Étape 3)

| # | Constat | Sévérité | Impact | Effort | Ratio |
|---|---|:--:|:--:|:--:|:--:|
| 3.1.2 | `notify_best_effort` (publish+notify ×7) | 🟠 | Élevé | Faible | ⭐⭐⭐ |
| 3.1.4 | Unifier la redaction (×3) — **+ ferme une fuite Étape 5** | 🟠 | Élevé | Moyen | ⭐⭐⭐ |
| 3.1.5 | Timeouts dupliqués → constantes | 🟡 | Moyen | Faible | ⭐⭐⭐ |
| 3.3.2 | `vulture` dead-code en CI | 🟡 | Moyen | Faible | ⭐⭐⭐ |
| 3.1.1 | `require_by_id` / `require_in_session` (lookup-or-404 ×12) | 🟠 | Élevé | Moyen | ⭐⭐ |
| 3.1.3 | Chemin d'audit unique (succès/erreur) | 🟠 | Élevé | Moyen | ⭐⭐ (couplé SRP 2.1) |
| 3.3.1 | Littéral `tool_error` spéculatif | 🟡 | Faible | Faible | ⭐⭐ |

**Bilan Étape 3** : 4 abstractions à créer suppriment l'essentiel de la duplication ; **2 quick wins sans risque** (3.1.2, 3.1.5) + 1 outillage (3.3.2). Le refactoring redaction (3.1.4) est **prioritaire car il ferme aussi une incohérence de sécurité** (instruite en Étape 5). Les items « lookup-or-404 » et « chemin d'audit » se font naturellement *pendant* le refactoring SRP de l'Étape 2 — **ne pas les planifier séparément**.

---

> ✅ **Étape 3 validée.**

---

## Étape 4 — Architecture & DDD

**Résumé exécutif.** Le **DDD *stratégique* est remarquable** : les contextes bornés sont réels et *contraints par l'outillage* (import-linter), l'intégration inter-contextes se fait par **événements de domaine** (outbox + event bus) — c'est du manuel. Le **DDD *tactique* est, lui, absent** : pas de couche domaine, un **modèle anémique** (les modèles ORM *sont* les entités, sans comportement), et surtout **la donnée métier la plus riche — la configuration d'un agent — vit dans un `JSONB` non typé** (`config`, `snapshot`) re-parsé défensivement partout. Les invariants sont dispersés sur **3 lieux** (contraintes DB, DTO Pydantic, code impératif des services), sans propriétaire unique. Ce constat **fédère les 🟠 des Étapes 2-3** (SRP, DIP persistance, redaction éclatée) : ils sont tous des symptômes du même manque de cœur de domaine. Verdict global : 🟠 **majeur, mais c'est un choix *défendable pour un PoC*** — la recommandation est **par paliers**, avec un déclencheur clair d'investissement.

### 4.1 Verdict & lecture stratégique

**À créditer fortement (DDD stratégique = fort) :**
- **Contextes bornés réels et défendus mécaniquement.** Chaque `features/mN` est un contexte isolé ; le contrat import-linter `features-isolated` *interdit* qu'un contexte importe un autre. C'est la matérialisation d'une **frontière de contexte**, pas juste un dossier.
- **Intégration par événements de domaine.** Les contextes communiquent via `shared/event_bus` + `shared/contracts/events` (`m2.agent_template.created`, `m5.tool.invoked`, `m7.playground.run_completed`…), pas par appels directs. C'est le **pattern d'intégration DDD par excellence** (Published Language + Domain Events), avec en plus une **durabilité outbox**.
- **Langage ubiquitaire présent** : `Archetype`, `Template` vs `Instance`, `Department`, `Namespace`, `ToolServer`… le vocabulaire du PRD se retrouve dans le code.
- **Bons instincts tactiques ponctuels** : `AgentInstance.snapshot` (snapshot **immuable** figé à l'instanciation), `Prompt` **append-only versionné**, `AuditEvent` **immuable** (partitionné + `REVOKE UPDATE/DELETE`). Ce sont des intuitions d'immuabilité/event-sourcing saines.

**Le manque (DDD tactique = absent) :** pas de répertoire `domain/`, pas d'entités riches, pas de Value Objects — développé en 4.3–4.5.

### 4.2 Carte des contextes bornés

| Contexte (module) | Rôle métier | Agrégats / entités (tables) | Intégration sortante | État |
|---|---|---|---|---|
| **Agent Registry** (`m2`) | Cycle de vie des agents | `AgentTemplate` (racine) · `Prompt` · `AgentTemplateTool` · `AgentInstance` | events `m2.agent_template.*`, `m2.agent_instance.created` | ✅ implémenté |
| **Tool Hub** (`m5`) | Serveurs MCP & outils | `ToolServer` (racine) · `Tool` | events `m5.tool.*` | ✅ implémenté |
| **Playground** (`m7_playground`) | Test d'agent isolé | *(aucune table — process éphémère)* | event `m7.playground.run_completed` | ✅ implémenté |
| Memory (`m4`) | Mémoire RAG | `Namespace` · `MemoryChunk` · `ChunkEmbedding` | — | ⏭️ stub |
| Workflow (`m3`) | Orchestration | `Workflow` · `WorkflowRun` | — | ⏭️ stub |
| Auth/Identity (`shared/auth`) | Identité & session | `User` · `Session` | — | ✅ (Story 1.7) |
| Audit (`shared`) | Traçabilité immuable | `AuditEvent` · `OutboxEvent` | — | ✅ infra |

> Observation : les tables des contextes **stub** (`Workflow`, `Namespace`, `MemoryChunk`…) existent déjà dans `infra/db/models.py` — le schéma est posé avant le comportement. Cohérent avec l'approche « schéma d'abord » du scaffolding, mais cela **renforce le modèle anémique** (des tables sans domaine associé).

### 4.3 Modélisation tactique actuelle (domaine anémique)

**🟠 Constat central.** Les « entités » sont les modèles SQLAlchemy de `infra/db/models.py` : de **purs sacs de données** (`Mapped[...]` + `mapped_column`), **zéro méthode, zéro invariant en code**. Les repos retournent ces objets ORM, et les services lisent/écrivent leurs attributs. Il n'y a **pas de type de domaine** distinct de la persistance : **le modèle ORM EST l'entité** (pattern « ORM-as-domain »).

**Le point le plus aigu — la config métier en `JSONB` non typé.** La donnée la plus importante du domaine (ce qui *définit* un agent) est un dictionnaire opaque :
```python
# infra/db/models.py:214
class AgentTemplate(Base):
    ...
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)   # ← system_prompt, llm, contracts, error_policy
# infra/db/models.py:237
class AgentInstance(Base):
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False) # ← copie figée de config
```
Sa structure n'est **jamais** garantie par un type de domaine ; elle est :
1. validée **au bord** par des DTO Pydantic (`LLMParams`, `ContractDefinition`, `ErrorPolicy` dans `m2/schemas.py`), puis
2. **re-désérialisée défensivement** dans les services avec des `.get(..., default)` magiques :
```python
# m7_playground/service.py:130,147-150 — le domaine est "deviné" à la lecture
system_prompt_template = str(config_snapshot.get("system_prompt", ""))
llm_cfg = config_snapshot.get("llm", {}) or {}
model = str(llm_cfg.get("model", "claude-sonnet-4-6"))
max_tokens = int(llm_cfg.get("max_tokens", 4096))
temperature = float(llm_cfg.get("temperature", 0.7))
```
**Impact** :
- **Aucune source de vérité** sur « qu'est-ce qu'une config d'agent valide ». La forme validée à l'écriture (Pydantic) est **perdue** au stockage (`dict`) et **re-devinée** à la lecture, avec des défauts dupliqués (cf 1.4/3.1.5).
- Impossible de tester unitairement une règle métier de config sans passer par HTTP ou par la DB.
- Un `config` écrit par une story et lu par une autre peut diverger silencieusement (pas de contrat partagé typé).

### 4.4 Value Objects manquants

Plusieurs concepts métier sont aujourd'hui des **primitives** ou des **`dict`**, alors qu'ils portent des invariants → **Value Objects candidats** :

| Concept | Aujourd'hui | Invariant à encapsuler | VO proposé |
|---|---|---|---|
| Version d'un template/prompt | `version: int` | monotone, bump sur changement de prompt | `Version` (`.next()`) |
| Archétype | `archetype: String(50)` **sans CHECK DB** | ∈ 8 valeurs connues | `Archetype(StrEnum)` |
| Config d'agent | `config: JSONB` | prompt + llm + contrats + error_policy cohérents | `AgentConfig` (agrégat interne) |
| Params LLM | `dict` (validé Pydantic au bord) | `0 ≤ temperature ≤ 2`, `max_tokens > 0`, `provider_chain` non vide dédupliquée | `LLMParams` |
| Type de namespace | `type: String(50)` **sans CHECK DB** (commentaire seul) | ∈ {client, metier, operationnelle, contextuelle} | `NamespaceType(StrEnum)` |
| Politique de rétention | `retention_policy: JSONB` | TTL cohérent, règle d'archivage | `RetentionPolicy` |
| Coût | `cost_estimate_usd: Decimal \| None` | ≥ 0, fini, devise | `Cost` |
| Profil de sandbox | dataclass dans `sandbox.py` | caps CPU/mem, backend | ✅ déjà quasi-VO |

> Deux invariants ne sont **même pas gardés au niveau DB** : `agent_templates.archetype` et `namespaces.type` sont des `String` libres **sans `CheckConstraint`** (comparer à `tool_servers.transport` / `status` qui, eux, ont un CHECK). Un `archetype = "banana"` passerait la DB — l'invariant ne vit que dans le registre/service. **🟡 à corriger même sans DDD complet** (ajouter les CHECK).

### 4.5 Agrégats & où vivent les invariants

**Le problème de dispersion.** Pour l'agrégat `AgentTemplate`, la règle métier *« éditer le prompt système bump la version et crée une révision de prompt »* est portée **impérativement par le service** :
```python
# m2_agent_registry/service.py (update_template) — la règle métier vit dans l'application
# (c) `payload.system_prompt is not None` = trigger unique du bump version + insert prompt
```
…tandis que d'autres invariants du même agrégat vivent **dans la DB** (`uq_agent_template` sur (name, version, tenant), `uq_prompt_template_version`, FK cascade) et **dans Pydantic** (bornes des params). **Aucun objet ne possède l'invariant complet de l'agrégat.** La racine `AgentTemplate` ne « sait » pas gérer ses propres `Prompt`/`AgentTemplateTool` : c'est le service qui orchestre 3 repos avec une atomicité *same-session* maintenue à la main (invariant P-02).

**Où devrait vivre une règle ? — l'exemple de l'expiration (analogie remappée).** Le brief citait « expiration d'un code à 1 min : où doit-elle vivre ? ». L'analogue réel d'Agentive est **l'expiration/archivage mémoire** : `MemoryChunk.ttl_seconds` / `expires_at` / `archived_at` + `Namespace.retention_policy`. Aujourd'hui, aucun code ne porte cette règle (contexte `m4` stub). **Réponse DDD** : la règle *« un chunk est expiré si `now > expires_at` ; il est archivable selon `retention_policy` »* appartient au **domaine** — soit à un VO `RetentionPolicy.is_expired(chunk, now)`, soit à une méthode de l'entité `MemoryChunk.is_expired(clock)` — **ni au SQL seul, ni à un service**. Le service *déclenche* (orchestration/temps), le domaine *décide* (règle). Cadrer ça **maintenant**, avant d'implémenter `m4`, évite de reproduire l'anémie.

### 4.6 Refactoring illustré — agrégat `AgentTemplate`

Objectif : donner au domaine un type propre, framework-free, qui **possède ses invariants** ; l'ORM redevient un simple **mapping de persistance**.

**AVANT** — règle métier dans le service, config en `dict` :
```python
# service impératif : lit/écrit des dict, décide du bump
if payload.system_prompt is not None and payload.system_prompt != current["system_prompt"]:
    new_version = template.version + 1
    # ... insert prompt row, bump agent_templates.version, publish event (3 repos, same session)
```
**APRÈS** — domaine riche (`domain/agents/`), invariants encapsulés :
```python
# domain/agents/value_objects.py  (framework-free : dataclasses/enum, PAS de SQLAlchemy)
class Archetype(StrEnum):
    ORCHESTRATOR = "orchestrator"; RESEARCHER = "researcher"; ANALYST = "analyst"
    PRODUCER = "producer"; STRATEGIST = "strategist"; CONTROLLER = "controller"
    WATCHER = "watcher"; COMMUNICATOR = "communicator"

@dataclass(frozen=True)
class Version:
    value: int
    def __post_init__(self) -> None:
        if self.value < 1: raise ValueError("version must be >= 1")
    def next(self) -> "Version": return Version(self.value + 1)

@dataclass(frozen=True)
class LLMParams:
    model: str; max_tokens: int; temperature: float; provider_chain: tuple[str, ...]
    def __post_init__(self) -> None:
        if not (0.0 <= self.temperature <= 2.0): raise ValueError("temperature out of range")
        if self.max_tokens < 1: raise ValueError("max_tokens must be positive")
        if not self.provider_chain: raise ValueError("provider_chain must be non-empty")

@dataclass(frozen=True)
class AgentConfig:                              # remplace le JSONB deviné
    system_prompt: str; llm: LLMParams
    contracts: ContractDefinition; error_policy: ErrorPolicy
    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AgentConfig": ...   # UN SEUL point de parse
    def to_mapping(self) -> dict[str, Any]: ...                           # UN SEUL point de sérialisation
```
```python
# domain/agents/aggregate.py — la racine possède l'invariant de bump
@dataclass
class AgentTemplate:
    id: UUID; name: str; archetype: Archetype
    version: Version; config: AgentConfig
    def revise(self, new_config: AgentConfig) -> "PromptRevision | None":
        """Invariant métier : le bump + une révision de prompt surviennent
        SSI le system_prompt change. Toute la règle vit ICI, testable sans DB."""
        if new_config.system_prompt != self.config.system_prompt:
            self.version = self.version.next()
            self.config = new_config
            return PromptRevision(version=self.version, content=new_config.system_prompt)
        self.config = new_config
        return None
```
```python
# le repo mappe domaine <-> ORM (la persistance devient un détail)
class AgentTemplateRepo:
    async def save(self, tmpl: AgentTemplate, revision: PromptRevision | None, *, session): ...
    def _to_domain(self, row: models.AgentTemplate) -> AgentTemplate: ...
```
**Gains** : la règle de version est **unit-testable en microsecondes sans DB** ; le `config` n'est parsé qu'à **un seul endroit** (`from_mapping`) → supprime les `.get(..., default)` dispersés (1.4/4.3) ; `update_template` maigrit (SRP 2.1) ; le contrat de config est **partagé et typé** entre écriture et lecture.

### 4.7 Recommandation par paliers

Le DDD tactique complet est un **investissement** ; pour un PoC mono-développeur, l'appliquer *partout d'un coup* serait contre-productif. Approche **incrémentale, par ROI** :

- **Palier 1 — Value Objects sur les `JSONB` + CHECK manquants (🟠 fort ROI, risque faible).**
  Introduire `AgentConfig`/`LLMParams`/`Archetype`/`Version` et les utiliser à l'écriture **et** à la lecture (un seul `from_mapping`). Ajouter les `CheckConstraint` DB manquants (`archetype`, `namespace.type`). **Supprime** l'anémie la plus coûteuse (config devinée) et les défauts dupliqués. *Ne touche pas l'ORM ni les repos.*
- **Palier 2 — Comportement d'agrégat (🟠 moyen).**
  Déplacer les règles (bump de version, assignation d'outils, validité d'instanciation) sur des entités de domaine ; les repos mappent domaine ↔ ORM. Résout SRP-classe (2.1b) et prépare les **ports de persistance** (DIP 2.5a).
- **Palier 3 — Couche domaine complète + ports (⚪ seulement si Agentive devient un produit durable).**
  `domain/` framework-free, repositories exposés en `Protocol`, événements *levés par les agrégats*. À déclencher au **2ᵉ département (Epic 5)** ou dès que les règles métier se multiplient — c'est le **signal** que l'anémie commence à coûter.

> **Position de senior** : rester au **Palier 1** tant que le domaine est simple est *le bon choix*. Mais **cadrer la règle d'expiration mémoire (4.5) avant d'implémenter `m4`**, et introduire `AgentConfig` **avant** que 3 contextes ne lisent le `config` JSONB, sont deux fenêtres à ne pas rater (le coût de rattrapage croît avec le nombre de lecteurs du `dict`).

### 4.8 Synthèse (Étape 4)

| # | Constat | Sévérité | Impact | Effort | Ratio |
|---|---|:--:|:--:|:--:|:--:|
| 4.4b | `CheckConstraint` manquants (`archetype`, `namespace.type`) | 🟡 | Moyen | Faible | ⭐⭐⭐ |
| 4.3/4.4 | Config métier en `JSONB` non typé → VO `AgentConfig` (Palier 1) | 🟠 | Élevé | Moyen | ⭐⭐ |
| 4.5 | Invariants dispersés (DB/Pydantic/service), agrégat sans comportement | 🟠 | Élevé | Élevé | ⭐⭐ |
| 4.5b | Cadrer la règle d'expiration/rétention **avant** d'implémenter `m4` | 🟠 | Élevé | Faible* | ⭐⭐⭐ |
| 4.3 | Domaine anémique / ORM-as-domain (Paliers 2-3) | 🟠 | Élevé | Élevé | ⭐ (conditionnel) |

*\*Faible car préventif : c'est de la conception, pas du refactoring de code existant.*

**À créditer** : DDD **stratégique** (contextes bornés outillés + intégration par événements + snapshots/append-only immuables) = **au-dessus du niveau attendu**. Le chantier est le DDD **tactique**, à mener **par paliers**, en saisissant les 2 fenêtres préventives (VO `AgentConfig` avant multiplication des lecteurs ; règle de rétention avant `m4`).

**Convergence des Étapes 2-3-4** : SRP-méthodes (2.1a), SRP-classe (2.1b), DIP-persistance (2.5a), redaction éclatée (3.1.4) et anémie (4.3) sont **un seul et même chantier** : *faire émerger un cœur de domaine avec ses VO, ses agrégats et ses ports*. À planifier comme **un** épic technique, pas 5 tickets isolés.

---

> ✅ **Étape 4 validée.**

---

## Étape 5 — Sécurité

**Résumé exécutif.** La **posture sécurité est mature et *consciente*** pour un PoC : SOPS + gitleaks, auth constant-time avec anti-bypass, flag `AGENTIVE_ALLOW_MCP_REGISTRATION` *deny-by-default*, audit immuable, redaction des clés en logs. Les primitives sont bien faites. Les points à traiter sont **contextuels au threat model** : (a) l'**exécution d'outils MCP** est le vrai risque (RCE/SSRF) et son sandbox **tombe en mode dégradé sans isolation réseau/FS** dans les environnements conteneurisés courants ; (b) l'**injection de prompt `format_map`** attrape trop peu d'exceptions (500 + echo du prompt) — **finding CR P-02 encore ouvert** ; (c) la **redaction `connection_config` laisse fuiter** des secrets via une route authentifiée (confirme 3.1.4) ; (d) **rate-limiting absent** (dépendance présente, non câblée). Aucun 🔴 *dans le threat model mono-utilisateur*, mais **deux 🟠 deviennent 🔴 si Agentive passe multi-utilisateur (Epic 12) ou est exposé**.

### 5.1 Verdict & threat model

**Threat model déclaré** (PRD) : application **self-hosted, mono-utilisateur**, derrière un reverse proxy (Caddy TLS) et une auth par token. L'attaquant réaliste n'est donc pas « un utilisateur malveillant authentifié » mais : (1) un **tool/serveur MCP non fiable** que l'utilisateur enregistre, (2) une **exposition accidentelle** du service, (3) une **fuite via logs/exports/réponses**. La calibration de sévérité ci-dessous suit ce modèle — et signale explicitement **ce qui change au passage multi-utilisateur**.

**À créditer (défense en profondeur réelle) :**
- 🟢 `AGENTIVE_ALLOW_MCP_REGISTRATION` **deny-by-default** gate l'enregistrement, l'invocation ET le Playground — la surface RCE/SSRF est *opt-in explicite*.
- 🟢 Auth **constant-time** + anti-bypass token vide (5.2).
- 🟢 Secrets : **SOPS** (`.env.encrypted`) + **gitleaks** en pre-commit *et* CI + redaction de clés en logs.
- 🟢 **Audit immuable** (`audit_events` partitionné, `REVOKE UPDATE/DELETE`, `INSERT`-only).
- 🟢 Erreurs **RFC 7807 sanitizées** (422 nettoyé, pas de fuite d'internals) — *sauf* le chemin `format_map` (5.5).

### 5.2 Authentification & session

**Verdict : 🟢 bien fait pour son périmètre.** (`shared/auth/__init__.py`)
- `generate_token` = `secrets.token_urlsafe(32)` → **256 bits** d'entropie. ✅
- `hash_token` = **bcrypt**. ✅
- `verify_token` = **`hmac.compare_digest`** (constant-time) + support bcrypt ; et surtout un **garde-fou anti-bypass documenté** : sans lui, une variable d'env vide ferait que `compare_digest("", "")` → `True`, laissant un `Authorization: Bearer ` (vide) authentifier. Le code neutralise explicitement ce cas (l.65-69). **Bon réflexe de sécurité.**

**Résiduels :**
- 🟡 **Modèle à token statique unique** (mono-utilisateur MVP). Pas d'expiration/rotation automatisée au-delà de l'endpoint `rotate_token` manuel ; pas de scoping par utilisateur. Acceptable pour le PoC ; **à revoir intégralement en Epic 12** (multi-user) — sinon un seul token = accès total.
- 🟡 **TODO Story 1.7** (`api/admin/health_llm.py:49`) : le gate `admin` repose encore sur un **loopback** au lieu d'un `Authorization: Bearer`. À fermer avant toute exposition non-loopback de l'admin.

### 5.3 Exécution d'outils MCP — RCE & sandbox dégradé

**C'est LE point de sécurité d'Agentive.** Invoquer un outil MCP en transport `stdio` = **spawn d'un sous-processus arbitraire** (`StdioServerParameters(command, args)`). C'est la fonctionnalité même — donc le sandbox est la seule barrière.

**Ce qui est bien (bwrap) :** `sandbox.py` construit un `bwrap --unshare-net` (pas de réseau) `--cap-drop ALL` `--die-with-parent` + ro-binds + tmpfs. **Isolation forte** — quand bwrap fonctionne.

**🟠 Le risque réel — dégradation silencieuse en `setrlimit`.** La docstring l'écrit noir sur blanc (`sandbox.py:17-19`) :
> *« Le fallback [setrlimit] est best-effort : il NE peut PAS restreindre le réseau ni [le FS]. »*

Or `detect_sandbox_backend` bascule en `setrlimit` quand bwrap est présent mais **non fonctionnel** — ce qui est le cas par défaut **dans un conteneur Docker** (seccomp par défaut bloque les user namespaces non privilégiés). Donc **dans l'environnement de déploiement conteneurisé courant, la barrière effective est `setrlimit`, qui n'isole ni le réseau ni le système de fichiers** : un outil MCP non fiable a alors **accès réseau sortant complet et lecture/écriture FS** avec les droits du process backend, borné seulement en CPU/mémoire/nproc.

**Impact** : si `AGENTIVE_ALLOW_MCP_REGISTRATION=true` dans un environnement où bwrap est inopérant, l'exécution d'un outil non fiable = **RCE + SSRF non contenus**. Mitigé aujourd'hui par : flag deny-by-default + mono-utilisateur + le fait que l'utilisateur enregistre ses propres serveurs.

**Recommandations (par ordre) :**
1. **Documenter un invariant opérationnel dur** : *« ne jamais activer le flag dans un environnement où `detect_sandbox_backend()` retourne `setrlimit` ET où des outils non fiables peuvent tourner »*. Le surfacer au démarrage : **log `WARNING` (ou refus) si flag=on ET backend=setrlimit**.
2. **CI kernel-configuré** pour valider bwrap (déjà tracé D71) — sans quoi les tests de sécurité bypass sont skippés.
3. Cible prod : conteneur avec `--security-opt seccomp=unconfined` *ou* gVisor/kata, pour que bwrap `--unshare-*` fonctionne.
4. Ajouter un **timeout + cap de nproc** confirmés même en bwrap (la docstring note que bwrap seul n'applique pas les caps CPU/mem → envisager `prlimit` en wrapper, déjà tracé D74).

### 5.4 SSRF (transport SSE)

**🟠 (contextuel).** Pour `transport="sse"`, le sandbox **ne wrappe pas** le serveur distant (documenté `client.py:213`) et **il n'y a pas d'allowlist d'URL** (différée Story 4.x, D60/D63). Le backend fait donc une **requête sortante vers une URL fournie à l'enregistrement** → surface **SSRF** classique (accès à des services internes, métadonnées cloud `169.254.169.254`, etc.).

**Mitigation actuelle** : enregistrement gaté par le flag + admin mono-utilisateur. **Recommandation** : avant tout multi-user/exposition, **allowlist d'hôtes** + blocage des plages privées/link-local (RFC 1918, `169.254/16`, `::1`, `fd00::/8`) au moment du fetch SSE.

### 5.5 Injection de prompt (format_map)

**🟠 — finding CR P-02 encore OUVERT** (Story 2.7 en `review`, P-02 non appliqué). `m7_playground/service.py:131-142` :
```python
try:
    prompt_resolved = system_prompt_template.format_map(arguments)
except KeyError as exc:                      # ← SEULE exception attrapée
    raise ValidationError(...) from exc
```
**Deux problèmes :**
1. **Exceptions non gérées → 500 + fuite.** `format_map` sur un template contenant `{0}`, `{x:!r}`, `{a.b}` lève `IndexError` / `ValueError` / `AttributeError` — **non attrapées** → 500 Internal Server Error au lieu d'un 422 propre, avec risque de **stack trace** dans la réponse/les logs.
2. **Divulgation par field-access.** `str.format_map` autorise l'accès **attribut/index** (`{x.__class__...}`) — le vecteur classique de *format-string info-disclosure* en Python. Ici `arguments` vient de JSON (types simples), mais le **template** peut référencer des variables serveur, et `prompt_resolved` est **renvoyé tel quel au caller** → un template mal conçu peut exfiltrer un contexte serveur.

**Correction (AVANT/APRÈS) :**
```python
# APRÈS — (a) attraper toute la famille, (b) formatter restreint qui interdit
# l'accès attribut/index, (c) décider si prompt_resolved doit être renvoyé.
import string
class _SafeFormatter(string.Formatter):
    def get_field(self, field_name: str, args, kwargs):
        if any(c in field_name for c in ".[]"):          # bloque {x.__class__}, {x[0]}
            raise ValueError(f"unsupported field reference: {field_name!r}")
        return super().get_field(field_name, args, kwargs)

try:
    prompt_resolved = _SafeFormatter().vformat(system_prompt_template, (), dict(arguments))
except (KeyError, IndexError, ValueError, AttributeError) as exc:
    raise ValidationError(
        detail="system_prompt could not be resolved from the provided arguments",
        context={"template_id": str(template_id)},   # PAS de détail exploitable
    ) from exc
```
> Bloque la traversée d'attributs, transforme tout échec de résolution en **422 sanitizé**, et évite la fuite de trace. **Sévérité 🟠 · effort faible · à traiter avec le fix-batch 2.7.** *(À décider produit : `prompt_resolved` doit-il figurer dans la réponse ? Si le template peut porter des variables serveur → non, ou derrière un flag.)*

### 5.6 Fuite de redaction & secrets at-rest

**🟠 — fuite confirmée reachable** (instruit en 3.1.4). La route **`GET /tools/servers/{id}`** (`router.py:110-124`) renvoie `ToolServerDetailView` avec `connection_config=_redact_connection_config(...)`. Or `_redact_connection_config` ne masque que **6 clés** (`authorization, token, password, api_key, secret, auth`) alors que `_redact_arguments` en couvre **12** (`bearer, credential, passphrase, client_secret, _key, key_`, …). **Conséquence concrète** : un serveur enregistré avec un secret sous une clé `bearer`/`credential`/`passphrase`/`access_key` voit ce secret **renvoyé en clair** dans la réponse de détail (et potentiellement re-loggué).

**Correction** = 3.1.4 (unifier sous `shared/security/redaction.py` avec le prédicat `is_secret_key` unique). La déduplication **ferme la fuite** — d'où sa priorité sécurité.

**🟡 Secrets at-rest** : `tool_servers.connection_config` est **stocké en clair** (JSONB) — chiffrement Fernet différé Story 9.2 (D54). Acceptable en dev mono-user ; **à chiffrer avant tout environnement partagé** (un dump DB = tous les credentials MCP).

### 5.7 Rate limiting absent

**🟠 (contextuel).** `slowapi` est **déclaré en dépendance** mais **n'est câblé nulle part** (`grep` : aucun `Limiter`/`@limit` dans `src/` — seules les *classes d'exception* `RateLimitError` existent). Les endpoints **Playground `run`** (coût LLM réel) et **`invoke_tool`** (exécution/compute) sont donc **sans limite de débit**.

**Impact** : cost-DoS (facture LLM), épuisement de ressources, brute-force du token. **Mitigation** : mono-utilisateur derrière auth (le rate-limiting est planifié Story 9.5). **Recommandation** : câbler au moins un **limiter basique** sur `run`/`invoke`/l'auth **avant** multi-user/exposition, et **retirer `slowapi` des deps si non utilisé à court terme** (dépendance morte = surface inutile — lien Étape 3 code mort).

### 5.8 Secrets & logs

**Verdict : 🟢 solide, un caveat.**
- **SOPS** (`.sops.yaml`, `.env.encrypted`) + **gitleaks** en pre-commit **et** CI (`.gitleaks.toml`) → secrets hors du repo. ✅
- **Redaction des logs** (`shared/llm/redaction.py`) : masque par **préfixes connus** (`sk-ant-`, `sk-proj-`, `sk-`, `pa-`). ✅ mais **🟡 caveat documenté** (`interface.py:53-63`) : la redaction **ne couvre que ces préfixes** — un secret arbitraire (sans préfixe connu) dans un contenu utilisateur/outil **échappe non masqué** dans les completions et events. C'est explicitement de la responsabilité de l'appelant. → converge avec 5.6 : un **module de redaction unifié** (patterns de clés **+** préfixes) élargirait la couverture.

### 5.9 Synthèse (Étape 5)

| # | Constat | Sév. (mono-user) | Devient (multi-user/exposé) | Impact | Effort |
|---|---|:--:|:--:|:--:|:--:|
| 5.5 | `format_map` : exceptions non gérées (500 + echo) — **CR P-02 ouvert** | 🟠 | 🟠 | Élevé | Faible |
| 5.6 | Fuite redaction `connection_config` (route authentifiée) | 🟠 | 🔴 | Élevé | Moyen |
| 5.3 | Sandbox dégradé `setrlimit` = pas d'isolation réseau/FS si flag on | 🟠 | 🔴 | Critique* | Moyen |
| 5.7 | Rate limiting absent (`slowapi` non câblé) | 🟠 | 🔴 | Élevé | Faible |
| 5.4 | SSRF SSE (pas d'allowlist d'URL) | 🟠 | 🔴 | Élevé | Moyen |
| 5.6b | `connection_config` en clair at-rest (Fernet différé) | 🟡 | 🟠 | Moyen | Moyen |
| 5.2 | Token statique unique + gate admin loopback (TODO 1.7) | 🟡 | 🟠 | Moyen | Moyen |
| 5.8 | Redaction logs préfixes-only | 🟡 | 🟡 | Moyen | Faible |

*\*Critique uniquement si le flag est activé dans un env où bwrap est inopérant.*

**Quick wins sécurité prioritaires (faible effort, fort impact) :**
1. **5.5** — corriger `format_map` (famille d'exceptions + formatter restreint) : à intégrer au **fix-batch 2.7**.
2. **5.6** — unifier la redaction (= 3.1.4) : ferme la fuite *et* dédुplique.
3. **5.3** — **refus/warning au boot si flag=on ET backend=setrlimit** : transforme un risque silencieux en garde-fou explicite.
4. **5.7** — câbler un limiter basique sur `run`/`invoke`/auth.

**Message clé** : la sécurité est **pensée**, pas improvisée (flag deny-by-default, auth solide, SOPS/gitleaks, audit immuable). Le travail restant est de (a) fermer les 2 findings *déjà identifiés mais non appliqués/incohérents* (5.5 P-02, 5.6 redaction) et (b) **matérialiser des garde-fous explicites** autour du seul vrai danger — l'exécution d'outils — avant tout changement de threat model.

---

> ✅ **Étape 5 validée.**

---

## Étape 6 — Tests (TDD/FIRST)

**Résumé exécutif.** La discipline de test est **le point fort le plus tangible du projet** : **533 tests** pour **11 474 LOC** de prod (ratio 1,16:1), une **vraie pyramide** (unit mockés / integration sur Postgres réel via `testcontainers` / security / spike), des tests **AAA propres** avec assertions **comportementales** (pas seulement sur les valeurs de retour), et une **large couverture de cas limites** (404/403/409/422/503, timeouts, bornes, redaction, isolation). Deux réserves : (1) **la couverture (mesurée ici à 83,81 %) n'est ni mesurée ni gatée en CI** (B-02), et **le sandbox de sécurité n'est couvert qu'à 65,8 %** car ses tests d'isolation sont skippés (bwrap absent) ; (2) **deux trous de cas limites tombent exactement sur les bugs des Étapes 4-5** (injection `format_map` 5.5 ; incohérence de redaction 5.6) — les tests *valident la divergence* au lieu de l'attraper. Verdict : 🟢 **au-dessus du niveau attendu**, avec des correctifs ciblés à fort ratio.

### 6.1 Verdict & principes FIRST

| Principe FIRST | Verdict | Preuve |
|---|:--:|---|
| **F**ast | 🟢 | Les tests unitaires mockent les repos (`AsyncMock`) et le LLM → aucun I/O (`tests/unit/m7_playground/test_service.py`). |
| **I**solated / Independent | 🟢 | Chaque test construit son propre service (`_make_service`) ; `monkeypatch` auto-annulé ; pas d'état partagé. |
| **R**epeatable | 🟢 | Déterministe ; `testcontainers` fournit un Postgres éphémère reproductible sans pré-provisionnement (`conftest.py`). |
| **S**elf-validating | 🟢 | Assertions explicites, `pytest.raises` typés, pas d'inspection manuelle. |
| **T**imely | 🟡 | Tests **numérotés par tâche** (T4.6…) et livrés *avec* la story — mais **single-pass** (`bmad-dev-story`), donc **test-alongside**, pas strictement **test-first (TDD red-green)**. |

> **Nuance TDD honnête** : les artefacts (implémentation single-pass puis revue adversariale) ne prouvent pas un cycle *red-green-refactor*. Le *résultat* respecte FIRST, mais parler de « TDD » serait inexact — c'est du **test-driven-by-acceptance-criteria**, discipliné mais a posteriori.

### 6.2 Pyramide & isolation

Vraie **pyramide de tests** (marqueurs mesurés) :
- **Unit** (59 fichiers) — repos/LLM mockés, rapides. `@pytest.mark.asyncio` ×128.
- **Integration** (52 fichiers) — `@pytest.mark.integration` ×75, **Postgres réel** (`testcontainers` pgvector:pg17) + serveur MCP mocké. Y compris des tests qui **vérifient les contrats d'architecture** (`test_import_linter_contract3/5.py`).
- **Security** (`@pytest.mark.security` ×5) — fork-bomb, deny réseau/FS (skippés proprement quand bwrap inopérant — cf 5.3).
- **Spike** (7 fichiers) — validation LangGraph, job CI gating séparé.

**Isolation** : `conftest.py` **skip automatiquement** si Docker absent (« useful in constrained CI ») — bonne résilience. Fixtures `session`-scopées pour le container (coût amorti), fonction-scopées pour les données.

### 6.3 Qualité des tests (AAA, mocks)

**Exemplaire.** `tests/unit/m7_playground/test_service.py` :
- **AAA** limpide, **helpers de construction** (`_make_service`, `_completion`) → zéro duplication de setup.
- **Assertions comportementales** — le test ne vérifie pas que la valeur de retour, il **compte les événements publiés** :
```python
assert len(publish_calls) == 1                       # AC5 — exactement 1 audit event
assert publish_calls[0][1].status == "llm_error"     # bon statut sur chemin d'échec
llm_router.complete.assert_not_called()              # 422 AVANT tout appel LLM
assert publish_calls == [], "404 path MUST NOT publish audit event"
```
- **Test d'isolation structurel** (`test_run_isolation_no_instance_repo_call`) par **introspection de signature** — garde AC2 (pas d'`instance_repo`/`memory_chunk_repo`/`event_bus`). Astucieux, mais 🟡 **fragile** (teste la forme du constructeur, pas le comportement runtime) : un contournement passant par un import direct ne serait pas attrapé.

**Réserve 🟡 — mocks encodant l'anémie.** Les tests injectent des configs `dict` brutes (`template.config = {"system_prompt": "...", "llm": {...}}`), reproduisant le `JSONB` non typé (Étape 4). Des **Value Objects** (`AgentConfig`) rendraient les tests moins *stringly-typed* et détecteraient les configs invalides à la construction.

### 6.4 Couverture des cas limites

**Large et pertinente** (noms de tests réels) :

| Cas limite | Couvert ? | Test |
|---|:--:|---|
| Template/outil/serveur inexistant → 404 | ✅ | `test_invoke_tool_unknown_tool_returns_404`, `…_template_not_found…` |
| Flag désactivé → 403 | ✅ | `test_invoke_tool_disabled_by_flag_returns_403` |
| Nom dupliqué → 409 (double activation) | ✅ | `test_create_tool_server_duplicate_name_returns_409` |
| Variable manquante → 422 | ✅ | `test_run_missing_variable_raises_422` (KeyError) |
| Outil non assigné → 422 | ✅ | `test_run_enabled_tool_ids_filter_rejects_unassigned` |
| Timeout → 503 | ✅ | `test_invoke_tool_timeout_returns_503` + bornes schéma (`…lower_bound`, `…upper_bound_600s`) |
| Sortie non-JSON → `parsed_output=None` | ✅ | `test_run_parsed_output_none_when_raw_is_not_json` |
| Audit best-effort n'écrase pas le résultat | ✅ | (integration `test_invoke_tool_e2e`) |
| Redaction des secrets (args) | ✅ | `test_redact_arguments_masks_secret_synonyms…`, `…depth_bounded…` |
| Rejet de champs extra / bornes | ✅ | `test_invoke_tool_request_rejects_extra_fields` |

**🟠 Deux trous — exactement sur les bugs des Étapes 4-5 :**
1. **Injection `format_map` (5.5) non testée.** `test_run_missing_variable` ne couvre que `KeyError`. **Aucun test** avec `system_prompt="{0}"` (IndexError), `"{x:!r}"` (ValueError) ou `"{a.b}"` (AttributeError) → le bug 5.5 (500 + fuite) **n'a aucun test qui le révélerait**. Un tel test *échouerait aujourd'hui* et **piloterait le correctif**.
2. **Incohérence de redaction (5.6) non attrapée.** Il existe `test_redact_connection_config_masks_top_level_secret_keys` **et** `test_redact_arguments_masks_secret_synonyms…` — mais **chaque fonction est testée contre son *propre* jeu de clés**. Aucun test ne vérifie que **les deux chemins masquent la même clé canonique** → la divergence (`bearer`/`credential` masqués côté args mais **pas** côté `connection_config`) **passe en vert**. Un test *table-driven partagé* (« pour chaque clé secrète canonique, les 2 redactions masquent ») échouerait et exposerait la fuite.

> Leçon : **une suite exhaustive peut valider un comportement incohérent** si aucun test ne croise les deux implémentations. C'est l'argument le plus fort pour l'unification de redaction (3.1.4/5.6).

### 6.5 Couverture mesurée (run réel)

**Run exécuté** en parité CI (`docker run … agentive-backend:dev uv run pytest --ignore=tests/spike --cov`), Postgres éphémère via `testcontainers`. Résultat : **575 passed, 4 skipped, 0 failed** en **4 min 33 s**.

> ### 📊 Couverture globale : **83,81 %** (3 319 statements, 445 non couverts · 602 branches, 98 partielles)

C'est une **couverture réelle solide** (et honnête : lignes *exécutables*, `branch=true`). Les **modules métier audités sont excellents** ; les creux sont sur l'**infra de bootstrap** et — point notable — sur le **sandbox de sécurité**.

**Modules audités (cœur) :**
| Module | Couverture | Lignes non couvertes (clés) |
|---|:--:|---|
| `m2_agent_registry/router.py` | **97,1 %** | 68 |
| `m2_agent_registry/service.py` | **95,5 %** | branches d'échec `emit_notify` (209-210, 372-373, 499-500…) |
| `m7_playground/service.py` | **92,9 %** | chemin audit LLM-error partiel (293-305) |
| `m5_tool_hub/service.py` | **90,2 %** | branches d'audit/except (552-562, 617-618, 685-686) |
| `m5_tool_hub/router.py` | **88,3 %** | 171-173 (import inline `detect_sandbox_backend`, cf 1.6) |
| `shared/llm/router.py` | **86,3 %** | branches de fallback rares (229-232, 321-322) |

**Les 3 creux à noter :**
| Module | Couverture | Cause |
|---|:--:|---|
| `infra/db/session.py` | **40,9 %** | bootstrap moteur/engine — difficile à unit-tester (acceptable) |
| **`infra/mcp/sandbox.py`** | **65,8 %** | 🔴 **les tests de bypass sécurité sont SKIPPÉS** (bwrap inopérant ici → 4 tests `@pytest.mark.security` skippés) — lignes 324-374 non exécutées |
| `infra/mcp/client.py` | **73,2 %** | chemins d'erreur de transport (stdio/sse) peu exercés |
| `shared/correlation.py` | **64,7 %** | branches de génération d'ID de repli |

> **🔴 Constat fort (croise 5.3).** Le module qui **fournit la barrière de sécurité** (`sandbox.py`) est **le moins couvert des modules importants (65,8 %)**, précisément parce que **ses tests d'isolation nécessitent un bwrap fonctionnel absent en CI/Docker** (4 skips). Autrement dit : *le code de sécurité le plus critique est celui dont on prouve le moins le comportement.* C'est l'argument le plus concret pour le **runner CI kernel-configuré** (defer D71) — sans lui, l'isolation réseau/FS n'est **jamais exécutée** par la suite.

**Conclusion couverture** : le problème **n'est pas le niveau (83,8 % est bon)**, mais qu'il est **invisible et non gaté** (B-02) — un refactor pourrait le faire chuter sans alerte — et que **les tests sécurité les plus importants ne tournent pas**. → activer `--cov --cov-fail-under=80` en CI **et** un runner bwrap-capable.

### 6.6 Synthèse (Étape 6)

| # | Constat | Sévérité | Impact | Effort | Ratio |
|---|---|:--:|:--:|:--:|:--:|
| 6.4a | Ajouter tests injection `format_map` (`{0}`, `{x.__class__}`) — **pilote le fix 5.5** | 🟠 | Élevé | Faible | ⭐⭐⭐ |
| 6.4b | Test partagé de cohérence de redaction — **révèle la fuite 5.6** | 🟠 | Élevé | Faible | ⭐⭐⭐ |
| 6.5 | Activer `--cov` + `--cov-fail-under` en CI (B-02) | 🟠 | Élevé | Faible | ⭐⭐⭐ |
| 6.3 | Test d'isolation structurel fragile (introspection signature) → doubler d'un test runtime | 🟡 | Moyen | Faible | ⭐⭐ |
| 6.3b | Mocks encodant l'anémie → renforcer via VO (dépend Étape 4) | 🟡 | Moyen | Moyen | ⭐ |

**À créditer** : pyramide réelle, `testcontainers` (vraies dépendances), assertions comportementales, tests des **contrats d'architecture**, skip propre sans Docker. **La discipline de test est le meilleur signal de maturité du projet.** Le travail restant n'est pas d'en *écrire plus*, mais (a) de **mesurer/gater** la couverture et (b) d'ajouter **2 tests ciblés** qui transforment la suite en *filet de sécurité* pour les bugs 5.5/5.6 au lieu de spectateur.

---

> ✅ **Étape 6 validée** (couverture réelle **83,81 %** intégrée §6.5).

---

## Étape 7 — Synthèse & roadmap

### 7.1 Verdict global & scorecard

**Agentive est un PoC nettement au-dessus de la moyenne** — pas par le nombre de features (3 modules sur 13), mais par la **qualité d'ingénierie du socle** : couplage contraint mécaniquement, ports/adapters LLM & MCP exemplaires, intégration par événements, sécurité pensée, et une **couverture de tests réelle de 83,8 %**. Les faiblesses ne sont pas des accidents mais un **manque unique et cohérent** — l'absence d'un cœur de domaine — dont découlent la plupart des 🟠. Aucun 🔴 dans le threat model mono-utilisateur ; deux findings sécurité **passent 🔴 au multi-utilisateur**.

| Axe | Note | Justification (1 ligne) |
|---|:--:|---|
| Architecture stratégique & couplage | **A** | 5 contrats import-linter en CI, contextes bornés, intégration par événements (§0.5, 4.1) |
| SOLID | **B+** | OCP/LSP/DIP-LLM exemplaires ; SRP & DIP-persistance à travailler (§2) |
| Clean Code / lisibilité | **A−** | PEP8, docstrings NumPy, noms parlants ; réserves de cohérence FR/EN & fonctions longues (§1) |
| DRY | **B** | Peu de copier-coller littéral ; 4 abstractions manquantes (§3) |
| DDD tactique | **C** | Domaine anémique, config en JSONB non typé — *défendable pour un PoC* (§4) |
| Sécurité | **B** | Posture consciente (deny-by-default, SOPS, auth solide) ; 4 findings à fermer (§5) |
| Tests | **A−** | 83,8 %, vraie pyramide, `testcontainers` ; non gaté + sandbox sous-testé (§6) |
| **Global** | **B+ / A−** | **Socle de très bonne facture ; le chantier est l'émergence d'un domaine.** |

### 7.2 Tableau consolidé des constats

**~40 constats** dédupliqués et regroupés. Tri par **sévérité puis ratio impact/effort**. « Renvois » = constats liés (à traiter ensemble).

#### 🟠 Majeurs
| ID | Constat | Étape | Effort | Ratio | Renvois |
|---|---|:--:|:--:|:--:|---|
| **A-01** | Injection `format_map` : exceptions non gérées (500 + echo) — **CR P-02 ouvert** | 5.5 | Faible | ⭐⭐⭐ | 6.4a |
| **A-02** | Redaction éclatée ×3 + **fuite `connection_config`** reachable | 3.1.4 / 5.6 | Moyen | ⭐⭐⭐ | 6.4b, 5.8 |
| **A-03** | Sandbox dégradé `setrlimit` = pas d'isolation réseau/FS si flag on | 5.3 | Moyen | ⭐⭐ | 6.5 |
| **A-04** | Rate-limiting absent (`slowapi` non câblé) | 5.7 | Faible | ⭐⭐⭐ | 3.3 |
| **A-05** | SSRF transport SSE (pas d'allowlist d'URL) | 5.4 | Moyen | ⭐⭐ | — |
| **A-06** | `publish + notify best-effort` dupliqué ×7 | 3.1.2 | Faible | ⭐⭐⭐ | — |
| **A-07** | `lookup-or-404` dupliqué ×12 | 3.1.1 | Moyen | ⭐⭐ | — |
| **A-08** | Config métier en `JSONB` non typé → VO `AgentConfig` | 4.3/4.4 | Moyen | ⭐⭐ | 1.4, 6.3b |
| **A-09** | Méthodes multi-responsabilités (`invoke_tool` 224, `run` 185) + double chemin d'audit | 2.1a / 3.1.3 | Élevé | ⭐⭐ | 1.5 |
| **A-10** | Classe `m2` multi-responsabilités (7 repos) | 2.1b | Élevé | ⭐ | — |
| **A-11** | DIP : services couplés aux repositories concrets (pas de ports) | 2.5a | Moyen | ⭐⭐ | — |
| **A-12** | Invariants dispersés (DB/Pydantic/service), agrégat sans comportement | 4.5 | Élevé | ⭐⭐ | — |
| **A-13** | Cadrer la règle d'expiration/rétention **avant** d'implémenter `m4` | 4.5b | Faible* | ⭐⭐⭐ | — |
| **A-14** | Couverture non gatée + **sandbox couvert à 65,8 %** (tests skippés) | 6.5 | Faible | ⭐⭐⭐ | B-02 |
| **A-15** | Complexité cyclomatique non gouvernée (pas de `radon`/seuil) | B-01 | Faible | ⭐⭐ | — |

#### 🟡 Mineurs / suggestions
| ID | Constat | Étape | Ratio |
|---|---|:--:|:--:|
| M-01 | Valeurs magiques / timeouts dupliqués → constantes | 1.4/3.1.5 | ⭐⭐⭐ |
| M-02 | Commentaires FR → EN (~40 lignes) | 1.2 | ⭐⭐⭐ |
| M-03 | Commentaires « archéologiques » (IDs de revue) | 1.7 | ⭐⭐ |
| M-04 | Imports en milieu de fonction (+ DI `detect_sandbox_backend`) | 1.6/2.5b | ⭐⭐ |
| M-05 | `CheckConstraint` DB manquants (`archetype`, `namespace.type`) | 4.4b | ⭐⭐⭐ |
| M-06 | Dead-code gate (`vulture`) absent + littéral `tool_error` spéculatif | 3.3 | ⭐⭐⭐ |
| M-07 | Suffixes de DTO incohérents (`View`/`Response`) | 1.3 | ⭐ |
| M-08 | ISP : scinder `LLMProvider` → `Completer`/`RawProviderAccess` | 2.4 | ⭐⭐ |
| M-09 | Secrets `connection_config` en clair at-rest (Fernet différé) | 5.6b | ⭐ |
| M-10 | Token statique unique + gate admin loopback (TODO 1.7) | 5.2 | ⭐ |
| M-11 | Redaction logs préfixes-only | 5.8 | ⭐⭐ |
| M-12 | Test d'isolation structurel fragile (introspection signature) | 6.3 | ⭐⭐ |
| M-13 | `shared` volumineux (43 %) — surveiller le fourre-tout | B-05 | ⭐ |

*\*A-13 : effort faible car **préventif** (conception avant code).*

### 7.3 Le chantier structurant unique

**Le message central de l'audit** : **A-08, A-09, A-10, A-11, A-12 + A-02 ne sont pas 6 tickets — c'est UN chantier** : *faire émerger un cœur de domaine*. Ils partagent la même cause racine (pas de couche domaine) et la même solution :

```
Value Objects (AgentConfig, LLMParams, Archetype, Version, RetentionPolicy)
    └─> supprime la config JSONB devinée (A-08) + les défauts magiques (M-01)
Agrégats avec comportement (AgentTemplate.revise(), assignation d'outils)
    └─> résout SRP-classe (A-10) + invariants dispersés (A-12)
Décomposition des méthodes + chemin d'audit unique
    └─> résout SRP-méthodes (A-09) + double audit (3.1.3)
Ports de persistance (Protocols) + module redaction partagé
    └─> résout DIP (A-11) + redaction éclatée (A-02)
```
> Le planifier comme **un épic technique unique** (et non 6 correctifs isolés) évite les demi-refactors et fait converger 6 🟠 d'un coup. **Déclencheur d'investissement** : le 2ᵉ département (Epic 5) ou dès que 3 contextes lisent le `config` JSONB.

### 7.4 Roadmap de remédiation (4 vagues)

**Vague 0 — Quick wins hygiène (≈ 1–2 j, risque quasi nul, 1 commit `chore`).**
`M-01` (constantes) · `M-02` (FR→EN) · `M-03` (comments) · `M-04` (imports/DI) · `M-05` (CHECK DB) · `M-06` (`vulture` + `tool_error`) · `A-06` (`notify_best_effort`) · `A-14`/`A-15` (activer `--cov --cov-fail-under=80`, `radon`/`xenon` en CI).
→ *Nettoie l'essentiel du bruit + met en place les garde-fous d'outillage.*

**Vague 1 — Sécurité à fermer (≈ 2–4 j, AVANT tout le reste).**
`A-01` (`format_map` + test 6.4a) · `A-02` (redaction unifiée + test 6.4b — **ferme la fuite**) · `A-03` (refus/warning boot si flag=on & backend=setrlimit) · `A-04` (limiter basique sur `run`/`invoke`/auth). **+ finir le fix-batch Story 2.7** (4 High / 13 Med / 21 Low restants) → passer 2.7 `done`.
→ *Ferme les findings déjà identifiés mais non appliqués/incohérents.*

**Vague 2 — Émergence du domaine (≈ 1–2 sem, l'épic structurant §7.3, par paliers).**
Palier 1 (VO + `from_mapping` unique + `A-13` rétention) → Palier 2 (agrégats, décomposition SRP `A-09`, split `A-10`, `A-07` lookup-or-404, ports `A-11`, ISP `M-08`).
→ *Transforme le socle CRUD en domaine testable ; à lancer au déclencheur §7.3.*

**Vague 3 — Prérequis avant multi-utilisateur / prod (Epics 9 & 12).**
`M-09` Fernet secrets at-rest · `M-10` auth multi-user + fermer admin loopback · `A-05` allowlist SSRF · **runner CI bwrap-capable (D71)** pour exécuter enfin les tests sécurité (débloque `A-14`) · rate-limiting complet (Story 9.5) · audit-bypass cleanup (Story 9.1, dette `B-06`).
→ *Les 2 findings 🟠→🔴 (A-02, A-03) doivent être clos ici au plus tard.*

### 7.5 Gaps outillage & CI

La CI est solide (lint, mypy strict, import-linter, testcontainers) mais **3 garde-fous manquent** — tous *quick wins* :
| Gap | Effet | Correctif |
|---|---|---|
| Pas de `--cov` en CI (`B-02`) | Couverture (83,8 %) peut chuter sans alerte | `pytest --cov --cov-fail-under=80` |
| Pas de gate complexité (`B-01`) | Fonctions 224 LOC passent | `radon cc`/`xenon --max-absolute C` |
| Pas de dead-code gate (`M-06`) | Code mort retiré à la main | `vulture src/ --min-confidence 80` |
| Runner sans bwrap (`D71`) | **Tests sécurité skippés → sandbox 65,8 %** | runner kernel-configuré / seccomp desserré |
| `security.yml`/`sbom.yml` = stubs | Pas de scan CVE/SBOM | activer trivy/dependabot + cyclonedx |

### 7.6 Ce qu'il faut créditer

Un audit équilibré doit nommer les forces — ici, elles sont **remarquables pour un PoC** :
- 🏆 **Couplage contraint mécaniquement** (5 contrats import-linter en CI) — rare même en entreprise.
- 🏆 **`LLMRouter` + `Protocol` + callback `on_fallback`** = référence OCP/DIP, avec la motivation *écrite* dans le code.
- 🏆 **Intégration par événements de domaine** (outbox transactionnel + event bus) = DDD stratégique de manuel.
- 🏆 **Sécurité pensée** : deny-by-default, auth constant-time anti-bypass, SOPS+gitleaks, audit immuable partitionné.
- 🏆 **Tests** : 83,8 % réels, vraie pyramide, `testcontainers`, tests des *contrats d'architecture* eux-mêmes.
- 🏆 **Traçabilité exemplaire** : ADR, spikes/benchmarks, dette *tracée* et rattachée à des stories, revues adversariales.

### 7.7 Conclusion

Agentive n'a **pas de problème de qualité de code au sens usuel** — le code est propre, typé, testé, documenté et architecturé avec discipline. Son unique **dette structurelle** est l'**absence d'un cœur de domaine** : la logique métier vit dans des services impératifs opérant sur des `dict` JSONB, ce qui explique — *par une seule cause* — les frictions SRP, DIP-persistance, DRY-redaction et la difficulté à tester les règles métier isolément.

**Recommandation de priorité :**
1. **D'abord la sécurité** (Vague 1) — fermer A-01/A-02/A-03/A-04, qui sont soit déjà identifiés (P-02), soit des incohérences reachable.
2. **Puis l'outillage** (Vague 0) — quick wins qui *empêchent la régression* de tout le reste.
3. **Enfin, au bon moment, le domaine** (Vague 2) — pas trop tôt (YAGNI sur un PoC), mais **avant le 2ᵉ département** et **avant que `m4`/plusieurs contextes ne consomment le `config` JSONB**.

Le projet est un **excellent candidat** pour passer de « PoC très soigné » à « socle produit durable » : la fondation est là, le chemin est clair, et la discipline d'équipe (revues, ADR, contrats, tests) est déjà celle d'une équipe mûre.

---

*Fin de l'audit — 7 étapes, ~40 constats, couverture réelle mesurée à 83,81 %. Document produit sur code réel, sans supposition non sourcée.*
