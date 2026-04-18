---
stepsCompleted: [1, 2, 3, 4]
inputDocuments: ['prd_x_brainstorm.md', 'prd_x_brainstorm_v2.md', 'rapport_outils_agent_ia.md', 'workflow_agent_ia.md']
session_topic: 'Agentive — AI Company Builder : architecture complète, modules, agents, Company Architect'
session_goals: 'Compléter, affiner et détailler le projet. Résoudre les tensions architecturales. Spécifier le Company Builder. Définir les modules fondamentaux domain-agnostic.'
selected_approach: 'progressive-flow'
techniques_used: ['first-principles-thinking', 'morphological-analysis', 'six-thinking-hats', 'decision-tree-mapping']
ideas_generated: ['agent-debugger', 'agent-evolutionnaire', 'dry-run-mode', 'agent-playground', 'department-recipes', 'event-hooks', 'recrutement-dynamique']
context_file: ''
department_pilote: 'dev'
decisions_actees: ['LangGraph orchestrateur + CrewAI agents', 'Consolidateur = capacité orchestrateur', 'pgvector dans PostgreSQL', 'Pôle Dev comme département pilote']
---

# Brainstorming Session Results

**Facilitateur:** John
**Date:** 2026-03-18

## Session Overview

**Sujet:** Agentive — AI Company Builder
**Objectifs:** Compléter, structurer et détailler l'architecture du projet. Résoudre la tension SEO-spécifique vs domain-agnostic. Spécifier le Company Architect. Définir les modules fondamentaux.

### Context Documents

- `prd_x_brainstorm.md` — Brainstorm initial orienté SaaS SEO (v1)
- `prd_x_brainstorm_v2.md` — Pivot vers AI Company Builder self-hosted (v2)
- `rapport_outils_agent_ia.md` — Cartographie technique des outils
- `workflow_agent_ia.md` — Workflow détaillé agence SEO (12 agents)

### Session Setup

Le projet Agentive est un outil interne pour l'agence de John, destiné à créer des départements IA (juridique, fiscal, SEO, dev, etc.) 100% custom et customisables. Les documents existants sont des brainstorms partiels qui nécessitent complétion, structuration et approfondissement.

---

## Phase 1 — First Principles Thinking : Les 10 Principes Fondamentaux

### Principes validés par John

| # | Principe | Vérité irréductible |
|---|---|---|
| 1 | **Compréhension du métier** | Pas d'entreprise IA sans modélisation du métier. Avant de créer un agent, comprendre les rôles humains, livrables, entrées/sorties, outils du métier. |
| 2 | **Agent = rôle humain précis** | La spécialisation extrême fait la qualité. "Consultant audit technique" > "Agent SEO". La granularité de la décomposition détermine la qualité du système. |
| 3 | **Contrats de données inter-agents** | Structures définies, pas du texte libre. Chaque agent a un Input Schema et un Output Schema comme des APIs entre microservices. |
| 4 | **Human-in-the-loop partout** | L'intervention humaine est un événement du système, pas une exception. Le CEO peut intervenir à tout moment sans casser le flux. |
| 5 | **Mémoire structurée partagée** | 4 types : mémoire client, mémoire métier, mémoire opérationnelle, mémoire contextuelle. Sans mémoire structurée, chaque exécution repart de zéro. |
| 6 | **Traçabilité totale** | Chaque output remonte à sa source. Un output sans provenance est une hallucination potentielle. La traçabilité est la crédibilité du système. |
| 7 | **Introspection du système** | Le système sait ce qu'il est et ce qu'il sait faire. Prérequis pour que le Company Architect fonctionne et que le système évolue intelligemment. |
| 8 | **Départements autonomes et interconnectés** | Architecture en holding : chaque département est une mini-entreprise avec ses agents, workflows, mémoire. Coordination via bus d'événements + mémoire globale. |
| 9 | **Le prompt est le produit** | La qualité du system prompt détermine à 80% la qualité de l'agent. Le Company Architect crée des prompts de qualité professionnelle encapsulés dans une configuration d'exécution. |
| 10 | **Opérationnel avant parfait** | La valeur se prouve agent par agent. Un seul agent qui fonctionne bien vaut mieux qu'une architecture complète qui ne produit rien. |

---

## Phase 2 — Morphological Analysis : Cartographie du Système

### Dimension 1 : Les 12 modules de la plateforme

