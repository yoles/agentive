---
stepsCompleted: [1, 2, 3, 4]
status: 'complete'
completedAt: '2026-04-19'
inputDocuments:
  - 'prd.md'
  - 'architecture.md'
  - 'ux-design-specification.md'
  - 'brainstorming-session-2026-03-18-151800.md'
  - 'brainstorming-session-2026-03-18-142205.md'
  - 'ux-design-directions.html'
workflowType: 'create-epics-and-stories'
project_name: 'Agentive'
user_name: 'John'
date: '2026-04-19'
---

# Agentive - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for Agentive, decomposing the requirements from the PRD, UX Design Specification, and Architecture into implementable stories.

## Requirements Inventory

### Functional Requirements

**1. Orchestration & Workflows (FR1-FR8)**

- **FR1:** John peut créer un workflow en définissant les agents participants, l'ordre d'exécution, et les conditions de branchement
- **FR2:** Le système peut exécuter un workflow multi-agents de bout en bout avec checkpointing (reprise en cas d'interruption)
- **FR3:** L'Orchestrateur peut router automatiquement les tâches simples via des règles déterministes sans appel LLM
- **FR4:** L'Orchestrateur peut escalader une décision de routage vers le LLM quand la confiance du routage déterministe est insuffisante
- **FR5:** Le système peut exécuter une Mise en Place automatique avant chaque workflow (vérification outils, mémoire, budget, risques)
- **FR6:** Le système peut estimer le coût, le temps, et le chemin d'exécution probable d'un workflow avant lancement (Dry Run Prédictif)
- **FR7:** John peut interrompre, relancer, ou annuler un workflow en cours d'exécution
- **FR8:** Le système peut planifier l'exécution récurrente de workflows via le Scheduler (M11)

**2. Agents & Configuration (FR9-FR15)**

- **FR9:** John peut créer un agent à partir d'un template d'archétype (Orchestrateur, Chercheur, Analyste, Producteur, Stratège, Contrôleur, Veilleur, Communicateur)
- **FR10:** John peut configurer l'identité, le system prompt, les contrats input/output, le modèle LLM, et la politique d'erreur d'un agent
- **FR11:** John peut configurer un agent via un mode wizard guidé ou un mode expert direct
- **FR12:** Le système peut distinguer agent-template (définition) et agent-instance (exécution en cours)
- **FR13:** Les agents peuvent communiquer via des contrats élastiques (noyau obligatoire + zone flexible enrichissable)
- **FR14:** Le Contrôleur peut retourner une review conversationnelle avec commentaires localisés et niveaux de sévérité
- **FR15:** Le Contrôleur peut utiliser un modèle LLM ou des paramètres différents de l'agent qu'il contrôle

**3. Mémoire & Connaissances (FR16-FR21)**

- **FR16:** Le système peut stocker et retrouver des chunks mémoire via recherche vectorielle (pgvector)
- **FR17:** John peut définir des namespaces mémoire isolés par département et par projet
- **FR18:** Le système peut appliquer un TTL par chunk et archiver automatiquement les données expirées
- **FR19:** Le système peut scorer la pertinence des chunks en tenant compte de la décroissance temporelle
- **FR20:** Le système peut injecter proactivement les chunks pertinents dans le contexte d'un agent (Push Memory) avec marquage explicite
- **FR21:** John peut purger manuellement des chunks mémoire spécifiques

**4. Outils & Intégrations (FR22-FR24)**

- **FR22:** Le système peut connecter des outils MCP aux agents via le Tool Hub (M5)
- **FR23:** John peut assigner des outils spécifiques à chaque agent
- **FR24:** Le système peut exécuter les outils dans un environnement sandboxé

**5. Interface Utilisateur & Interaction (FR25-FR33)**

- **FR25:** John peut interagir avec les agents via une interface Chat avec streaming SSE
- **FR26:** John peut valider, rejeter, ou modifier un output d'agent dans le Chat (human-in-the-loop)
- **FR27:** John peut consulter un Dashboard avec métriques, alertes, statuts par département et par projet
- **FR28:** John peut consulter le résumé automatique généré par le Sprint Reporter dans le Dashboard
- **FR29:** John peut naviguer du Trace Explorer vers la Config d'un agent ou vers le Chat en un clic (navigation contextuelle)
- **FR30:** John peut tracer l'exécution complète d'un workflow dans le Trace Explorer (sources → données → raisonnement → décision)
- **FR31:** John peut voir les métriques en overlay sur chaque nœud du Trace Explorer (métriques contextuelles)
- **FR32:** Le système peut envoyer des notifications proactives quand un seuil d'alerte est dépassé
- **FR33:** John peut configurer les seuils d'alerte par agent et par métrique

**6. Sécurité & Audit (FR34-FR38)**

- **FR34:** Le système peut tracer toute action (agent ou humain) dans un audit trail persistant
- **FR35:** Le système peut chiffrer les données sensibles at-rest (clés API, credentials, données clients)
- **FR36:** John peut gérer les clés API des providers LLM (ajout, rotation, suppression)
- **FR37:** Le système peut appliquer des budget caps par département et par workflow
- **FR38:** Le système peut gérer les rate limits des providers LLM (queues, retries avec backoff, répartition)

**7. Monitoring & Qualité (FR39-FR42)**

- **FR39:** Le système peut mesurer et afficher le score qualité de chaque output d'agent
- **FR40:** Le système peut mesurer le taux de retry par agent et sa tendance temporelle
- **FR41:** Le système peut identifier les retries sur des patterns déjà rencontrés (corrélation retry × mémoire)
- **FR42:** Le système peut exécuter un benchmark mensuel de recall mémoire (20 requêtes, % chunks pertinents dans top-5)

**8. Département Dev — Pôle Pilote (FR43-FR53)**

- **FR43:** Le Pôle Dev peut fonctionner avec 9 agents spécialisés : Dev Lead, Code Researcher, Architect Analyst, Code Producer, Code Reviewer, Test Engineer, CI/CD Watcher, Doc Writer, Sprint Reporter
- **FR44:** Le Dev Lead peut décomposer une tâche en sous-tâches et les assigner aux agents appropriés
- **FR45:** Le Code Reviewer peut valider ou rejeter le code produit avec des commentaires structurés
- **FR46:** Le CI/CD Watcher peut surveiller les pipelines et alerter sur les vulnérabilités ou échecs
- **FR47:** Le Sprint Reporter peut générer un résumé automatique des tâches complétées par période
- **FR48:** John peut tester un agent en isolation dans un Playground (entrée manuelle, exécution, inspection de l'output) sans lancer un workflow complet
- **FR49:** L'Agent Debugger peut analyser automatiquement les traces d'un agent en échec et proposer un diagnostic (prompt, mémoire, outil, input)
- **FR50:** John peut configurer des Event Hooks sur le bus d'événements (règles "quand X → déclencher Y" entre départements ou modules)
- **FR51:** Le Code Researcher peut explorer le codebase, trouver les fichiers pertinents, et analyser les dépendances pour une tâche donnée
- **FR52:** John peut configurer les namespaces mémoire selon 4 types (client, métier, opérationnelle, contextuelle) avec des règles de rétention distinctes par type
- **FR53:** Le système génère automatiquement un résumé de passage entre chaque étape d'un workflow pour réduire la consommation de tokens des agents suivants

### NonFunctional Requirements

**Performance**

- **NFR1:** Temps de réponse API backend < 500ms (p95) pour les endpoints non-LLM
- **NFR2:** Latence premier token SSE < 200ms après envoi de la requête au provider
- **NFR3:** Temps d'exécution workflow standard < 10 min bout en bout
- **NFR4:** Temps de boot Docker Compose < 60s pour l'ensemble du stack
- **NFR5:** Recherche vectorielle M4 < 200ms pour une requête avec reranking sur 10k chunks

**Sécurité**

- **NFR6:** Chiffrement at-rest AES-256 pour clés API, credentials, données clients
- **NFR7:** Isolation namespace — aucun cross-read possible entre namespaces sans autorisation explicite
- **NFR8:** Audit trail 100% des actions tracées, rétention ≥ 90 jours
- **NFR9:** Gestion des secrets — aucune clé API en clair dans le code, les logs, ou les traces
- **NFR10:** Sandbox outils MCP — exécution isolée, aucun accès réseau non-autorisé, timeout configurable

**Fiabilité**

- **NFR11:** Reprise sur interruption — tout workflow interrompu peut reprendre à partir du dernier checkpoint
- **NFR12:** Graceful degradation LLM — si un provider est down, fallback automatique sur un provider alternatif dans M3
- **NFR13:** Persistance données — aucune perte de données en cas de redémarrage Docker (volumes persistants PostgreSQL)
- **NFR14:** Gestion des timeouts — timeout configurable par agent et par outil, avec retry automatique et backoff exponentiel

**Observabilité**

- **NFR15:** Traçabilité bout en bout — chaque output traçable jusqu'à ses sources (prompt, mémoire, outils, inputs)
- **NFR16:** Logs structurés — tous les logs au format JSON structuré avec correlation ID par workflow
- **NFR17:** Métriques temps réel — Dashboard refresh < 2s, métriques à jour à ≤ 30s de latence
- **NFR18:** Alertes — notification < 60s après détection d'un seuil dépassé

**Intégration**

- **NFR19:** Protocole MCP — compatibilité avec les serveurs MCP standard (stdio et SSE)
- **NFR20:** Multi-provider LLM — support simultané de ≥ 2 providers LLM (Anthropic, OpenAI minimum)
- **NFR21:** API interne — tous les modules communiquent via des APIs REST internes documentées

### Additional Requirements

**Starter Template & Sprint 0 (CRITIQUE — Epic 1)**

- **AR1:** Starter composite manuel (shadcn CLI v4 + uv + scaffolding manuel) — pas de starter officiel retenu ; composite décidé dans ADR-001
- **AR2:** Story 0.1 (Sprint 0) — Scaffolding time-boxed 1-2 jours : init des 3 stacks (frontend Vite/shadcn/TanStack, backend FastAPI/uv, docker-compose pgvector), `justfile` racine, structure de dossiers (core/modules/api/infra), migration Alembic initiale (CREATE EXTENSION vector + index HNSW), tuning Postgres, pre-commit (gitleaks, ruff, eslint), `mise.toml` pour pinning versions
- **AR3:** Story 0.2 (Sprint 0, RISQUE CRITIQUE #1) — Spike M3 LangGraph : workflow minimal 2 agents + 1 quality gate de bout en bout. Gating : si échec, pivoter l'abstraction avant tout autre investissement
- **AR4:** Story 0.3 (Sprint 0) — Benchmark M4 pgvector sur 10k chunks synthétiques. Gating NFR5 (< 200ms) avant Sprint 1

**Infrastructure & Déploiement**

- **AR5:** Docker Compose self-hosted single-node avec services : db (`pgvector/pgvector:pg17`), backend (FastAPI/uvicorn), frontend (Vite dev), Caddy reverse proxy auto-HTTPS
- **AR6:** PostgreSQL 17 + extension pgvector 0.8+, tuning shared_buffers et work_mem calibré dès Sprint 0
- **AR7:** Backup `pg_dump` quotidien + rotation 7 jours + restore-test mensuel automatisé (alerte Dashboard M6 si échec)
- **AR8:** Health checks endpoints `/health` (liveness) + `/ready` (readiness avec deps DB) + Docker HEALTHCHECK
- **AR9:** CI/CD GitHub Actions (lint + test + build au push). Pas de deploy auto MVP — déploiement manuel via SSH
- **AR10:** Pinning des versions via `mise.toml` racine (Python 3.13, Node 22 LTS, uv latest)

**Core Modules Techniques (shared/)**

- **AR11:** Bus d'événements interne via PostgreSQL `LISTEN/NOTIFY` wrappé dans `core/event_bus/` + Outbox Pattern (table `outbox_events`) pour durabilité — migration Redis Streams préparée pour SaaS futur
- **AR12:** Repository Pattern dans `core/repositories/` (1 classe async par agrégat : AgentRepo, MemoryChunkRepo, WorkflowRepo, AuditEventRepo) — accès DB exclusif via ces repos
- **AR13:** Abstraction multi-LLM provider dans `core/llm/` — wrapping `langchain-anthropic` + `langchain-openai`, fallback automatique configurable par agent + escape hatch `raw_provider_call` pour features spécifiques (prompt caching, tools)
- **AR14:** Auth MVP — token statique dans env var `AGENTIVE_API_TOKEN` via header `Authorization: Bearer`, vérifié par middleware FastAPI. Endpoint `POST /api/v1/admin/rotate-token` avec audit log `token_used`
- **AR15:** Préparation Growth (dès Sprint 0) — table `users` (1 ligne owner John) + colonnes `tenant_id NULL` sur tables critiques (memory_chunks, workflows, audit_events, agent_instances, chunk_embeddings)

**Données & Mémoire**

- **AR16:** SQLAlchemy 2.0 async + Alembic (autogenerate + revue manuelle) + migrations initiales : extension vector, tables core, `outbox_events`, `audit_events` partitioned by month
- **AR17:** Index vectoriel HNSW (m=16 et ef_construction=64 comme valeurs de départ, paramètres ajustés dans Story 0.3 après benchmark itératif documenté)
- **AR18:** Embedding Router hybride dans M4 — 2 backends configurables par namespace : local FastEmbed + modèle `BAAI/bge-small-en-v1.5` (384 dims), cloud `text-embedding-3-small` OpenAI (1536 dims) + `voyage-3-lite` optionnel
- **AR19:** Stockage embeddings dans table séparée `chunk_embeddings` (chunk_id, model, embedding) avec index partiels par modèle pour permettre ajout de modèles futurs sans migration lourde
- **AR20:** Multi-namespace mémoire — 1 table `memory_chunks` + colonne `namespace` indexée + `tenant_id` nullable + RLS Postgres activée dès Sprint 0 sur tables critiques

**Sécurité (hardening)**

- **AR21:** Secrets management — `.env` chiffré avec SOPS + age (commit dans repo), variables Docker au runtime, pre-commit `gitleaks` + config `.gitleaks.toml` racine, GitHub secret scanning activé
- **AR22:** Chiffrement at-rest app-level via `cryptography` Fernet/AES-256 sur champs sensibles (clés API LLM, credentials MCP, données clients) — clé chiffrement séparée des données
- **AR23:** Sandbox MCP renforcée — `bubblewrap` (`bwrap`) installé dans l'image backend + profile sandbox par outil (namespaces réseau, mount read-only, whitelist binaires) + `setrlimit` en fallback + test de bypass dans CI
- **AR24:** Rate limiting via `slowapi` (stockage mémoire MVP, swap Redis si scaling)
- **AR25:** Caddy security headers (HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, CSP avec nonces) + CORS whitelist explicite (localhost:5173 dev, domaine Tailscale/Caddy prod)
- **AR26:** RLS Postgres active dès Sprint 0 sur `memory_chunks`, `workflows`, `audit_events`, `agent_instances`, `chunk_embeddings` — policy `tenant_isolation` avec `current_setting('app.tenant_id', true)::uuid`
- **AR27:** Audit events partitioned by month + REVOKE DELETE/UPDATE on partitions pour immutabilité + purge cron mensuelle (rétention ≥ 90j)
- **AR28:** 3 rôles Postgres créés dans init.sql : `agentive_app`, `agentive_audit_admin`, `agentive_owner`

**API & Communication**

- **AR29:** API REST + OpenAPI 3.1 versioning `/api/v1/...`, génération auto client TypeScript via `openapi-typescript`
- **AR30:** Gestion erreurs RFC 7807 Problem Details (`application/problem+json`) + champs custom (`correlation_id`, `agent_id`, `module`, `tenant_id`)
- **AR31:** Correlation ID (UUID v7 ou ULID) via middleware FastAPI header `X-Correlation-ID`, injecté dans contexte `structlog` + propagé dans events bus
- **AR32:** Streaming SSE via `sse-starlette` côté serveur + hook custom `useSSE` côté client (reconnect backoff exponentiel + circuit breaker, AbortController cleanup, tests Vitest en React StrictMode)
- **AR33:** Validation requêtes via Pydantic v2 strict mode + Zod côté frontend (miroir Pydantic schemas) dans React Hook Form
- **AR34:** Logs structurés via `structlog` JSON + processeurs (correlation_id auto, redaction secrets)
- **AR35:** Communication inter-modules EXCLUSIVEMENT via bus d'événements ou contrats `core/contracts/` — `import-linter` bloque les imports directs en CI
- **AR36:** Pagination — cursor-based (opaque base64) pour audit/traces, offset-based pour listes courtes

**Observabilité**

- **AR37:** OpenTelemetry SDK + export Prometheus dès Sprint 1 (alimente Dashboard M6) — champ `tenant_id` optionnel sur métriques, sampling rate configurable, décorateur `@no_trace` pour hot paths
- **AR38:** OpenTelemetry traces dès Sprint 3 (alimente Trace Explorer M12) — backend Tempo ou Jaeger sidecar Docker
- **AR39:** Logs aggregation MVP — stdout JSON capturé par Docker `json-file` driver. Loki + Promtail dès Sprint 4+

**Project Structure & Conventions**

- **AR40:** Structure backend hybride (feature-based) : `shared/` (event_bus, repositories, llm, auth, contracts, config) + `features/m2_agent_registry/`, `features/m3_workflow_engine/`, etc. + `api/` (routes par espace UI) + `infra/`
- **AR41:** Structure frontend : `src/routes/` (TanStack Router file-based : dashboard/, chat/, trace/, config/) + `components/ui/` (shadcn) + `components/custom/` + `api/` + `state/` + `lib/`
- **AR42:** Conventions nommage — snake_case (Python/DB/JSON), camelCase (TS), PascalCase (classes/composants). Barrel imports obligatoires (`from features.m3_workflow_engine import WorkflowEngine`), jamais d'imports profonds
- **AR43:** Feature publique barrel : chaque feature expose `__init__.py` (barrel) + `service.py` + `schemas.py` + `events.py` + `tests/`
- **AR44:** Tous les inputs externes wrappés dans `<user_input>...</user_input>` ou `<tool_output>...</tool_output>` avant LLM call (anti prompt injection)

### UX Design Requirements

**Design System & Tokens**

- **UX-DR1:** Design system basé sur shadcn/ui v4 + Tailwind CSS v4 + Radix Primitives — composants copiés dans le codebase (100% ownership)
- **UX-DR2:** Tokens CSS variables pour palette Dark (défaut) : `--background` `#09090B`, `--card` `#18181B`, `--muted` `#27272A`, `--primary` `#8B5CF6` (violet-500), `--destructive` `#EF4444`, `--ring` `#8B5CF6` — et équivalents Light
- **UX-DR3:** Couleurs sémantiques restreintes aux statuts d'exécution (workflow cards, agent states) : Success `#10B981`, Running violet-500, Pending neutral-500, Warning `#F59E0B`, Error `#EF4444`
- **UX-DR4:** Typographie Geist Sans v1.7.0 (primaire) + Geist Mono v1.7.0 (code/logs/IDs/timestamps), base 14px, scale modulaire ratio 1.2 (8 tokens text-xs à text-4xl)
- **UX-DR5:** Spacing scale base 4px Tailwind standard (8 tokens de 4px à 64px), border radius `--radius: 8px` par défaut
- **UX-DR6:** Theme switching via `next-themes` + classe `dark` sur `<html>` + `prefers-color-scheme` respecté + dark default
- **UX-DR7:** Toutes les couleurs respectent WCAG AA (≥ 4.5:1 texte normal, ≥ 3:1 texte large et éléments UI interactifs), testés dark ET light

**Composants shadcn/ui (foundation)**

- **UX-DR8:** Copier depuis shadcn/ui pour MVP les composants : Button, Input, Textarea, Card, Dialog, Sheet, Command (Cmd+K critique), Tabs, Select, Tooltip, Badge, Avatar, Separator, Skeleton, Toast, Accordion, Scroll Area

**Composants custom Agentive**

- **UX-DR9:** Composant `AgentCard` — avatar coloré par archétype, nom, badge archétype, status dot, metadata. Variants : compact / default / detailed. Accessibility : role=button, aria-label complet
- **UX-DR10:** Composant `WorkflowCard` (inline Chat) — header (titre, ETA), liste steps (status icon, agent, durée/ETA), bouton "Voir dans Trace". States : running/completed/error/paused. Variants : expanded/collapsed/mini. Accessibility : role=progressbar, live region
- **UX-DR11:** Composant `OutputCard` (inline Chat) — header (titre, badge score, status), preview tronqué avec fade-out, actions (Valider/Commenter/Voir Trace/Relancer). States : pending/validated/rejected/needs_review. Variants : code/document/data
- **UX-DR12:** Composant `MetricBlock` — label, valeur principale (tabular-nums), tendance (vert/rouge/muted), action inline. States : default/positive_trend/negative_trend/alert. Variants : standard/compact/detailed (mini-chart)
- **UX-DR13:** Composant `ChatMessage` — avatar, header (nom + timestamp), bubble markdown, inline optionnels (WorkflowCard/OutputCard). States : sending/sent/streaming/complete/error. Variants : user (droite)/agent (gauche coloré)/system (centré discret)
- **UX-DR14:** Composant `TraceNode` (Trace Explorer) — indent selon profondeur, status dot, nom opération (mono), durée (mono muted). Accessibility : tree keyboard navigation flèches, aria-expanded, aria-level
- **UX-DR15:** Composant `AlertBanner` — icône sévérité, texte (quoi/où/pourquoi), 1-2 actions, bouton fermeture si non-critique. States : critical/warning/info/resolved. Variants : banner/inline/toast
- **UX-DR16:** Composant `ModeToggle` — switch 2 modes mutuellement exclusifs (wizard/expert, dark/light). Variants : text-only/icon-only/icon+text. Accessibility : role=radiogroup, switch clavier
- **UX-DR17:** Composant `ArchetypeSelector` — grid 4x2 cartes archétypes (nom + icône), bordure violet sélectionnée. Variants : single (primaire) / multi (secondaires mode expert)
- **UX-DR18:** Composant `KeyboardShortcut` — `<kbd>` wrapper, touches combinées (⌘+K), background muted, mono font. Alt text pour screen reader

**Layout & Espaces UI**

- **UX-DR19:** Layout principal — Sidebar fixe 240px (collapsable à 56px icônes), Main content fluide max-width 1440px, Header optionnel 56px
- **UX-DR20:** Direction visuelle "Hybride Agentive" (validée) — densité varie par espace : Dashboard/Chat aérés (Trade Republic-style), Trace Explorer dense (Grafana-style, tabular nums, mono), Config wizard OU expert toggleable
- **UX-DR21:** Dashboard — grid CSS 12 colonnes, gap 24px, cartes dimensionnées en spans (4/6/8/12)
- **UX-DR22:** Chat — max-width conversation 720px centré, sidebar historique 280px à gauche, messages aérés
- **UX-DR23:** Trace Explorer — tree view 320-340px à gauche, détail panel fluide à droite (pas de max-width)
- **UX-DR24:** Config — wizard mode : 1 étape visible, progression en haut, max-width 640px centré — expert mode : accordions empilés pleine largeur
- **UX-DR25:** Routing 4 espaces via TanStack Router file-based : `dashboard/`, `chat/`, `trace/`, `config/`. URL-state préservé entre espaces via search params (deep-linkable, Navigation Contextuelle)

**Patterns UX**

- **UX-DR26:** Button Hierarchy — max 1 bouton primary par écran visible. Types : primary (violet-500) / secondary (border muted) / ghost (transparent) / destructive / icon-only (32x32 + tooltip obligatoire). Tailles : sm 28px / default 36px / lg 44px
- **UX-DR27:** Feedback pattern "Silence = Succès" — action réussie réversible → silence + état visuel ; irréversible → toast discret 4s bottom-right ; action en cours → bouton loading state ; erreur récupérable → inline ; erreur critique → AlertBanner. Jamais de modal "Action réussie"
- **UX-DR28:** Form patterns — label au-dessus (jamais placeholder comme label) : Label 13px medium / Input 14px padding 9x12 / Helper text 12px muted
- **UX-DR29:** Empty states — jamais de vide à regarder, chaque état propose une action : icône muted + titre court + sous-titre + bouton action primary
- **UX-DR30:** Loading states — Skeleton > Spinner pour zones de contenu, pulse max 2s cycle, `prefers-reduced-motion` → opacité statique
- **UX-DR31:** Error states — chaque erreur répond à Quoi/Pourquoi/Comment corriger : icône alerte + titre clair + explication + actions. Niveaux : recoverable (inline) / blocking (AlertBanner) / system (page dédiée). Pas de stack trace visible, langage humain, diff erreur utilisateur (amber) vs système (destructive)
- **UX-DR32:** Modal & Overlay patterns — éviter les modals sauf nécessité absolue. Dialog réservé aux confirmations d'actions destructives, créations rapides, saisies focalisées. Escape + clic overlay ferment. Sheet latéral pour config rapide ou preview
- **UX-DR33:** Navigation patterns — Command Palette Cmd+K (critique, via cmdk), sidebar fixe 4 espaces, breadcrumbs contextuels, slash commands dans le Chat (`/deploy`, `/review`, `/status`, `/traces`), autocomplétion `@` (agents) et `#` (projets)
- **UX-DR34:** Patterns novateurs — Multi-auteur dans Chat (messages de plusieurs agents identifiés par avatar/badge), Workflow inline (carte déroulable états temps réel), Validation inline (outputs dans cartes avec boutons Valider/Commenter/Voir Trace/Relancer), Retour de contrôleur conversationnel structuré
- **UX-DR35:** Tooltips — délai apparition 600ms, position intelligente (flip si bord), texte < 80 caractères, obligatoires sur tous boutons icon-only
- **UX-DR36:** Timestamps — format HH:MM (aujourd'hui) / DD/MM HH:MM (autres), tabular-nums, tooltip hover format complet, relative pour récents ("Il y a 12 min")

**Accessibility (WCAG 2.1 AA minimum)**

- **UX-DR37:** Focus management — ring violet-500 (`--ring`) de 2px avec offset 2px systématique, tab order logique suivant flow visuel, skip links pour bypass navigation
- **UX-DR38:** Keyboard navigation 100% — raccourcis globaux Cmd+1-4 (espaces), Cmd+K (palette), Cmd+/ (help), contextuels Chat Cmd+Entrée (envoi), Cmd+Shift+L (nouveau chat), Trace Explorer flèches + Entrée, Trap focus dans modals + restore au close
- **UX-DR39:** Screen readers — structure sémantique HTML5 (`<main>`, `<nav>`, `<section>`, headers h1-h6 hiérarchiques), ARIA labels sur tous boutons icon-only, ARIA roles (`role="progressbar"` WorkflowCard, `role="alert"` AlertBanner critical), live regions pour updates asynchrones
- **UX-DR40:** Tap targets — minimum 44x44px tactiles, icon-only 32x32 minimum (44x44 mobile), espacement 8px minimum entre tap targets
- **UX-DR41:** Motion & preferences — `prefers-reduced-motion: reduce` respecté, durée standard 150-200ms max, pas de parallax ni auto-play
- **UX-DR42:** Formulaires accessibles — labels visibles liés via `<label for>` + `id`, messages d'erreur liés via `aria-describedby`, `required` + `aria-required`, validation visuelle ET textuelle
- **UX-DR43:** Language — `lang="fr"` sur `<html>`, éléments en autre langue marqués `lang="en"`, pas d'abréviations non explicites
- **UX-DR44:** CI/CD accessibility — eslint-plugin-jsx-a11y bloquant, Lighthouse audit ≥ 90 score, axe DevTools par feature, tests manuels keyboard + VoiceOver par release

**Responsive Design**

- **UX-DR45:** Desktop P0 (1024px+) — utilisation pleine écran, features desktop-only : Command Palette Cmd+K recherche floue, raccourcis clavier complets, multi-sélection listes, drag-and-drop (Growth)
- **UX-DR46:** Tablet P1 (768px-1023px) — sidebar collapsable 56px icons, Dashboard grille 6 colonnes, Chat sidebar historique en drawer, Trace tree + détail en tabs, adaptations tactiles (tap 44x44, swipe wizard)
- **UX-DR47:** Mobile P2 (320px-767px) — sidebar → bottom navigation 4 icônes, Dashboard colonne unique, Chat pleine largeur input fixed bottom, Trace vue simplifiée list, Config lecture seule recommandée. Mobile-excluded : Company Architect, mode expert, drag-and-drop, multi-select
- **UX-DR48:** Breakpoints Tailwind standard : xs (< 640px) / sm (640-767) / md (768-1023) / lg (1024-1279) / xl (1280-1535) / 2xl (1536+)
- **UX-DR49:** Inputs mobile font-size ≥ 16px (pas de zoom iOS Safari), pas de scroll horizontal, tap targets ≥ 44x44px sur mobile

### FR Coverage Map

**Functional Requirements — 100% coverage (53/53)**

| FR | Epic | Description courte |
|---|---|---|
| FR1 | Epic 4 | Création de workflow (agents, ordre, branchements) |
| FR2 | Epic 4 | Exécution workflow multi-agents avec checkpointing |
| FR3 | Epic 4 | Routage déterministe sans LLM |
| FR4 | Epic 4 | Escalade routage déterministe → LLM |
| FR5 | Epic 4 | Mise en Place automatique avant workflow |
| FR6 | Epic 4 | Dry Run Prédictif |
| FR7 | Epic 4 | Interruption / relance / annulation workflow |
| FR8 | Epic 7 | Scheduler (planification récurrente) |
| FR9 | Epic 2 | Création agent depuis template archétype |
| FR10 | Epic 2 | Configuration identité / prompt / contrats / LLM / erreurs |
| FR11 | Epic 2 | Mode wizard vs expert |
| FR12 | Epic 2 | Distinction agent-template vs agent-instance |
| FR13 | Epic 2 | Contrats élastiques (noyau + zone flexible) |
| FR14 | Epic 2 | Review conversationnelle (commentaires localisés, sévérité) |
| FR15 | Epic 2 | Contrôleur sur modèle/paramètres différents |
| FR16 | Epic 3 | Stockage + recherche vectorielle pgvector |
| FR17 | Epic 3 | Namespaces mémoire isolés par département/projet |
| FR18 | Epic 3 | TTL par chunk + archivage auto |
| FR19 | Epic 3 | Scoring avec décroissance temporelle |
| FR20 | Epic 3 | Push Memory proactif + marquage explicite |
| FR21 | Epic 3 | Purge manuelle chunks |
| FR22 | Epic 2 | Tool Hub MCP |
| FR23 | Epic 2 | Assignation outils → agents |
| FR24 | Epic 2 | Exécution outils sandboxée |
| FR25 | Epic 6 | Chat + streaming SSE |
| FR26 | Epic 6 | Human-in-the-loop (valider/rejeter/modifier) |
| FR27 | Epic 7 | Dashboard (métriques, alertes, statuts) |
| FR28 | Epic 7 | Résumé Sprint Reporter dans Dashboard |
| FR29 | Epic 8 | Navigation contextuelle Trace → Config / Chat |
| FR30 | Epic 8 | Traçabilité complète workflow |
| FR31 | Epic 8 | Métriques contextuelles overlay Trace |
| FR32 | Epic 7 | Notifications proactives sur seuils |
| FR33 | Epic 7 | Configuration des seuils d'alerte |
| FR34 | Epic 9 | Audit trail persistant 100% |
| FR35 | Epic 9 | Chiffrement at-rest données sensibles |
| FR36 | Epic 9 | Gestion clés API LLM (ajout/rotation/suppression) |
| FR37 | Epic 9 | Budget caps par département/workflow |
| FR38 | Epic 9 | Rate limits providers LLM (queues, retry, backoff) |
| FR39 | Epic 7 | Score qualité output |
| FR40 | Epic 7 | Taux de retry par agent + tendance |
| FR41 | Epic 7 | Corrélation retry × mémoire |
| FR42 | Epic 7 | Benchmark mensuel recall mémoire |
| FR43 | Epic 5 | Pôle Dev — 9 agents spécialisés |
| FR44 | Epic 5 | Dev Lead décompose + assigne |
| FR45 | Epic 5 | Code Reviewer valide/rejette |
| FR46 | Epic 5 | CI/CD Watcher surveille + alerte |
| FR47 | Epic 5 | Sprint Reporter résumé automatique |
| FR48 | Epic 2 | Agent Playground (test en isolation) |
| FR49 | Epic 8 | Agent Debugger (diagnostic automatique) |
| FR50 | Epic 8 | Event Hooks sur bus d'événements |
| FR51 | Epic 5 | Code Researcher — exploration codebase |
| FR52 | Epic 3 | Namespaces 4 types (client/métier/opé/contextuelle) |
| FR53 | Epic 4 | Résumés de passage auto entre étapes |

**Non-Functional Requirements — 100% coverage (21/21)** — distribués par thème :
- **Performance** : NFR1 (all) · NFR2 (Epic 6) · NFR3 (Epic 4) · NFR4 (Epic 1) · NFR5 (Epic 3)
- **Sécurité** : NFR6-NFR10 → Epic 9
- **Fiabilité** : NFR11-NFR12 (Epic 4) · NFR13 (Epic 1) · NFR14 (Epic 4)
- **Observabilité** : NFR15 (Epic 8) · NFR16 (Epic 1, cross-cutting) · NFR17-NFR18 (Epic 7)
- **Intégration** : NFR19-NFR20 (Epic 2) · NFR21 (Epic 1, cross-cutting)

**Additional Requirements — 100% coverage (44/44)** :
- AR1-AR4 (Starter + Sprint 0) → Epic 1
- AR5-AR10 (Infra/Déploiement) → Epic 1
- AR11-AR15 (Core shared services) → Epic 1
- AR16-AR17 (ORM/HNSW initial) → Epic 1 · AR18-AR20 (Embedding/Memory) → Epic 3
- AR21-AR28 (Sécurité hardening) → Epic 9
- AR29-AR36 (API & Communication) → Epic 1 (fondations), Epic 6 (SSE)
- AR37 (OTel Prometheus) → Epic 7 · AR38 (OTel traces) → Epic 8 · AR39 (Logs) → Epic 1
- AR40-AR44 (Structure/Conventions) → Epic 1

**UX Design Requirements — 100% coverage (49/49)** :
- UX-DR1-8 (Design system foundation) → Epic 1
- UX-DR9, UX-DR17 (AgentCard, ArchetypeSelector) → Epic 2
- UX-DR10-11, UX-DR13 (WorkflowCard, OutputCard, ChatMessage) → Epic 6
- UX-DR12, UX-DR15 (MetricBlock, AlertBanner) → Epic 7
- UX-DR14 (TraceNode) → Epic 8
- UX-DR16, UX-DR18 (ModeToggle, KeyboardShortcut) → Epic 1 (partagés)
- UX-DR19, UX-DR25 (Layout principal, routing) → Epic 1
- UX-DR20-24 (Layouts par espace) → Epics 6, 7, 8 (selon espace) + Epic 1 (base)
- UX-DR26-36 (Patterns UX) → Epic 1 (foundations) + epics applicables
- UX-DR37-44 (Accessibility AA) → cross-cutting + Epic 1 (CI setup)
- UX-DR45-49 (Responsive) → cross-cutting tous les epics frontend

## Epic List

### MVP (Sprint 0-3)

### Epic 1: Foundation & Spike Validation

John peut faire tourner le squelette d'Agentive (`docker compose up`), accéder à une UI minimale authentifiée, et les risques techniques critiques (M3 LangGraph + M4 pgvector) sont validés. Cet epic pose l'ensemble de la fondation (scaffolding, core services partagés, design system, migrations initiales, CI minimale) et fait office d'enabler pour tous les epics suivants.

**FRs covered:** aucun FR direct (enabler technique)
**NFRs covered:** NFR4, NFR13, NFR16, NFR21 (fondations)
**ARs covered:** AR1-AR17, AR29-AR36, AR39-AR44
**UX-DRs covered:** UX-DR1-8, UX-DR16, UX-DR18, UX-DR19, UX-DR25, UX-DR26-36 (patterns foundation), UX-DR37-44 (CI a11y), UX-DR45-49 (responsive base)
**Sprint cible:** Sprint 0

### Epic 2: Agent Platform (Templates, Configuration, Tools, Playground)

John peut créer des agents depuis les 8 archétypes universels (Orchestrateur, Chercheur, Analyste, Producteur, Stratège, Contrôleur, Veilleur, Communicateur), les configurer (identité, system prompt, contrats élastiques, modèle LLM, politique d'erreur), leur assigner des outils MCP sandboxés, et les tester en isolation via le Playground avant toute utilisation en workflow.

**FRs covered:** FR9, FR10, FR11, FR12, FR13, FR14, FR15, FR22, FR23, FR24, FR48
**NFRs covered:** NFR10, NFR19, NFR20
**ARs covered:** AR13 (core/llm abstraction), AR23 (bwrap sandbox), AR44 (anti prompt injection)
**UX-DRs covered:** UX-DR9 (AgentCard), UX-DR17 (ArchetypeSelector)
**Sprint cible:** Sprint 1
**Dépendance:** Epic 1

### Epic 3: Memory & Knowledge System

Le système stocke et retrouve des chunks mémoire par namespace isolé (4 types : client, métier, opérationnelle, contextuelle) avec règles de rétention distinctes. Il applique un TTL par chunk avec archivage automatique, scoring de pertinence à décroissance temporelle, injection proactive (Push Memory) avec marquage explicite. John peut purger manuellement des chunks spécifiques.

**FRs covered:** FR16, FR17, FR18, FR19, FR20, FR21, FR52
**NFRs covered:** NFR5, NFR7
**ARs covered:** AR17 (HNSW tuning), AR18 (Embedding Router hybride), AR19 (chunk_embeddings), AR20 (multi-namespace RLS)
**Sprint cible:** Sprint 1
**Dépendance:** Epic 1

### Epic 4: Workflow Orchestration Engine

John peut exécuter des workflows multi-agents bout en bout avec orchestration hybride (70% déterministe / 30% LLM + escalade par confiance), checkpointing (reprise après interruption), Dry Run Prédictif (estimation coût/temps/chemin avant lancement), Mise en Place automatique (pré-vérification outils/mémoire/budget/risques), fallback LLM multi-provider automatique, et résumés de passage automatiques entre étapes.

**FRs covered:** FR1, FR2, FR3, FR4, FR5, FR6, FR7, FR53
**NFRs covered:** NFR3, NFR11, NFR12, NFR14
**UX-DRs covered:** UX-DR10 (WorkflowCard states)
**Sprint cible:** Sprint 2
**Dépendance:** Epics 1, 2, 3

### Epic 5: Dev Department (Pôle Pilote)

Les 9 agents Dev (Dev Lead, Code Researcher, Architect Analyst, Code Producer, Code Reviewer, Test Engineer, CI/CD Watcher, Doc Writer, Sprint Reporter) fonctionnent ensemble pour des tâches réelles de l'agence. Critère MVP : John utilise quotidiennement le Pôle Dev (dogfooding).

**FRs covered:** FR43, FR44, FR45, FR46, FR47, FR51
**Sprint cible:** Sprint 2
**Dépendance:** Epics 2, 3, 4 (consomme toute la plateforme)

### Epic 6: Chat Interface & Human-in-the-Loop

John interagit avec les agents via Chat (streaming SSE temps réel, slash commands `/deploy` `/review` `/status` `/traces`, autocomplétion `@` agents / `#` projets, multi-auteur, workflow inline, validation inline avec boutons Valider/Commenter/Voir Trace/Relancer). Support du Journey 1 (Chef d'orchestre).

**FRs covered:** FR25, FR26
**NFRs covered:** NFR2
**ARs covered:** AR32 (useSSE hook robuste)
**UX-DRs covered:** UX-DR11 (OutputCard), UX-DR13 (ChatMessage), UX-DR22 (Chat layout), UX-DR33-34 (Command Palette, slash commands, patterns novateurs)
**Sprint cible:** Sprint 3
**Dépendance:** Epics 1, 4, 5

### Epic 7: Dashboard, Alerts & Scheduler

John consulte un cockpit (métriques temps réel, statuts par département/projet, résumé auto Sprint Reporter, alertes configurables par agent/métrique), et planifie l'exécution récurrente de workflows. Le système mesure qualité par output, taux de retry, corrélation retry × mémoire, et exécute un benchmark mensuel de recall.

**FRs covered:** FR8, FR27, FR28, FR32, FR33, FR39, FR40, FR41, FR42
**NFRs covered:** NFR17, NFR18
**ARs covered:** AR37 (OpenTelemetry + Prometheus)
**UX-DRs covered:** UX-DR12 (MetricBlock), UX-DR15 (AlertBanner), UX-DR21 (Dashboard layout)
**Sprint cible:** Sprint 3
**Dépendance:** Epics 1, 3, 4, 5

### Epic 8: Trace Explorer & Agent Debugger

John remonte la chaîne complète d'exécution (sources → données → raisonnement → décision) avec métriques contextuelles en overlay, navigue contextuellement Trace → Config → Chat en un clic (URL-state préservé), configure des Event Hooks sur le bus d'événements, et l'Agent Debugger analyse automatiquement les traces d'agents en échec pour proposer un diagnostic. Support du Journey 2 (Diagnosticien).

**FRs covered:** FR29, FR30, FR31, FR49, FR50
**NFRs covered:** NFR15
**ARs covered:** AR38 (OpenTelemetry traces)
**UX-DRs covered:** UX-DR14 (TraceNode), UX-DR23 (Trace Explorer layout)
**Sprint cible:** Sprint 3
**Dépendance:** Epics 1, 4, 7

### Epic 9: Security, Audit & Trust Controls

Le système garantit la confiance au niveau production : audit trail 100% des actions (agent + humain) avec rétention ≥ 90 jours sur table partitionnée immutable, chiffrement at-rest AES-256 sur clés API LLM / credentials MCP / données clients, gestion sécurisée des clés API (ajout, rotation, suppression), budget caps par département/workflow avec alertes, rate limiting multi-provider avec queues/retry/backoff, secrets management via SOPS+age + détection `gitleaks`.

**FRs covered:** FR34, FR35, FR36, FR37, FR38
**NFRs covered:** NFR6, NFR8, NFR9
**ARs covered:** AR21 (SOPS+age), AR22 (Fernet), AR24 (slowapi), AR25 (Caddy security headers), AR27 (audit partitioned), AR28 (3 rôles PG)
**Sprint cible:** Cross-cutting Sprint 0-3 (stories réparties)
**Dépendance:** Epic 1

### Growth (Sprint 4-5) — Placeholders

### Epic 10: 2nd Department — Design/UX

6 agents Design/UX (Design Lead, Trend Watcher, Moodboard Curator, UX Architect, UI Producer, Design Reviewer) valident l'universalité des 8 archétypes — test critique Growth. Permet de capitaliser les patterns du Pôle Dev et de confirmer que l'architecture (Company Builder) est vraiment multi-domaines.

**Sprint cible:** Sprint 4
**Stories:** à détailler lors du cycle Growth

### Epic 11: Company Architect (M1) — Wizard 7 étapes

John crée de nouveaux départements via un processus guidé en 7 étapes : Discovery → Role Mapping → Agent Specification → Workflow Design → Tool Binding → Memory Architecture → Deploy & Validate. Intègre le Tool Scout (recherche d'outils existants + spécification d'outils manquants). Support du Journey 3 (Architecte de département).

**Sprint cible:** Sprint 5
**Stories:** à détailler lors du cycle Growth

### Epic 12: Multi-User & Permissions

Sophie (lecture seule par département) et Marc (accès limité par projet/agent/action) peuvent utiliser Agentive avec RBAC (`owner`, `collaborator`, `freelance`), isolation namespace stricte, export d'outputs, et audit trail des utilisateurs externes. Support des Journeys 4 (Sophie collaboratrice) et 5 (Marc freelance).

**Sprint cible:** Sprint 4-5
**Stories:** à détailler lors du cycle Growth

### Epic 13: Topology View, Reporting & Growth Innovations

M9 Topology View (visualisation graphe agents/workflows), M10 Reporting Engine (rapports automatisés), Recrutement dynamique (scatter-gather), Tool Scout hybride (LLM + audit déterministe), et les 6 innovations Growth (Registre d'Anticorps, Workflows Adaptatifs, Autonomie Graduée, Agents Proactifs, Auto-Graduation, Audit Trail comme Dataset).

**Sprint cible:** Sprint 5
**Stories:** à détailler lors du cycle Growth

---

## Epic 1: Foundation & Spike Validation

John peut faire tourner le squelette d'Agentive, accéder à une UI minimale authentifiée, et les risques techniques critiques sont validés. Fondation pour tous les epics MVP.

### Story 1.1: Scaffolding minimal viable des 3 stacks

As John (développeur solo),
I want un squelette runnable combinant frontend Vite/React/shadcn, backend FastAPI/uv, et Postgres+pgvector via Docker Compose avec structure de dossiers alignée feature-based,
So that je peux construire les features sur une base validée et reproductible.

**Acceptance Criteria:**

**Given** un poste dev avec Docker + `mise` installés
**When** je clone le repo et exécute `just dev`
**Then** les 3 stacks démarrent en moins de 60s (NFR4)
**And** le frontend est accessible sur `localhost:5173` avec la sidebar des 4 espaces visibles
**And** le backend expose `/health` + `/ready` répondant 200 OK
**And** la commande `docker compose ps` montre db/backend/frontend/caddy tous healthy

**Given** le repo fraîchement cloné
**When** j'inspecte la structure
**Then** je trouve `backend/` avec sous-dossiers `shared/` (event_bus/repositories/llm/auth/contracts/config), `features/` (vide, prêt pour M2-M12), `api/` (routes par espace UI), `infra/`, `tests/`
**And** `frontend/src/routes/` contient `dashboard/`, `chat/`, `trace/`, `config/` (TanStack Router file-based)
**And** `mise.toml` pinne Python 3.13, Node 22 LTS, uv latest
**And** `justfile` racine expose `just dev / test / lint / bench`

**Given** je modifie un fichier Python ou TS
**When** je commit
**Then** `pre-commit` exécute `gitleaks` + `ruff` + `eslint` et bloque si échec
**And** une tentative de commit avec un secret canary (ex: fake API key) échoue en détection gitleaks

**Given** le backend démarre pour la 1ère fois
**When** la migration Alembic initiale s'applique
**Then** l'extension `vector` est créée dans Postgres
**And** un index HNSW de référence existe sur la table `chunk_embeddings` (paramètres initiaux m=16, ef_construction=64 ; ajustés en Story 1.3)
**And** les 3 rôles Postgres sont créés : `agentive_app`, `agentive_audit_admin`, `agentive_owner`

### Story 1.2: Spike M3 LangGraph (gating critique)

As John,
I want valider que LangGraph supporte checkpointing + scatter-gather + human-in-the-loop nativement via un workflow minimal 2 agents + 1 quality gate,
So that je peux m'engager sur l'abstraction M3 avant d'investir dans tout le reste du Sprint 1.

**Acceptance Criteria:**

**Given** le scaffolding Sprint 0 terminé
**When** je lance le workflow spike `python -m spike.m3_langgraph`
**Then** 2 agents factices (Producer + Reviewer) s'exécutent en séquence avec un quality gate intermédiaire
**And** le workflow se termine en < 30s en mode local (mock LLM ou LLM réel selon env)

**Given** un workflow en cours d'exécution
**When** je simule un crash du process (kill -9) pendant l'étape Reviewer
**Then** relancer le process reprend l'exécution à partir du dernier checkpoint sans re-exécuter le Producer (NFR11)
**And** le workflow complet se termine correctement après reprise

**Given** le quality gate intermédiaire est configuré en mode human-in-the-loop
**When** le gate atteint le point de validation
**Then** l'exécution se suspend
**And** un callback/endpoint d'approbation permet de reprendre (`resume(approval=True)`)
**And** un `resume(approval=False, feedback="...")` renvoie au Producer

**Given** le spike s'exécute
**When** j'inspecte les checkpoints
**Then** ils sont persistés dans Postgres (pas en mémoire volatile)
**And** le format checkpoint est lisible et migrable (JSON structuré)

**Given** le spike échoue sur un des 3 critères (checkpoint / scatter-gather / HITL)
**When** le rapport Sprint 0 est écrit
**Then** un ADR documente le pivot (abstraction custom, alternative framework, ou ajustement approche)
**And** le gating bloque le démarrage du Sprint 1 tant qu'un pivot n'est pas validé

### Story 1.3: Benchmark M4 pgvector HNSW (gating NFR5)

As John,
I want valider que la recherche vectorielle HNSW sur pgvector respecte NFR5 (< 200ms p95 sur 10k chunks avec reranking) avec tuning itératif des paramètres,
So that je peux confirmer la viabilité de pgvector avant le Sprint 1 et documenter les paramètres HNSW optimaux.

**Acceptance Criteria:**

**Given** la base avec extension pgvector et table `chunk_embeddings` initialisée
**When** j'exécute le script `just bench-m4`
**Then** 10 000 chunks synthétiques sont générés avec embeddings dummy (384 dims local + 1536 dims cloud)
**And** le bench mesure recall@5, recall@10, latence p50/p95, build time, taille index pour 3 triplets de paramètres : m ∈ [8, 16, 32], ef_construction ∈ [64, 128, 256], ef_search ∈ [40, 100, 200]

**Given** les résultats du bench
**When** je compare HNSW vs ivfflat sur les mêmes datasets
**Then** un tableau comparatif est produit dans `docs/decisions/hnsw-tuning.md`
**And** les paramètres finaux retenus sont documentés avec justification (tradeoff recall vs latence vs build time)

**Given** les paramètres HNSW retenus
**When** on vérifie la latence p95 sur 10k chunks avec reranking
**Then** la latence p95 est < 200ms (NFR5)
**And** le recall@5 est > 90% sur un set de validation de 50 requêtes types
**And** si NFR5 échoue, un ADR documente l'ajustement (tuning Postgres shared_buffers/work_mem ou changement d'indexation) avant Sprint 1

### Story 1.4: Core event bus (PostgreSQL LISTEN/NOTIFY + Outbox Pattern)

As the system,
I want un bus d'événements interne implémentant LISTEN/NOTIFY avec Outbox Pattern pour la durabilité,
So that tous les modules communiquent asynchroniquement sans perdre d'événements en cas de crash du listener.

**Acceptance Criteria:**

**Given** la migration Alembic initiale
**When** la table `outbox_events` est créée
**Then** elle contient `id (uuid)`, `correlation_id`, `event_type`, `payload (jsonb)`, `created_at`, `processed_at (nullable)`
**And** un index partiel `WHERE processed_at IS NULL` existe pour le replay

**Given** un module publie un événement via `shared.event_bus.publish()`
**When** la transaction DB est commitée
**Then** l'événement est INSÉRÉ dans `outbox_events` dans la même transaction
**And** un `NOTIFY` Postgres déclenche le worker consumer

**Given** un worker consumer redémarre après crash
**When** il démarre
**Then** il lit tous les `outbox_events WHERE processed_at IS NULL` et les replay
**And** chaque événement marque `processed_at = NOW()` après traitement réussi

**Given** un `correlation_id` est fourni en entrée d'une chaîne d'événements
**When** les événements sont publiés/consommés
**Then** le `correlation_id` est propagé à travers tous les events + logs structlog + spans OTel

**Given** deux modules A et B veulent communiquer
**When** A publie un event type que B a enregistré
**Then** B reçoit l'event sans import direct de A (enforcement `import-linter` en CI)

### Story 1.5: Core repositories + migrations tenant-ready + RLS

As the system,
I want des Repository Pattern async dans `shared/repositories/` pour toutes les entités critiques (users, workflows, agents, memory_chunks, audit_events) avec `tenant_id` nullable et RLS Postgres active,
So that les modules n'accèdent jamais directement à la DB et la multi-tenancy Growth est préparée à coût quasi-nul.

**Acceptance Criteria:**

**Given** les migrations Alembic s'exécutent
**When** la base est initialisée
**Then** les tables `users`, `workflows`, `workflow_runs`, `agent_templates`, `agent_instances`, `memory_chunks`, `chunk_embeddings`, `audit_events` existent
**And** toutes ont une colonne `tenant_id UUID NULL`
**And** les tables `memory_chunks`, `workflows`, `audit_events`, `agent_instances`, `chunk_embeddings` ont RLS activée avec policy `tenant_isolation` (`tenant_id IS NULL OR tenant_id = current_setting('app.tenant_id', true)::uuid`)

**Given** un module veut lire/écrire une entité
**When** il utilise `shared.repositories.AgentRepo / MemoryChunkRepo / WorkflowRepo / AuditEventRepo`
**Then** l'accès DB passe exclusivement par ces classes async
**And** `import-linter` bloque en CI tout import direct `AsyncSession` ou `asyncpg` depuis un module feature

**Given** un repo ouvre une transaction dans un contexte multi-tenant
**When** le context manager `with_tenant(tenant_id)` est utilisé
**Then** `SET LOCAL app.tenant_id = :tenant_id` est exécuté au début de la transaction
**And** les queries respectent automatiquement le RLS

**Given** la table `users`
**When** la migration s'applique
**Then** 1 ligne owner (John) est insérée avec `tenant_id = NULL`

### Story 1.6: Core LLM abstraction (multi-provider)

As the system,
I want une couche `shared/llm/` exposant une interface `LLMProvider` unifiée wrappant `langchain-anthropic` + `langchain-openai` avec fallback configurable et escape hatch `raw_provider_call`,
So that les agents sont découplés des SDK providers et je peux changer/ajouter des providers sans refactoring.

**Acceptance Criteria:**

**Given** l'interface `LLMProvider` est définie
**When** je regarde le protocol
**Then** elle expose `async complete(messages, **kwargs) -> Completion` et `async raw_provider_call(**provider_specific_kwargs) -> Any`
**And** 2 implémentations concrètes existent : `AnthropicProvider`, `OpenAIProvider`

**Given** une configuration d'agent spécifie `provider_chain = ["anthropic", "openai"]`
**When** le provider primaire Anthropic retourne une erreur 5xx ou timeout
**Then** le système bascule automatiquement sur OpenAI (NFR12 graceful degradation)
**And** l'événement `llm_fallback_triggered` est publié sur le bus

**Given** un agent a besoin du prompt caching Anthropic
**When** il appelle `raw_provider_call(cache_control={"type": "ephemeral"}, ...)`
**Then** la feature provider-specific est exposée sans fuite dans l'interface standard

**Given** une clé API invalide est fournie
**When** un agent fait un appel LLM
**Then** l'erreur est capturée et transformée en RFC 7807 Problem Details avec `correlation_id`
**And** aucune clé API n'apparaît dans les logs/traces (NFR9)

### Story 1.7: Core auth (token statique MVP + rotation)

As John,
I want un middleware FastAPI validant `Authorization: Bearer` contre la variable d'env `AGENTIVE_API_TOKEN` + un endpoint de rotation sécurisée avec audit,
So that la plateforme est protégée quand exposée via Tailscale/VPN sans complexité RBAC au MVP.

**Acceptance Criteria:**

**Given** une requête sur `/api/v1/*` sans header `Authorization`
**When** le middleware auth s'exécute
**Then** la réponse est 401 Unauthorized au format RFC 7807
**And** aucun détail du token n'est révélé dans la réponse

**Given** une requête avec `Authorization: Bearer <token_correct>`
**When** le middleware valide le token (comparaison bcrypt hash vs env var)
**Then** la requête continue vers le handler
**And** un audit event `token_used` est publié avec `correlation_id`

**Given** John appelle `POST /api/v1/admin/rotate-token` avec le token courant
**When** un nouveau token est généré (random 32 bytes base64)
**Then** son hash bcrypt remplace le précédent dans le secret store
**And** la réponse retourne le nouveau token en clair (une seule fois)
**And** un audit event `token_rotated` est publié

**Given** la fréquence d'utilisation d'un token dépasse un seuil configurable
**When** le compteur audit dépasse le seuil
**Then** une alerte Dashboard M6 est déclenchée (accès anormal détecté) — Epic 7

### Story 1.8: Design system foundation (shadcn + tokens + routing 4 espaces)

As John,
I want les 17 composants shadcn/ui v4 copiés dans `components/ui/`, les tokens CSS Dark/Light (violet-500 accent), theme switching via `next-themes`, et TanStack Router file-based pour les 4 espaces,
So that la fondation UI est prête pour le développement des composants custom dans les epics suivants.

**Acceptance Criteria:**

**Given** le scaffolding est en place
**When** j'exécute `npx shadcn@latest add button input textarea card dialog sheet command tabs select tooltip badge avatar separator skeleton toast accordion scroll-area`
**Then** les 17 composants shadcn sont copiés dans `frontend/src/components/ui/`
**And** `tailwind.config.js` déclare les tokens `--background`, `--card`, `--muted`, `--primary` (`#8B5CF6`), `--destructive`, `--ring`
**And** les palettes Dark + Light sont configurées conformément à UX-DR2

**Given** l'utilisateur arrive sur l'application
**When** le theme est résolu
**Then** dark mode s'applique par défaut (UX-DR6)
**And** `prefers-color-scheme` est respecté si configuré
**And** le `ModeToggle` permet de switcher dark ↔ light sans FOUT

**Given** les 4 routes TanStack (`dashboard/`, `chat/`, `trace/`, `config/`) sont définies
**When** j'utilise Cmd+1/2/3/4
**Then** la navigation bascule entre les 4 espaces en < 100ms
**And** les search params sont préservés lors des transitions contextuelles (préparation UX-DR25 Navigation Contextuelle)

**Given** la sidebar principale
**When** je la consulte
**Then** elle est fixe à 240px, collapsable à 56px (icônes only), avec les 4 icônes d'espaces + tooltip (UX-DR19)
**And** la typo Geist Sans + Geist Mono est chargée (UX-DR4)

**Given** un utilisateur clavier-only
**When** il navigue
**Then** le focus visible est un ring violet-500 2px + offset 2px (UX-DR37)
**And** Tab order suit le flow visuel

### Story 1.9: Observability foundations + Caddy + CI

As John,
I want des logs JSON structurés avec correlation ID propagé + responses d'erreur RFC 7807 + Caddy reverse proxy avec security headers + GitHub Actions CI (lint+test+build),
So that observabilité et sécurité sont baseline by design dès Sprint 0.

**Acceptance Criteria:**

**Given** une requête HTTP entre dans le backend
**When** elle atteint le middleware correlation
**Then** un header `X-Correlation-ID` (UUID v7 ou ULID) est généré ou propagé
**And** tous les logs structlog issus de cette requête contiennent le `correlation_id`
**And** les spans OTel sont enrichis du `correlation_id`

**Given** une exception applicative est levée
**When** elle remonte au handler FastAPI
**Then** la réponse est au format `application/problem+json` RFC 7807
**And** elle contient `correlation_id`, `agent_id` (si applicable), `module`, `tenant_id` (si applicable)
**And** aucun stack trace n'apparaît dans la réponse prod (uniquement logs)

**Given** Caddy démarre dans Docker Compose
**When** une requête HTTP arrive
**Then** Caddy redirige en HTTPS (auto Let's Encrypt si domaine public, Tailscale MagicDNS sinon, self-signed en dev)
**And** les headers HSTS / X-Content-Type-Options / X-Frame-Options / Referrer-Policy / CSP avec nonces sont présents
**And** CORS whitelist explicite (pas de `*`)

**Given** je push sur main ou ouvre une PR
**When** GitHub Actions s'exécute
**Then** les jobs `lint` (ruff + eslint + jsx-a11y) + `test` (pytest + vitest) + `build` (backend image + frontend bundle) passent tous
**And** le pipeline bloque les merges si un job échoue

---

## Epic 2: Agent Platform (Templates, Configuration, Tools, Playground)

John peut créer, configurer, outiller et tester des agents en isolation.

### Story 2.1: Créer un agent-template depuis un archétype

As John,
I want créer un nouveau agent-template à partir d'un des 8 archétypes universels,
So that je démarre toujours d'une structure éprouvée alignée sur le Company Builder.

**Acceptance Criteria:**

**Given** les 8 archétypes sont seedés en base au démarrage
**When** j'appelle `POST /api/v1/agents/templates` avec `{archetype: "Producteur", name: "Code Producer"}`
**Then** un agent-template est créé avec les champs par défaut de l'archétype (prompt base, contrats squelette, role)
**And** la réponse retourne `template_id` + version initiale `v1`

**Given** l'UI de création d'agent
**When** j'ouvre Config > Agents > Nouveau
**Then** l'`ArchetypeSelector` UX-DR17 affiche une grille 4x2 des 8 archétypes avec icône + description
**And** la sélection affiche un aperçu du prompt base et des contrats

**Given** je tente de créer un template avec un archetype inconnu
**When** la requête est envoyée
**Then** la réponse est 422 RFC 7807 avec détail des archétypes valides

### Story 2.2: Configurer l'identité, le prompt, les contrats élastiques, le LLM et la politique d'erreur

As John,
I want configurer en détail un agent-template (identité, system prompt, contrats input/output élastiques, modèle LLM, politique d'erreur, provider chain),
So that je l'adapte finement à son rôle spécifique.

**Acceptance Criteria:**

**Given** un agent-template existant
**When** j'appelle `PUT /api/v1/agents/templates/:id`
**Then** les champs modifiables incluent `identity`, `system_prompt`, `input_contract`, `output_contract`, `llm_model`, `llm_params`, `provider_chain`, `error_policy`
**And** Pydantic v2 strict valide les entrées
**And** une nouvelle version `v2` est créée (versioning prompts)

**Given** un contrat élastique
**When** je le définis
**Then** il contient un `core` (champs obligatoires typés) et un `extras: Dict[str, Any]` (zone flexible)
**And** la validation au runtime vérifie uniquement le `core`

**Given** un prompt contient un input externe
**When** l'agent l'injecte au LLM
**Then** le contenu externe est wrappé dans `<user_input>...</user_input>` ou `<tool_output>...</tool_output>` (AR44 anti prompt injection)

**Given** la politique d'erreur définie `{on_timeout: "retry_with_backoff", max_retries: 3}`
**When** un timeout survient
**Then** le retry avec backoff exponentiel s'applique automatiquement (NFR14)

### Story 2.3: Mode wizard guidé vs mode expert direct

As John,
I want basculer entre un mode Wizard guidé (étapes structurées) et un mode Expert direct (tout visible) pour la configuration d'un agent,
So that je choisis le niveau de détail selon ma charge cognitive du moment.

**Acceptance Criteria:**

**Given** l'UI de configuration d'agent
**When** j'ouvre la page Config > Agent > :id
**Then** un `ModeToggle` (UX-DR16) en haut permet de switcher Wizard ↔ Expert
**And** le choix est persisté par utilisateur (préférence)

**Given** le mode Wizard actif
**When** je configure l'agent
**Then** les champs sont présentés en 5-7 étapes (identité → prompt → contrats → LLM → erreurs → validation) avec progression visible
**And** chaque étape valide avant de passer à la suivante

**Given** le mode Expert actif
**When** je configure l'agent
**Then** tous les champs sont visibles sous forme d'accordéons empilés (UX-DR24)
**And** la validation Zod miroite la validation Pydantic côté serveur

### Story 2.4: Distinction agent-template vs agent-instance

As the system,
I want distinguer l'agent-template (définition persistée, versionnée) de l'agent-instance (exécution en cours avec snapshot gelé des paramètres),
So that je peux modifier un template sans casser les runs en cours et garder l'historique d'exécution auditable.

**Acceptance Criteria:**

**Given** 2 tables distinctes `agent_templates` + `agent_instances`
**When** une instance est créée au démarrage d'un workflow
**Then** elle référence `template_id` + `template_version`
**And** elle copie un snapshot des paramètres au moment du run (`system_prompt`, `llm_model`, `provider_chain`)

**Given** John modifie le template `v2 → v3` pendant qu'un workflow utilise une instance basée sur `v2`
**When** le workflow continue son exécution
**Then** l'instance reste sur `v2` (pas de hot-swap)
**And** le prochain workflow démarré utilisera `v3`

**Given** l'historique des runs
**When** je consulte `GET /api/v1/workflows/runs/:id/instances`
**Then** je vois quelle version de template a été utilisée pour chaque instance

### Story 2.5: Tool Hub MCP + assignation outils à un agent

As John,
I want brancher des serveurs MCP (stdio + SSE) via le Tool Hub (M5) et assigner des outils spécifiques à des agents,
So that chaque agent n'a que les capacités dont il a besoin (principe du moindre privilège).

**Acceptance Criteria:**

**Given** le module M5 Tool Hub est déployé
**When** je connecte un serveur MCP via `POST /api/v1/tools/servers` (stdio ou SSE)
**Then** le système découvre les outils exposés et les enregistre dans `tools` table
**And** la compatibilité MCP stdio + SSE est vérifiée (NFR19)

**Given** un agent-template existant
**When** j'appelle `POST /api/v1/agents/templates/:id/tools` avec une liste d'IDs d'outils
**Then** une junction table `agent_template_tools` est mise à jour
**And** au runtime l'agent-instance ne peut appeler que ces outils

**Given** l'UI Config > Agent > Outils
**When** je consulte la liste
**Then** les outils disponibles sont groupés par serveur MCP source
**And** une checkbox permet assignation/désassignation avec audit event

### Story 2.6: Exécution des outils MCP dans sandbox bwrap + setrlimit

As the system,
I want exécuter les outils MCP dans un sandbox `bubblewrap` avec whitelist réseau + mount filesystem read-only + timeout configurable + fallback `setrlimit`,
So that les outils ne peuvent pas compromettre l'hôte ou exfiltrer des données (NFR10).

**Acceptance Criteria:**

**Given** l'image backend contient `bubblewrap` (`bwrap`)
**When** un tool call est déclenché
**Then** le subprocess démarre dans un namespace réseau dédié avec whitelist explicite
**And** le filesystem est monté read-only sauf `/tmp` éphémère
**And** le timeout configurable par tool est appliqué (kill si dépassé)

**Given** `bwrap` est indisponible sur l'hôte
**When** le système boot
**Then** un warning est loggé et `setrlimit` (CPU, mémoire, fichiers) est appliqué en fallback
**And** la santé de ce fallback est remontée dans le Dashboard

**Given** un test de bypass (fork bomb, network outbound non-whitelisté)
**When** la CI exécute les tests sandbox
**Then** le test échoue si le sandbox est contournable
**And** le build est bloqué

### Story 2.7: Agent Playground (test en isolation)

As John,
I want un Playground permettant de tester un agent avec entrée manuelle et inspection complète de l'output sans déclencher events/memory writes,
So that j'itère rapidement sur les prompts sans side-effects (FR48).

**Acceptance Criteria:**

**Given** un agent-template existant
**When** j'ouvre `/config/playground/:agent_id`
**Then** un formulaire me permet de saisir l'input manuellement (aligné sur le `input_contract` de l'agent)
**And** je peux choisir les outils à activer (sous-ensemble des outils assignés)

**Given** je lance l'exécution Playground
**When** l'agent s'exécute
**Then** aucun event n'est publié sur le bus principal (`shared.event_bus`)
**And** aucun write ne touche `memory_chunks` (isolation stricte)
**And** le run est marqué `playground_only` dans les traces OTel

**Given** l'exécution se termine
**When** je consulte l'output
**Then** je vois : prompt final envoyé au LLM (avec variables résolues), output brut LLM, output parsé selon le contrat, tokens consommés + coût estimé
**And** je peux relancer avec ajustements rapides sans recharger la page

### Story 2.8: Review conversationnelle par le Contrôleur

As John,
I want que les agents Contrôleurs retournent des reviews conversationnelles avec commentaires localisés et niveaux de sévérité (bloquant / suggestion / question), sur un modèle LLM différent du Producteur,
So that le Producteur peut itérer de façon ciblée et la diversité des contrôles est garantie (FR14, FR15).

**Acceptance Criteria:**

**Given** un output de Producteur entre dans un agent Contrôleur
**When** le Contrôleur produit sa review
**Then** le schema `ReviewComment` contient `location`, `severity` ∈ {blocking, suggestion, question}, `message`, `suggested_fix (optional)`
**And** une liste de commentaires + un verdict global (pass/fail/needs_fix) est retourné

**Given** la configuration Controller → Producer
**When** le workflow s'exécute
**Then** le Contrôleur utilise un modèle LLM OU des paramètres (température) différents du Producteur (FR15)
**And** une alerte est levée si c'est la même combinaison modèle+params

**Given** la review est rendue dans le Chat
**When** je la consulte
**Then** l'`OutputCard` affiche les commentaires groupés par sévérité
**And** un bouton "Relancer avec corrections" réinjecte la review comme feedback au Producteur

---

## Epic 3: Memory & Knowledge System

Le système mémoire isolé par namespace avec TTL, scoring temporel, Push Memory et Embedding Router hybride.

### Story 3.1: Stockage + recherche vectorielle pgvector (HNSW)

As the system,
I want stocker des chunks mémoire dans Postgres avec embeddings dans la table séparée `chunk_embeddings` et faire la recherche via index HNSW,
So that les agents accèdent au contexte passé pertinent avec latence acceptable (NFR5).

**Acceptance Criteria:**

**Given** les tables `memory_chunks` + `chunk_embeddings` (clé composite chunk_id + model) sont migrées
**When** je fais `POST /api/v1/memory/chunks` avec `{content, namespace, ttl}`
**Then** le chunk est inséré, un embedding est calculé, l'embedding est stocké avec le modèle utilisé
**And** le `correlation_id` est propagé

**Given** 10k chunks en base
**When** j'appelle `GET /api/v1/memory/search?q=...&top_k=5&namespace=...`
**Then** la recherche HNSW retourne 5 chunks avec scores de similarité
**And** la latence p95 est < 200ms (NFR5)

**Given** une recherche sans `namespace` spécifié
**When** je suis authentifié comme John
**Then** la recherche ne traverse PAS les namespaces non-autorisés (RLS actif)

### Story 3.2: Namespaces isolés par département/projet + 4 types

As John,
I want définir des namespaces mémoire isolés par département et par projet avec 4 types (client, métier, opérationnelle, contextuelle) et règles de rétention distinctes par type (FR17, FR52),
So that la mémoire est organisée sémantiquement et le cross-read est impossible sans autorisation.

**Acceptance Criteria:**

**Given** la table `namespaces` existe
**When** je fais `POST /api/v1/memory/namespaces` avec `{name, type, department, project, retention_policy}`
**Then** le namespace est créé avec le type enum (`client`, `métier`, `opérationnelle`, `contextuelle`)
**And** un default `retention_policy` est appliqué selon le type (ex: `contextuelle → ttl=7j`, `métier → ttl=365j`, `client → ttl=illimité sauf demande`, `opérationnelle → ttl=90j`)

**Given** un utilisateur appartenant au département Dev
**When** il cherche dans un namespace du département Design/UX (Growth)
**Then** RLS refuse l'accès
**And** un audit event `namespace_access_denied` est publié

**Given** l'UI Config > Namespaces
**When** je consulte la liste
**Then** elle est groupée par département puis par type
**And** chaque namespace affiche son `retention_policy` + compteur de chunks

### Story 3.3: TTL par chunk + archivage automatique

As the system,
I want appliquer un TTL par chunk et archiver automatiquement les chunks expirés (pas hard-delete) via un job cron,
So that la mémoire se nettoie seule tout en préservant la traçabilité audit (FR18).

**Acceptance Criteria:**

**Given** un chunk avec `ttl = 30j`
**When** le chunk est inséré
**Then** `expires_at = created_at + ttl` est calculé automatiquement

**Given** le worker archival s'exécute quotidiennement
**When** il trouve des chunks avec `expires_at < NOW()` non-archivés
**Then** ils sont déplacés vers `memory_chunks_archived` (pas supprimés)
**And** un audit event `memory_archived` est publié avec `chunk_id` et `namespace`
**And** une métrique `chunks_archived_per_day` alimente le Dashboard

**Given** un chunk est archivé
**When** une recherche est faite
**Then** il n'apparaît plus dans les résultats par défaut
**And** une option `include_archived=true` permet de le récupérer pour audit

### Story 3.4: Scoring avec décroissance temporelle

As the system,
I want scorer la pertinence des chunks en combinant similarité vectorielle et décroissance temporelle configurable par namespace,
So that les chunks anciens perdent naturellement priorité sauf s'ils restent vraiment pertinents (FR19).

**Acceptance Criteria:**

**Given** une recherche avec reranking activé
**When** le système calcule les scores
**Then** `final_score = vector_similarity × decay_function(age)`
**And** `decay_function` est configurable par namespace (ex: exp(-λ·age), linear, step)

**Given** deux chunks avec similarité proche mais âges différents
**When** on compare les scores finaux
**Then** le chunk plus récent a un score supérieur (sauf si sa similarité était significativement plus basse)

**Given** des tests unit avec chunks de différents âges
**When** la suite s'exécute en CI
**Then** la formule produit des scores déterministes et documentés

### Story 3.5: Push Memory proactif avec marquage explicite

As the system,
I want injecter proactivement les chunks pertinents dans le contexte de l'agent (seuil similarité > 0.85, budget 15% tokens, marquage `[CONTEXTE MÉMORISÉ]`, opt-out Contrôleurs),
So that les agents bénéficient du contexte passé sans pollution de chunks peu pertinents (FR20).

**Acceptance Criteria:**

**Given** un agent démarre un task
**When** le service `push_memory` s'exécute pré-workflow
**Then** seuls les chunks avec `similarity > 0.85` sont remontés
**And** le budget est limité à 15% des tokens max du modèle
**And** les chunks sont injectés dans le prompt avec marquage explicite `<contexte_memorise source="namespace/chunk_id">...</contexte_memorise>`

**Given** un agent de type Contrôleur
**When** il démarre
**Then** Push Memory est désactivé par défaut (pour éviter le biais de validation via contexte)
**And** un flag `push_memory_optin=true` sur l'agent peut forcer son activation

**Given** le Dashboard
**When** je consulte les métriques workflow
**Then** `push_memory_chunks_injected` et `push_memory_tokens_used` sont exposés par workflow

### Story 3.6: Purge manuelle chunks + Embedding Router hybride

As John,
I want purger manuellement des chunks spécifiques (données obsolètes ou corrompues) et le système utilise un Embedding Router hybride (FastEmbed local / OpenAI cloud / Voyage optionnel) configurable par namespace,
So that je contrôle la qualité mémoire et les tradeoffs coût/qualité (FR21, AR18).

**Acceptance Criteria:**

**Given** un chunk spécifique
**When** j'appelle `DELETE /api/v1/memory/chunks/:id`
**Then** le chunk est soft-deleted (déplacé vers `memory_chunks_archived` avec raison `manual_purge`)
**And** un audit event `memory_purged_manual` est publié

**Given** un namespace configuré `embedding_backend = "local"`
**When** un chunk est inséré
**Then** FastEmbed avec `BAAI/bge-small-en-v1.5` (384 dims) produit l'embedding
**And** l'embedding est stocké avec `model = "bge-small-en-v1.5"` dans `chunk_embeddings`

**Given** un namespace configuré `embedding_backend = "cloud"`
**When** un chunk est inséré
**Then** OpenAI `text-embedding-3-small` (1536 dims) produit l'embedding
**And** l'option `voyage-3-lite` (1024 dims) est disponible via config

**Given** le Dashboard
**When** je consulte les coûts embedding
**Then** une métrique `embedding_cost_per_namespace` est visible

**Given** l'UI Config > Namespaces > :id
**When** je vois la vue détail
**Then** un bouton "Purger chunks obsolètes" ouvre une confirmation Dialog destructive
**And** un filtre par date/tag permet sélection groupée

---

## Epic 4: Workflow Orchestration Engine

Le moteur central d'orchestration multi-agents avec checkpointing, Dry Run, fallback LLM et résumés de passage.

### Story 4.1: Créer un workflow (agents, ordre, branchements)

As John,
I want définir un workflow comme un DAG avec agents, ordre d'exécution et conditions de branchement,
So that les collaborations multi-agents sont des citoyens de première classe (FR1).

**Acceptance Criteria:**

**Given** je fais `POST /api/v1/workflows` avec un DAG `{nodes, edges, conditions}`
**When** la validation s'exécute
**Then** les cycles sont refusés (422 avec détail)
**And** tous les `agent_template_id` référencés existent
**And** les conditions de branchement sont en DSL léger (ex: `output.status == 'pass'`)

**Given** un workflow valide
**When** il est créé
**Then** il est versionné (`v1`) et persistable
**And** la réponse contient `workflow_id` + `version`

**Given** le DSL de branchement
**When** une condition utilise une variable non-exposée par l'agent émetteur
**Then** la validation refuse et explique quelle variable manque

### Story 4.2: Exécuter un workflow multi-agents avec checkpointing

As the system,
I want exécuter un workflow bout en bout via le moteur M3 (abstraction LangGraph) avec checkpointing persisté à chaque node,
So que la reprise post-crash est possible sans re-exécuter les étapes terminées (FR2, NFR11).

**Acceptance Criteria:**

**Given** `POST /api/v1/workflows/:id/runs` est appelé
**When** un `workflow_run` est créé
**Then** l'état initial est `running`
**And** un stream SSE côté frontend suit les transitions de states

**Given** un node termine avec succès
**When** le checkpoint est persisté dans Postgres (table `workflow_checkpoints`)
**Then** le state complet du graphe (variables, historique nodes) est sauvegardé
**And** le `correlation_id` est dans chaque checkpoint

**Given** le process crashe puis redémarre
**When** le worker détecte un run en `running` sans checkpoint récent
**Then** il reprend depuis le dernier checkpoint sans re-exécuter les nodes complétés
**And** l'event `workflow_resumed` est publié

**Given** le run se termine
**When** l'état final est `completed`
**Then** les métriques (durée, tokens, coût par node) sont agrégées dans `workflow_runs`

### Story 4.3: Orchestration hybride (déterministe → escalade LLM)

As the system,
I want router les tâches simples via règles déterministes et escalader vers un LLM quand la confiance déterministe est < seuil,
So que nous économisons des coûts LLM sur les cas évidents et conservons la flexibilité (FR3, FR4).

**Acceptance Criteria:**

**Given** un moteur de règles déclaratives (YAML/Python)
**When** un routage est demandé
**Then** chaque règle expose un `confidence_score` calculé sur le contexte
**And** si la règle avec plus haut score est > seuil, routage déterministe (aucun LLM)

**Given** aucune règle ne dépasse le seuil
**When** le système escalade vers LLM
**Then** un LLM lightweight (configurable, défaut Haiku) prend la décision
**And** l'escalade est tracée avec raison + décision finale

**Given** le Dashboard
**When** je consulte les métriques workflow
**Then** `% routages déterministes vs LLM` est visible par workflow
**And** cela prépare l'innovation MVP #1 (apprentissage automatique des règles)

### Story 4.4: Dry Run Prédictif

As John,
I want exécuter un Dry Run d'un workflow avant de le lancer (chemin probable, agents mobilisés, estimation tokens/coût, risques identifiés),
So that je budgete et valide avant de dépenser des tokens LLM (FR6).

**Acceptance Criteria:**

**Given** `POST /api/v1/workflows/:id/dry-run` avec input
**When** le Dry Run s'exécute
**Then** aucun appel LLM réel n'est fait (estimation via heuristiques + historique)
**And** l'output contient `probable_path`, `agents_involved`, `token_estimate_per_provider`, `cost_estimate_usd`, `identified_risks` (recrutement dynamique nécessaire, dépassement budget, etc.)

**Given** le Chat au moment d'envoyer une demande
**When** John tape Entrée
**Then** un Dialog "Dry Run preview" s'affiche avec les estimations
**And** les boutons `[Lancer]` / `[Annuler]` / `[Ajuster]` sont présents

**Given** le Dry Run prédit un dépassement de budget cap
**When** l'output est affiché
**Then** un warning visible alerte John
**And** le bouton `[Lancer]` demande confirmation explicite

### Story 4.5: Mise en Place automatique pré-workflow

As the system,
I want pré-vérifier avant chaque workflow (outils disponibles, mémoire chargée, budget disponible, risques identifiés),
So que les workflows ne cassent pas en cours d'exécution pour des prérequis évidents manquants (FR5).

**Acceptance Criteria:**

**Given** un workflow est lancé
**When** le hook pre-workflow s'exécute
**Then** les checks suivants sont effectués : MCP tools ping (sont-ils joignables), namespaces mémoire existent et accessibles, budget disponible >= estimation Dry Run, providers LLM en health
**And** un rapport `mise_en_place` est persisté dans `workflow_runs`

**Given** un prérequis manque (ex: tool MCP down)
**When** la Mise en Place échoue
**Then** le workflow ne démarre pas
**And** une alerte est remontée avec détail + action suggérée

**Given** John veut forcer malgré un warning
**When** il utilise `--force` ou coche "Ignorer warnings"
**Then** un audit event `mise_en_place_bypassed` est publié avec raison

### Story 4.6: Interrompre/relancer/annuler + fallback multi-provider LLM

As John,
I want interrompre, relancer ou annuler un workflow en cours, et le système fallback automatiquement sur un provider LLM alternatif si le primaire est down,
So que je garde le contrôle et le système graceful-degrade (FR7, NFR12).

**Acceptance Criteria:**

**Given** un run en cours
**When** `POST /api/v1/workflows/runs/:run_id/pause|resume|cancel` est appelé
**Then** le state transitionne correctement et un audit event est publié
**And** pause suspend proprement (checkpoint immédiat), resume reprend au dernier checkpoint, cancel arrête et marque `cancelled`

**Given** une config provider chain `[anthropic, openai]`
**When** anthropic retourne 5xx ou timeout
**Then** le système bascule sur openai automatiquement
**And** l'event `llm_fallback_triggered` est publié avec raison

**Given** les deux providers sont down
**When** tous fallbacks épuisent
**Then** le workflow passe en state `error` avec détail
**And** une alerte Dashboard est levée

### Story 4.7: Résumés de passage automatiques entre étapes

As the system,
I want générer un résumé structuré et condensé de l'output de chaque agent pour l'agent suivant dans la chaîne,
So que la consommation de tokens est réduite sans perte d'information critique (FR53).

**Acceptance Criteria:**

**Given** un agent termine son output
**When** le service `summarize_handoff` s'exécute
**Then** un résumé structuré `{decisions, artifacts_refs, blockers, next_questions}` est produit
**And** persistance dans `workflow_state.handoffs[]`

**Given** l'agent suivant démarre
**When** il reçoit son contexte
**Then** il reçoit les résumés des étapes précédentes, pas les outputs bruts complets
**And** une option `include_raw_previous_output=true` permet de désactiver si nécessaire

**Given** une métrique `token_reduction_ratio_per_workflow`
**When** elle est calculée
**Then** la cible est ≥ 30% de réduction vs baseline sans résumés
**And** la métrique alimente le Dashboard

---

## Epic 5: Dev Department (Pôle Pilote)

Les 9 agents Dev fonctionnent ensemble — critère de validation MVP (dogfooding quotidien).

### Story 5.1: Dev Lead — Orchestrateur + décomposition de tâches

As John,
I want un Dev Lead (archétype Orchestrateur) qui comprend ma demande en langage naturel, la décompose en sous-tâches et les assigne aux agents Dev appropriés,
So that je délègue au niveau "manager" et non "contributeur individuel" (FR43, FR44).

**Acceptance Criteria:**

**Given** un agent-template `Dev Lead` créé depuis l'archétype Orchestrateur
**When** je consulte sa config
**Then** son system prompt est spécialisé orchestration Dev (décomposition, routage, communication managériale)
**And** ses outils assignés incluent accès workflow engine + memory namespaces Dev

**Given** John tape dans le Chat "Scaffold le module paiements pour Acme"
**When** Dev Lead s'exécute
**Then** son premier message streamé dans < 2s commence par "Compris. Je mobilise [agents]. ETA ~[X] min."
**And** il crée un workflow en background et retourne `workflow_run_id`

**Given** 3 cas de test : (a) scaffolding nouveau module, (b) refactoring existant, (c) bug fix
**When** Dev Lead traite chacun
**Then** la décomposition est cohérente et les agents assignés logiques
**And** les tests E2E passent

### Story 5.2: Code Researcher — Exploration codebase + analyse dépendances

As the Dev Lead,
I want un Code Researcher (archétype Chercheur) qui explore le codebase, trouve les fichiers pertinents et analyse les dépendances pour une tâche donnée,
So que les Producteurs downstream aient le bon contexte (FR51).

**Acceptance Criteria:**

**Given** un agent-template `Code Researcher` créé depuis l'archétype Chercheur
**When** je consulte sa config
**Then** ses outils MCP incluent filesystem (read-only) et grep/ripgrep
**And** sa politique limite les dirs accessibles aux projets Dev

**Given** une tâche "refactorer le module paiements"
**When** Code Researcher s'exécute
**Then** son output contient `{relevant_files[], dependencies_graph, existing_patterns, risk_areas}`
**And** le format est un contrat structuré consommable par Architect Analyst

**Given** le dogfooding sur Agentive lui-même
**When** je lance "ajouter un nouveau module M13"
**Then** Code Researcher trouve les patterns des modules M2-M12 existants
**And** les suggère comme baseline

### Story 5.3: Architect Analyst + Code Producer

As the Dev Lead,
I want un Architect Analyst (Analyste) qui analyse l'impact et propose une approche technique, et un Code Producer (Producteur) qui l'implémente en suivant les conventions du projet,
So que les décisions architecturales précèdent le code et l'implémentation suit le plan.

**Acceptance Criteria:**

**Given** 2 agent-templates créés (`Architect Analyst`, `Code Producer`)
**When** je consulte leurs outputs respectifs
**Then** Analyst output : `{approach, tradeoffs, risks, test_strategy}` comme contrat structuré
**And** Producer output : `{code_diffs[], tests[], docs_snippets[]}` comme contrat structuré

**Given** Producer est configuré
**When** il génère du code
**Then** son prompt système référence les conventions projet via le namespace mémoire `métier` (type 2)
**And** il wrappe tous les `<tool_output>` de Code Researcher

**Given** l'input d'Architect Analyst
**When** il produit son approche
**Then** la complexité estimée, les risques et les alternatives sont documentés
**And** Code Producer s'appuie explicitement sur cette approche dans son prompt

### Story 5.4: Code Reviewer + Test Engineer (diversité qualité)

As the Dev Lead,
I want un Code Reviewer (Contrôleur) qui review la qualité avec feedback conversationnel, et un Test Engineer (Contrôleur) qui génère les tests, tous deux sur des LLM/params différents des Producteurs,
So que les quality gates ont une vraie diversité d'angles morts (FR45, FR15).

**Acceptance Criteria:**

**Given** 2 agent-templates `Code Reviewer` + `Test Engineer` archétype Contrôleur
**When** je consulte leurs configs
**Then** leur `llm_model` ou `llm_params` diffère de `Code Producer` (enforcement FR15)
**And** Code Reviewer utilise le schema `ReviewComment` (cf Story 2.8)

**Given** un output de Code Producer
**When** Code Reviewer le traite
**Then** il retourne une liste de commentaires localisés par sévérité
**And** le verdict global (pass/fail/needs_fix) est visible dans l'`OutputCard`

**Given** Test Engineer traite un code diff
**When** il génère les tests
**Then** il produit des tests unit + integration adaptés au framework du projet (détection via mémoire namespace `métier`)
**And** la couverture cible est configurable par workflow

**Given** les métriques Dashboard
**When** je consulte la santé du Pôle Dev
**Then** `rejected_ratio_per_producer` et `retry_on_review_per_agent` sont visibles

### Story 5.5: CI/CD Watcher — Veilleur de pipelines

As John,
I want un CI/CD Watcher (Veilleur) qui surveille les pipelines (GitHub Actions) et alerte sur vulnérabilités ou échecs,
So que les scénarios proactifs (J1 CVE détectée overnight) fonctionnent (FR46).

**Acceptance Criteria:**

**Given** un agent-template `CI/CD Watcher` archétype Veilleur
**When** je consulte sa config
**Then** ses outils MCP incluent github API (workflow runs, security alerts)
**And** le template est prêt à être ordonnancé via M11 Scheduler (fréquence par défaut 15 min). Note : M11 Scheduler est livré par Story 7.3 (Sprint 3) — en Sprint 2, l'agent est validé via déclenchement manuel + Event Hooks (Story 8.5) ; l'ordonnancement automatique s'active en Sprint 3.

**Given** le Watcher détecte une vulnérabilité critique
**When** il traite l'événement
**Then** une alerte est remontée au Dashboard avec AlertBanner severity=critical
**And** une notification proactive est déclenchée (< 60s, NFR18)
**And** un Event Hook (cf Epic 8) peut auto-déclencher un workflow de remédiation

**Given** le Watcher est exécuté sur le repo Agentive lui-même
**When** un build échoue
**Then** l'alerte est correcte et actionnable (lien direct vers le run, suggestion diagnostic)

### Story 5.6: Doc Writer + Sprint Reporter

As John,
I want un Doc Writer (Communicateur) qui génère de la doc technique, et un Sprint Reporter (Analyste) qui génère un résumé narratif périodique des tâches complétées,
So que le Dashboard narratif (J1 opening) et le J4 (Sophie télécharge la doc) sont possibles (FR47).

**Acceptance Criteria:**

**Given** 2 agent-templates créés (`Doc Writer`, `Sprint Reporter`)
**When** Doc Writer est déclenché après validation d'un Code Producer output
**Then** il produit du markdown structuré (API docs ou ADR selon la nature du changement)
**And** l'output est persistable + exportable

**Given** Sprint Reporter s'exécute
**When** il génère son résumé
**Then** le format est narratif (pas une liste) avec section "Fait", "En cours", "Recommandations"
**And** il s'appuie sur la mémoire `opérationnelle` (runs complétés période) pour le contexte

**Given** le Dashboard widget "Résumé Sprint Reporter"
**When** je consulte la vue
**Then** le résumé le plus récent est affiché en markdown rendered
**And** un bouton "Régénérer" déclenche un nouveau run

_Note : en Sprint 2, cette story livre uniquement les templates d'agents + l'output markdown structuré persisté + exportable. L'intégration UI Dashboard (widget "Résumé Sprint Reporter") est livrée par Story 7.2 en Sprint 3._

---

## Epic 6: Chat Interface & Human-in-the-Loop

Chat Agentive avec streaming SSE, slash commands, workflow inline, validation inline et Command Palette.

### Story 6.1: Chat UI avec streaming SSE

As John,
I want un Chat avec streaming SSE temps réel (premier token < 200ms) affichant messages user/agent/système,
So que la délégation me paraît immédiate et productive (FR25, NFR2).

**Acceptance Criteria:**

**Given** la route `/chat` via TanStack Router
**When** j'ouvre le Chat
**Then** l'historique se charge via TanStack Query avec skeleton 2-3 messages (UX-DR30)
**And** le layout respecte max-width 720px centré + sidebar historique 280px (UX-DR22)

**Given** j'envoie un message
**When** l'agent répond
**Then** le streaming SSE commence < 200ms (NFR2)
**And** le hook `useSSE` gère reconnect avec backoff exponentiel + circuit breaker (AR32)
**And** `AbortController` cleanup dans `useEffect` évite les races React StrictMode

**Given** une perte de connexion
**When** SSE se reconnecte
**Then** le stream reprend au point correct (via correlation_id + séquence token)
**And** aucun token n'est dupliqué côté UI

### Story 6.2: Slash commands + autocomplétion @/#

As John,
I want taper des slash commands (`/deploy`, `/review`, `/status`, `/traces`) et utiliser `@` (agents) / `#` (projets) en autocomplétion,
So que les actions fréquentes sont keyboard-first (UX-DR33).

**Acceptance Criteria:**

**Given** je tape `/` dans le Chat input
**When** l'autocomplétion s'affiche
**Then** les 4 slash commands principales sont listées avec description + raccourci clavier
**And** la documentation in-app (Cmd+/) les décrit toutes

**Given** je tape `@` ou `#`
**When** le popover autocomplétion s'affiche
**Then** la liste des agents (resp. projets) accessibles est filtrée en fuzzy search
**And** la navigation Tab + Enter sélectionne l'item

**Given** je soumet un message contenant `@Dev Lead décompose X`
**When** le message est envoyé
**Then** le Dev Lead est explicitement ciblé (routage court-circuite l'orchestration hybride)

### Story 6.3: Workflow inline + messages multi-auteur

As John,
I want voir un `WorkflowCard` live inline dans le Chat montrant chaque step avec statut (`✓` / `⟳` / `⏸`) et plusieurs agents identifiés par avatar/badge,
So que je vois en temps réel qui fait quoi (UX-DR10, UX-DR34).

**Acceptance Criteria:**

**Given** un workflow multi-agent démarre
**When** le Dev Lead répond dans le Chat
**Then** un `WorkflowCard` expandable apparaît inline avec la liste des steps
**And** chaque step montre `agent_name + status_icon + duree/ETA`
**And** les mises à jour arrivent via SSE sans refresh

**Given** le workflow avance
**When** un nouveau step démarre
**Then** son indicateur passe `⏸ → ⟳` avec pulse animation (arrêtée si `prefers-reduced-motion`)
**And** le live region annonce la transition aux screen readers

**Given** plusieurs agents parlent dans le même workflow
**When** je consulte le Chat
**Then** chaque `ChatMessage` multi-auteur a un avatar coloré par archétype + badge rôle (UX-DR13)
**And** la distinction visuelle user/agent/système est claire

### Story 6.4: Output cards avec validation inline (HITL)

As John,
I want chaque output d'agent présenté comme un `OutputCard` avec preview + score qualité + boutons (Valider / Commenter / Voir Trace / Relancer),
So que je valide ou rejette en 1 clic sans quitter le Chat (FR26, UX-DR11).

**Acceptance Criteria:**

**Given** un output d'agent arrive dans le Chat
**When** l'`OutputCard` s'affiche
**Then** elle montre header (titre, badge score qualité, status), preview tronqué avec fade-out, 4 boutons d'action
**And** les variants `code|document|data` adaptent le rendu preview

**Given** je clique `[Valider]`
**When** l'action est envoyée
**Then** l'UI applique optimistic UI (carte se compacte immédiatement)
**And** un event `output_validated` est publié + audit event
**And** les métriques Dashboard sont mises à jour

**Given** je clique `[Commenter]`
**When** je tape un commentaire
**Then** il est réinjecté dans la conversation comme follow-up du workflow
**And** le workflow peut boucler sur une itération

**Given** je clique `[Relancer]` sur un output rejeté
**When** un nouveau run est lancé
**Then** les commentaires du Reviewer sont injectés comme feedback au Producer
**And** le nouveau run est lié au précédent (lineage traçable)

### Story 6.5: Command Palette Cmd+K

As John,
I want une Command Palette accessible via Cmd+K (shadcn `Command` / cmdk) avec fuzzy search sur workflows, agents, projets, actions,
So que je navigue sans souris (UX-DR33).

**Acceptance Criteria:**

**Given** j'appuie Cmd+K
**When** la palette s'ouvre
**Then** elle apparaît en moins de 100ms avec focus dans l'input
**And** 4 catégories sont visibles : Workflows, Agents, Projets, Actions

**Given** je tape "dev"
**When** la recherche fuzzy s'exécute
**Then** les résultats sont classés par pertinence en < 50ms
**And** les raccourcis clavier associés sont affichés à droite de chaque item

**Given** je navigue flèches + Enter
**When** je sélectionne un item
**Then** l'action correcte est déclenchée (navigation vers la page, ouverture d'un agent, etc.)
**And** Escape ferme la palette

---

## Epic 7: Dashboard, Alerts & Scheduler

Cockpit de pilotage avec métriques temps réel, alertes configurables et Scheduler.

### Story 7.1: Dashboard métriques temps réel (MetricBlocks)

As John,
I want un Dashboard avec `MetricBlock`s affichant métriques temps réel (refresh < 2s, fraîcheur < 30s) par département/projet : workflows actifs, score qualité moyen, taux de retry, recall mémoire,
So que j'ai un cockpit d'un coup d'œil (FR27, NFR17).

**Acceptance Criteria:**

**Given** le route `/dashboard`
**When** j'ouvre le Dashboard
**Then** la grille CSS 12 colonnes (gap 24px) est visible avec cartes `col-span-4/6/8/12` (UX-DR21)
**And** chaque `MetricBlock` a label, valeur tabular-nums, tendance vert/rouge/muted, action inline si applicable

**Given** les métriques OpenTelemetry sont exposées via Prometheus
**When** le Dashboard les lit
**Then** le refresh via TanStack Query polling + SSE push est < 2s
**And** les données sont fraîches à < 30s de latence

**Given** aucune métrique disponible (1er usage)
**When** le Dashboard est vide
**Then** un empty state "Tes métriques apparaîtront dès la première tâche" s'affiche avec CTA vers le Chat (UX-DR29)

### Story 7.2: Sprint Reporter — Résumé narratif Dashboard

As John,
I want le Dashboard afficher un résumé narratif auto-généré par le Sprint Reporter (pas une liste — un récit),
So que mon ouverture matinale me fait lire un rapport, pas un feed (FR28).

**Acceptance Criteria:**

**Given** un widget dédié "Résumé Sprint Reporter" sur le Dashboard
**When** la page charge
**Then** le résumé le plus récent est rendu en markdown (sections Fait / En cours / Recommandations)
**And** un bouton `[Régénérer]` permet déclenchement manuel

**Given** le Scheduler (cf Story 7.3) déclenche une exécution périodique
**When** Sprint Reporter s'exécute
**Then** un nouveau résumé est stocké et affiché
**And** l'ancien reste accessible via historique

### Story 7.3: Scheduler workflows récurrents (M11)

As John,
I want planifier l'exécution récurrente de workflows (cron-like),
So que le CI/CD Watcher tourne toutes les 15 min et les rapports hebdo partent le lundi (FR8).

**Acceptance Criteria:**

**Given** le module M11 Scheduler
**When** je fais `POST /api/v1/schedules` avec `{workflow_id, cron_expr, active, timezone}`
**Then** le schedule est persisté
**And** il apparaît dans Config > Schedules

**Given** un schedule actif
**When** le worker cron s'exécute à l'horaire
**Then** un `workflow_run` est créé avec la source `scheduler`
**And** l'exécution est tracée et auditable

**Given** un schedule avec cron invalide
**When** la validation s'exécute
**Then** la requête échoue 422 avec explication

### Story 7.4: Configuration des seuils d'alerte

As John,
I want configurer des seuils d'alerte par agent et par métrique (ex: retry > 25% sur 24h → alerte),
So que les notifications proactives collent à mon profil opérationnel (FR33).

**Acceptance Criteria:**

**Given** l'UI Config > Alertes
**When** je crée une règle
**Then** le formulaire demande `{scope (agent_id/département/global), metric, operator, threshold, window}`
**And** la règle est persistée dans `alert_rules`

**Given** une règle configurée
**When** le worker évaluation s'exécute (via M11 Scheduler)
**Then** la métrique est lue depuis OpenTelemetry
**And** si le seuil est franchi, un alert event est publié

### Story 7.5: Notifications proactives (AlertBanner)

As John,
I want recevoir une notification proactive (< 60s après franchissement du seuil) affichée comme `AlertBanner` avec contexte + 1-2 actions,
So que je peux réagir dans le Journey Diagnosticien (FR32, NFR18).

**Acceptance Criteria:**

**Given** un alert event
**When** le service notifications le traite
**Then** l'`AlertBanner` est visible dans < 60s dans l'UI (via SSE push)
**And** le variant adapté (`critical|warning|info|resolved`) applique la couleur sémantique (UX-DR3, UX-DR15)

**Given** une alerte `critical`
**When** elle apparaît
**Then** 1-2 actions contextuelles sont présentes (ex: `[Ouvrir Trace]`, `[Ajuster seuil]`)
**And** `role="alert"` (ARIA) déclenche l'annonce screen reader

**Given** je clique une action
**When** elle est exécutée
**Then** la navigation contextuelle fonctionne (URL state préservé via search params TanStack Router)

### Story 7.6: Monitoring qualité + retry + benchmark mensuel recall

As the system,
I want mesurer score qualité par output, retry par agent + tendance, corrélation retry × mémoire, et exécuter un benchmark mensuel recall (20 requêtes, % top-5 pertinents),
So que la santé plateforme est objectivement mesurable (FR39, FR40, FR41, FR42).

**Acceptance Criteria:**

**Given** un output est validé/rejeté
**When** le score qualité est calculé
**Then** il est persisté dans `output_scores` et exposé en métrique OTel
**And** visible dans Dashboard + Trace Explorer

**Given** des runs s'accumulent
**When** la métrique `retry_rate_per_agent_30d` est calculée
**Then** la tendance (up/down/stable) est exposée
**And** un widget Dashboard affiche le top-3 agents à surveiller

**Given** la corrélation retry × mémoire
**When** le job d'analyse nocturne s'exécute
**Then** les retries sur un pattern déjà rencontré (via memory) sont identifiés
**And** le `retry_on_known_pattern_ratio` cible < 5% (PRD critère succès technique)

**Given** un schedule mensuel du benchmark recall
**When** il s'exécute
**Then** 20 requêtes types parcourent la mémoire et mesurent le % chunks pertinents dans top-5
**And** le résultat alimente un widget Dashboard "Health" avec historique

---

## Epic 8: Trace Explorer & Agent Debugger

Diagnostic profond et navigation contextuelle — support du Journey 2.

### Story 8.1: Trace Explorer tree + detail panel

As John,
I want un Trace Explorer avec tree view des nodes (hiérarchie workflow) + panneau détail à droite,
So que je drill-down workflow → agent → LLM call → tool call (FR30).

**Acceptance Criteria:**

**Given** la route `/trace/:workflow_run_id`
**When** j'ouvre un trace
**Then** le layout est tree 320-340px à gauche + detail panel fluide à droite (UX-DR23)
**And** les `TraceNode` affichent indent, status dot, nom opération (mono), durée (mono muted)

**Given** je navigue au clavier
**When** j'utilise les flèches
**Then** la navigation tree respecte `aria-expanded`, `aria-level`, `role="tree"` (UX-DR14, UX-DR39)
**And** Entrée ouvre le détail d'un node

**Given** les traces OpenTelemetry
**When** un run se termine
**Then** tous les spans sont queryables et rendus dans le tree
**And** la traçabilité E2E (sources → données → raisonnement → décision) est préservée (NFR15)

### Story 8.2: Métriques contextuelles overlay (Dashboard-in-Trace)

As John,
I want voir des métriques overlay sur chaque Trace node (durée, coût, tokens, score, comparaison historique),
So que les problèmes de performance sont visibles sans quitter la vue trace (FR31, innovation MVP #6).

**Acceptance Criteria:**

**Given** un Trace rendu
**When** j'active le toggle "Métriques overlay"
**Then** chaque node affiche durée/coût/tokens/score en attributs overlay
**And** la comparaison historique (baseline du run précédent) est visible (vert = mieux, rouge = pire)

**Given** un node avec span attributes OTel
**When** je consulte le detail panel
**Then** les attributes sont exposés sans payloads complets (filtrés pour éviter fuite de données)

### Story 8.3: Navigation contextuelle (Trace → Config / Chat)

As John,
I want cliquer un agent node dans le Trace Explorer et ouvrir sa Config ou ré-ouvrir la conversation Chat en 1 clic, avec URL state préservé entre les espaces,
So que je diagnostique et agis sans perdre le contexte (FR29, UX-DR25).

**Acceptance Criteria:**

**Given** un agent node sélectionné
**When** je clique `[Ouvrir Config]`
**Then** je navigue vers `/config/agents/:id?from=trace&run_id=...`
**And** la Config est pré-sélectionnée sur le bon agent

**Given** un agent node sélectionné
**When** je clique `[Retour au Chat]`
**Then** je navigue vers `/chat?thread=...` avec la conversation originelle ouverte
**And** le contexte Zustand préserve la position/selection

**Given** je navigue entre espaces
**When** les search params changent
**Then** l'état est deep-linkable (URL partageable)
**And** le flow J2 de bout en bout est testé E2E

### Story 8.4: Agent Debugger — Diagnostic automatique

As John,
I want un Agent Debugger (agent actif) qui analyse les traces d'un agent en échec et propose un diagnostic (prompt / mémoire / outil / input),
So que j'ai des hypothèses racines plus vite pendant le Journey 2 (FR49).

**Acceptance Criteria:**

**Given** un agent-template `Agent Debugger` archétype Analyste
**When** il est configuré
**Then** ses outils incluent accès read-only aux spans OTel + memory queryable

**Given** un bouton `[Diagnostiquer]` dans le detail panel Trace d'un agent en échec
**When** je clique
**Then** Agent Debugger s'exécute avec `{workflow_run_id, agent_id}` en input
**And** son output structuré est `{hypotheses[{cause, confidence, evidence, suggested_fix}], recommended_action}`

**Given** le diagnostic est rendu
**When** je consulte
**Then** il est intégré dans le Trace detail panel avec boutons d'action
**And** les actions peuvent déclencher `[Modifier Prompt]` / `[Purger chunk]` / `[Ajuster outil]`

### Story 8.5: Event Hooks sur le bus d'événements

As John,
I want configurer des Event Hooks (règles "quand X → déclencher Y") sur le bus d'événements inter-modules/départements,
So que j'automatise des réactions sans hardcoder (ex: CI/CD Watcher détecte CVE → déclenche Dev Lead workflow) (FR50).

**Acceptance Criteria:**

**Given** l'UI Config > Event Hooks
**When** je crée un hook
**Then** le formulaire demande `{event_pattern, action_type, action_payload, rate_limit}`
**And** les patterns supportent wildcards (ex: `cicd.*.critical`)

**Given** un hook actif
**When** un event matche le pattern sur le bus
**Then** le worker trigger l'action (`workflow_run` / `notification` / `webhook`)
**And** un audit event `hook_triggered` est publié

**Given** un hook pourrait créer une boucle infinie
**When** le rate limit est atteint
**Then** le hook est circuit-break avec alerte
**And** John reçoit une notification pour investigation

---

## Epic 9: Security, Audit & Trust Controls

Cross-cutting : audit, chiffrement, gestion clés API, budget caps, rate limiting, secrets management.

### Story 9.1: Audit trail complet (middleware + partitioned)

As John,
I want chaque action (agent ou humain) loguée dans `audit_events` partitionnée mensuellement et immutable avec rétention ≥ 90j,
So que je peux auditer toute action et prouver l'intégrité du système (FR34, NFR8).

**Acceptance Criteria:**

**Given** le middleware audit FastAPI
**When** une requête entre et sort
**Then** un audit event `{actor, action, target, correlation_id, tenant_id, timestamp, payload_hash}` est persisté
**And** les intercepteurs SQLAlchemy capturent les mutations DB critiques

**Given** la table `audit_events`
**When** les migrations s'appliquent
**Then** elle est partitionnée par mois (via pg_partman ou triggers)
**And** `REVOKE DELETE, UPDATE` est appliqué sur chaque partition (immutabilité)

**Given** la rétention politique ≥ 90j
**When** le cron purge mensuelle s'exécute
**Then** les partitions > rétention sont exportées en archive (S3-compatible ou local) puis détachées
**And** un audit event `audit_archived` consigne l'opération

**Given** `GET /api/v1/audit` en cursor-based pagination
**When** je query avec filtres
**Then** les événements correspondants sont retournés sans perte
**And** les clés API ou secrets n'apparaissent jamais (NFR9)

### Story 9.2: Chiffrement at-rest (Fernet / AES-256) sur champs sensibles

As the system,
I want chiffrer les champs sensibles at-rest (clés API LLM, credentials MCP, données clients) via Fernet (AES-256) avec clé séparée des données,
So qu'un compromis DB ne fuit pas les secrets (FR35, NFR6).

**Acceptance Criteria:**

**Given** le module `shared/crypto/`
**When** un champ chiffré est écrit
**Then** un wrapper Fernet encrypt avec la clé env `AGENTIVE_ENCRYPTION_KEY`
**And** un SQLAlchemy `TypeDecorator` applique encrypt/decrypt transparently

**Given** des tests unit encrypt/decrypt roundtrip
**When** la CI les exécute
**Then** ils passent pour 1000+ itérations avec payloads variés

**Given** la clé est rotatée
**When** le système démarre avec une nouvelle clé
**Then** un script de re-encryption migre les anciens champs (one-shot)
**And** l'ancienne clé peut être supprimée après migration

### Story 9.3: Gestion des clés API LLM (ajout / rotation / suppression)

As John,
I want une UI pour gérer les clés API providers LLM (ajout, rotation, suppression) avec stockage chiffré et audit,
So que je ne hardcode aucune clé nulle part (FR36).

**Acceptance Criteria:**

**Given** l'UI Config > API Keys
**When** j'ajoute une clé
**Then** elle est chiffrée via Story 9.2 avant insertion
**And** l'UI ne la réaffiche jamais (mask "•••...1234")
**And** un audit event `apikey_added` est publié

**Given** je rotate une clé
**When** l'action est confirmée
**Then** la nouvelle clé remplace l'ancienne
**And** l'ancienne est marquée `revoked_at` + un audit event `apikey_rotated` est publié
**And** les workflows en cours utilisant l'ancienne clé continuent avec grace period (configurable)

**Given** `core/llm/` a besoin d'une clé
**When** il la demande
**Then** elle est récupérée décryptée uniquement en mémoire (jamais loggée, jamais exposée en REST response)

### Story 9.4: Budget caps par département / workflow

As John,
I want définir des budget caps par département et par workflow (max tokens / max cost / fenêtre temporelle) avec alerte puis blocage,
So que les coûts LLM restent maîtrisés (FR37).

**Acceptance Criteria:**

**Given** l'UI Config > Budgets
**When** je crée un cap
**Then** le schema `{scope, period_type, limit_usd OR limit_tokens}` est persisté
**And** la consommation actuelle est calculée depuis les métriques OTel

**Given** la consommation dépasse 80% du cap
**When** un workflow est déclenché
**Then** une alerte est affichée dans le Dry Run preview
**And** l'utilisateur doit confirmer explicitement

**Given** la consommation atteint 100% du cap
**When** un workflow est déclenché
**Then** l'exécution est bloquée
**And** un bypass `--force` nécessite justification + audit event `budget_bypassed`

### Story 9.5: Rate limiting multi-provider (slowapi + queues + backoff)

As the system,
I want gérer les rate limits providers LLM via slowapi + queues internes + retry avec backoff exponentiel + répartition entre providers,
So que les rate limits ne cassent pas les workflows (FR38, NFR14).

**Acceptance Criteria:**

**Given** `slowapi` intégré dans FastAPI
**When** un endpoint expose des appels LLM
**Then** le rate limit par client est appliqué

**Given** une queue interne par provider
**When** un provider atteint son rate limit
**Then** les requests entrantes sont mises en queue avec ordre FIFO
**And** le retry exponentiel + jitter respecte le `Retry-After` header si fourni

**Given** la distribution configurable (round-robin, weighted)
**When** plusieurs providers sont disponibles
**Then** la charge est répartie selon la config par agent ou workflow
**And** une métrique `rate_limit_hits_per_provider_per_day` alimente le Dashboard

**Given** un test avec provider mock retournant 429
**When** la suite CI s'exécute
**Then** le système ne crashe pas et complète le workflow via fallback

### Story 9.6: Secrets management (SOPS + age) + gitleaks + Caddy headers

As John,
I want les secrets chiffrés via SOPS + age (committable), détectés par gitleaks pre-commit, et Caddy qui serve avec security headers complets,
So que la plateforme est hardened by default (NFR9, AR21, AR25).

**Acceptance Criteria:**

**Given** une config `.sops.yaml` avec clé age publique
**When** je modifie `.env` chiffré
**Then** SOPS decrypt/encrypt transparently
**And** `decrypt at runtime` avant injection comme variables Docker

**Given** pre-commit actif avec gitleaks
**When** je tente un commit contenant un secret canary (ex: fake API key pattern)
**Then** le commit échoue avec détail de la fuite
**And** la CI re-vérifie via GitHub secret scanning

**Given** Caddy configuré
**When** je inspecte les headers d'une response
**Then** je trouve : HSTS (max-age=31536000), X-Content-Type-Options=nosniff, X-Frame-Options=DENY, Referrer-Policy=strict-origin-when-cross-origin, CSP avec nonces

**Given** un test leak canary dans la CI
**When** un contributeur tente un commit contenant le canary
**Then** la CI échoue et bloque le merge