| Module | Rôle | Criticité |
|---|---|---|
| **M1 — Company Architect** | Méta-agent qui guide la création d'entreprises IA (7 étapes conversationnelles) | Sprint 5 |
| **M2 — Agent Registry** | Registre de tous les agents : identité, configuration, statut, contrats. Introspection du système. Distinction agent-template / agent-instance. | Cœur |
| **M3 — Workflow Engine** | Orchestration : graphes d'agents, sync points, parallélisme, scatter-gather, gestion d'erreurs | Cœur |
| **M4 — Memory Manager** | 4 types de mémoire (client, métier, opérationnelle, contextuelle). Indexation, namespaces, droits d'accès, versioning. | Cœur |
| **M5 — Tool Hub** | Registre et gestion des MCP Servers. Connexion, config, monitoring, health check. | Cœur |
| **M6 — Dashboard** | Vue CEO : statut agents, flux d'activité, métriques (tokens, coûts, tâches), alertes | Essentiel |
| **M7 — Chat Interface** | Communication avec les agents. Human-in-the-loop. Directives stratégiques. | Essentiel |
| **M8 — Agent Configurator** | Édition des fiches agents via UI : prompt, outils, modèle LLM, seuils, mémoire | Essentiel |
| **M9 — Topology View** | Graphe visuel de l'organisation : agents, flux, dépendances, statuts temps réel | V1 |
| **M10 — Reporting Engine** | Génération de livrables formatés, agrégation des outputs, KPIs | V1 |
| **M11 — Scheduler** | Jobs récurrents : veille, reporting planifié, tâches programmées | V1 |
| **M12 — Trace Explorer** | Navigation dans les chaînes de raisonnement : output → sources → données → raisonnement | Essentiel |

**Organisation frontend — 4 espaces UI :**

| Espace UI | Modules exposés |
|---|---|
| Dashboard | M6 + M12 + M11 |
| Config | M2 + M5 + M8 + M4 |
| Chat | M7 + M1 |
| Topologie | M9 + M3 (vue) + M10 |

### Dimension 2 : Les 8 archétypes universels d'agents

| Archétype | Rôle universel | Applicable à tout domaine |
|---|---|---|
| **Orchestrateur** | Route, planifie, coordonne, gère les erreurs, recrute dynamiquement | ✓ |
| **Chercheur** | Collecte des données, crawle, interroge des sources | ✓ |
| **Analyste** | Extrait des patterns, score, compare, synthétise | ✓ |
| **Producteur** | Crée des livrables : contenu, documents, code | ✓ |
| **Stratège** | Synthétise les analyses, propose un plan d'action | ✓ |
| **Contrôleur** | Vérifie la qualité, valide, score les outputs | ✓ |
| **Veilleur** | Monitore, détecte les changements, alerte | ✓ |
| **Communicateur** | Interface avec l'humain, reformule, rapporte | ✓ |

**Processus de création d'agents :** Comprendre le métier → Identifier les rôles humains → Mapper sur les archétypes → Spécialiser avec le contexte métier.

### Dimension 3 : Anatomie universelle d'un agent

Chaque agent, quel que soit son domaine, contient :

- **Identity** : name, role, archetype, department
- **System Prompt** : rôle précis, expertise, ton, contraintes, format de sortie, anti-patterns, exemples
- **Contrats** : Input Schema, Output Schema, Résumé Schema
- **Outils (MCP)** : liste des MCP Servers assignés, permissions
- **Mémoire** : namespaces en lecture, namespaces en écriture
- **Qualité** : score minimum, critères de scoring, nombre max de retries
- **Modèle LLM** : modèle assigné, paramètres (température, max tokens)
- **Politique d'erreur** : retry policy, mode dégradé, seuil d'escalade humaine
- **Métadonnées** : dépendances, coût moyen, temps moyen, score de confiance moyen

### Dimension 4 : Architecture inter-départements

- **Intra-département** : agents communiquent via leur orchestrateur local (hub-and-spoke)
- **Inter-département** : bus d'événements partagé + mémoire globale. Pas de communication directe agent-à-agent cross-département.
- **Niveau plateforme** : Company Architect a une vue sur tous les départements.

### Dimension 5 : Processus Company Architect (7 étapes)

1. **Discovery** — Conversation structurée sur le métier
2. **Role Mapping** — Décomposition en rôles humains → mapping archétypes
3. **Agent Specification** — Fiches agents complètes (template + personnalisation)
4. **Workflow Design** — Graphe d'exécution, sync points, contrats
5. **Tool Binding** — Association outils MCP aux agents
6. **Memory Architecture** — Namespaces, droits, stratégie de chunking
7. **Deploy & Validate** — Configuration, vérification, test réel

### Dimension 6 : Mécanisme de Recrutement Dynamique

Quand un agent est surchargé (volume de données, saturation de contexte, timeout), l'Orchestrateur peut instancier N agents du même archétype pour répartir la charge (pattern scatter-gather), puis consolider les résultats.

**Déclencheurs :** volume de données, saturation de contexte, temps d'exécution.
**Impact architecture :** M2 distingue agent-template / agent-instance. M3 gère le scatter-gather.

---

## Phase 3 — Six Thinking Hats : Stress Test

### Chapeau Blanc (Faits)
- Stack validé : CrewAI + LangGraph + MCP + pgvector + FastAPI + React + Docker Compose
- Aucun agent testé en conditions réelles — toute l'architecture est théorique
- Inconnues : chunking RAG optimal, coût réel en tokens, qualité des prompts auto-générés

### Chapeau Rouge (Intuition)
- L'idée de diriger une entreprise IA est puissante et motivante
- Risque de construire le cockpit avant l'avion
- La dépendance aux LLMs externes est une préoccupation latente

### Chapeau Jaune (Bénéfices)
- Effet composé de la mémoire (chaque exécution enrichit la suivante)
- Réutilisabilité inter-départements (MCP, archétypes)
- Company Architect comme multiplicateur de scaling
- Traçabilité comme argument commercial différenciant
- Spécialisation progressive (un département à la fois)

### Chapeau Noir (Risques)
1. **Syndrome du framework** — construire la plateforme au lieu des agents. Mitigation : principe #10.
2. **Qualité des prompts auto-générés** — 60-70% vs expert humain. Mitigation : templates + personnalisation.
3. **Explosion de complexité inter-départements**. Mitigation : bus d'événements, pas de comm directe.
4. **Coûts tokens**. Mitigation : résumés de passage, modèles légers, estimation pré-exécution.
5. **Fragilité MCP custom**. Mitigation : tests auto, mode dégradé, alertes santé.
6. **Piège du monolithe**. Mitigation : interfaces claires entre modules dès le jour 1.

### Chapeau Vert (Idées créatives)
1. **Agent Debugger** — debug les autres agents via le Trace Explorer
2. **Agent Évolutionnaire** — analyse les métriques, propose des améliorations de prompts/config
3. **Mode Dry Run** — simulation pré-exécution avec estimation de coût
4. **Agent Playground** — sandbox de test par agent isolé
5. **Recettes de départements** — format YAML/JSON exportable/importable
6. **Event Hooks** — règles configurables sur le bus d'événements inter-départements
7. **Recrutement Dynamique** — instanciation d'agents supplémentaires en cas de surcharge (proposé par John)

### Chapeau Bleu (Processus)
**Résolution de la tension centrale** (ambition vs pragmatisme) : construire le premier département à la main, en s'assurant que l'architecture supporte le Company Architect futur, sans le construire tout de suite. Le Company Architect est le dernier module, pas le premier.

---

## Phase 4 — Decision Tree : Plan d'implémentation

### Décisions actées

| Décision | Choix | Justification |
|---|---|---|
| Département pilote | **Pôle Dev** | Expertise de John, feedback immédiat, dogfooding (les agents aident à construire Agentive) |
| Orchestration | **LangGraph + CrewAI hybride** | LangGraph pour l'orchestrateur (checkpointing, HITL), CrewAI pour les agents métiers |
| Consolidateur | **Capacité de l'Orchestrateur** | Pas d'archétype séparé au MVP, extraction si complexité le justifie |
| RAG | **pgvector dans PostgreSQL** | Simplicité, un seul service, migration vers Qdrant si besoin |

### Pôle Dev — Département pilote (8 agents)

| Agent | Archétype | Rôle |
|---|---|---|
| Dev Lead | Orchestrateur | Route les tâches, planifie, coordonne, gère les dépendances |
| Code Researcher | Chercheur | Explore le codebase, trouve les fichiers pertinents, analyse les dépendances |
| Architect Analyst | Analyste | Analyse l'impact des changements, évalue les options techniques, détecte les code smells |
| Code Producer | Producteur | Écrit du code, implémente des features, corrige des bugs, crée des tests |
| Tech Writer | Producteur | Génère la documentation technique, ADRs, README, docstrings |
| Code Reviewer | Contrôleur | Review qualité, conventions, sécurité, performance, tests |
| CI/CD Watcher | Veilleur | Monitore les pipelines, détecte fails, régressions, vulnérabilités |
| Sprint Reporter | Communicateur | Synthétise l'avancement, changelogs, rapports de sprint |

### Roadmap par sprints

**Sprint 0 — Fondations techniques**
- Monorepo Python + React
- PostgreSQL + pgvector
- FastAPI + WebSocket
- React + Tailwind (shell 4 espaces)
- Docker Compose

**Sprint 1 — Premier agent qui produit de la valeur**
- M2 Agent Registry (minimal)
- M4 Memory Manager (minimal)
- M5 Tool Hub (minimal)
- 1er agent Dev fonctionnel (testé en isolation sur le codebase Agentive)

**Sprint 2 — Département Dev complet**
- M3 Workflow Engine (minimal)
- Dev Lead (LangGraph orchestrateur)
- Tous les 8 agents configurés
- Workflow story-to-delivery de bout en bout

**Sprint 3 — Cockpit UI**
- M6 Dashboard + M12 Trace Explorer
- M7 Chat + Human-in-the-loop
- M8 Agent Configurator

**Sprint 4 — Scaling + 2ème département**
- 2ème département (SEO ou Juridique)
- Bus d'événements inter-départements
- Recrutement Dynamique
- M9 Topology View + Agent Playground + Dry Run

**Sprint 5 — Company Architect**
- M1 Company Architect (7 étapes conversationnelles)
- Templates de domaine (basés sur les départements construits)
- Export/Import recettes
- M10 Reporting + M11 Scheduler + Agent Évolutionnaire

**Sprint 6+ — Expansion**
- Agent Debugger, Marketplace, modèles locaux (Ollama), éditeur visuel workflows, A2A