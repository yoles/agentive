---
stepsCompleted: ['step-01-init', 'step-02-discovery', 'step-02b-vision', 'step-02c-executive-summary', 'step-03-success', 'step-04-journeys', 'step-05-domain', 'step-06-innovation', 'step-07-project-type', 'step-08-scoping', 'step-09-functional', 'step-10-nonfunctional', 'step-11-polish']
inputDocuments: ['brainstorming-session-2026-03-18-151800.md', 'brainstorming-session-2026-03-18-142205.md']
workflowType: 'prd'
documentCounts:
  briefs: 0
  research: 0
  brainstorming: 2
  projectDocs: 0
  projectContext: 0
classification:
  projectType: 'web_app'
  domain: 'AI Infrastructure / Agent Orchestration'
  complexity: 'high'
  projectContext: 'greenfield'
---

# Product Requirements Document - Agentive

**Author:** John
**Date:** 2026-03-20

## Executive Summary

### Glossaire

| Terme | Définition |
|---|---|
| **Agentive** | La plateforme — l'application web self-hosted |
| **Company Builder** | La vision produit — construire des entreprises IA |
| **Company Architect (M1)** | Le module qui guide la création de départements (post-MVP) |
| **Département** | Une unité autonome d'agents spécialisés dans un métier |
| **Archétype** | Un modèle universel de rôle d'agent (Orchestrateur, Chercheur, etc.) |

### Vision

Agentive est une application web self-hosted qui permet à un utilisateur unique de concevoir, déployer et piloter des départements IA composés d'agents spécialisés. Chaque agent incarne un rôle humain précis — analyste, producteur, contrôleur, veilleur — et collabore au sein de workflows orchestrés avec mémoire partagée, contrats de données structurés et traçabilité complète.

Le produit s'adresse en priorité à l'usage interne de l'agence de John, avec l'objectif de créer des pôles opérationnels (Dev, Juridique, Fiscal, SEO) 100% automatisés et customisables. Le département pilote est le Pôle Dev, choisi pour permettre un feedback critique immédiat et le dogfooding (les agents Dev contribuent à construire Agentive lui-même).

Le problème fondamental résolu : les frameworks d'agents IA existants (CrewAI, LangGraph, n8n) fournissent des briques, mais aucun ne propose la couche de composition — le système qui transforme ces briques en organisations cohérentes, spécialisées par métier, avec mémoire capitalisée et qualité traçable.

### Ce qui rend Agentive spécial

**Le Company Builder comme méta-système.** Agentive ne crée pas des agents — il crée des entreprises d'agents. Un processus en 7 étapes (Discovery → Role Mapping → Agent Specification → Workflow Design → Tool Binding → Memory Architecture → Deploy & Validate) guide la création de départements entiers à partir de la compréhension du métier cible.

**8 archétypes universels, extensibles par design.** Orchestrateur, Chercheur, Analyste, Producteur, Stratège, Contrôleur, Veilleur, Communicateur. Chaque rôle humain dans n'importe quel métier se mappe sur un ou plusieurs de ces archétypes. Ces 8 archétypes couvrent la majorité des structures organisationnelles observées dans les métiers de services et de conseil. L'architecture supporte l'ajout de nouveaux archétypes — les 8 sont un point de départ empirique, pas un dogme. L'ajout d'un 9ème ou 10ème archétype ne nécessite aucune modification de l'infrastructure, uniquement un nouveau template. Le département pilote (Dev) et les suivants serviront de validation empirique. Le sprint 4 (2ème département) constitue le test de validation critique.

**Le prompt est le levier le plus contrôlable, mais pas le seul.** Le prompt système est le facteur de qualité le plus directement contrôlable par l'utilisateur. Mais la qualité réelle d'un agent résulte de la combinaison prompt + modèle LLM + données RAG + outils disponibles + qualité des inputs. Agentive expose et optimise chacun de ces leviers. La valeur de la plateforme est dans l'orchestration qui fait collaborer ces leviers, la mémoire qui les rend plus intelligents à chaque exécution, et la traçabilité qui rend leurs résultats crédibles.

**Principe de diversité des contrôles.** Les agents Contrôleurs utilisent des modèles LLM ou des paramètres différents de ceux des agents qu'ils vérifient, pour éviter la convergence de biais. Un quality gate n'est un vrai gate que si le contrôleur a des angles morts différents du producteur.

**Architecture en holding.** Chaque département est autonome (ses agents, sa mémoire, ses workflows) mais interconnecté via un bus d'événements et une mémoire globale. Le scaling se fait département par département, sans big bang.

**Recrutement dynamique.** Quand un agent est surchargé, le système instancie automatiquement des agents supplémentaires du même archétype pour répartir la charge (pattern scatter-gather), puis consolide les résultats.

**Mémoire avec cycle de vie et structure en 4 types.** pgvector dans PostgreSQL assure le stockage vectoriel. Le Memory Manager (M4) organise la mémoire en 4 types structurés : mémoire client (données projet, briefs, livrables), mémoire métier (conventions, patterns, bonnes pratiques), mémoire opérationnelle (logs, métriques, historique d'exécution), mémoire contextuelle (contexte de la tâche en cours, éphémère). Au-dessus : expiration configurable par namespace (TTL par chunk), scoring de pertinence décroissant dans le temps, reranking en Python, filtrage par namespace, et archivage automatique des données obsolètes. Le système oublie autant qu'il mémorise.

**Tool Discovery & Specification (module hybride).** Le Tool Scout analyse les besoins en outils de chaque agent et recherche dans l'écosystème existant (registre MCP officiel, repositories GitHub, annuaires d'APIs publiques). C'est un module hybride : la partie LLM évalue la couverture fonctionnelle et rédige les spécifications d'outils manquants ; la partie déterministe assure l'audit de sécurité (analyse statique du code, vérification des CVE, exécution en sandbox, monitoring du trafic réseau). Aucune recommandation n'est auto-installée sans validation humaine.

**Trace Explorer comme hub de debug.** Le Trace Explorer (M12) est le point d'entrée unique pour le diagnostic : pour chaque output, remonter la chaîne complète (sources → données → raisonnement → décision) avec des actions contextuelles intégrées — accès direct à la configuration de l'agent, relance dans le Chat, vue du graphe dans Topologie.

### Scénarios types

**Scénario réactif :** John ouvre Agentive le matin. Le CI/CD Watcher a détecté 3 vulnérabilités overnight. Il demande au Dev Lead d'évaluer l'impact. L'Architect Analyst analyse les dépendances concernées, le Code Producer prépare les correctifs, le Code Reviewer — tournant sur un modèle différent — valide la qualité et la sécurité. John approve dans le Chat. Temps total : 20 minutes au lieu d'une demi-journée.

**Scénario proactif :** Lundi matin, pas d'alerte. John ouvre le Dashboard. Le Sprint Reporter a préparé un résumé automatique : 12 tâches complétées la semaine précédente, 2 workflows en attente de validation, 1 recommandation de l'Architect Analyst pour refactorer un module dont la complexité a dépassé le seuil. John priorise les validations dans le Chat, approuve la recommandation de refactoring, et le Pôle Dev se met au travail.

### Frontière MVP

**MVP = Sprints 0 à 3** — infrastructure + département Dev complet + cockpit UI. Le MVP est atteint quand John utilise quotidiennement le Pôle Dev pour de vraies tâches de son agence.

**Vision post-MVP = Sprints 4 à 6** — scaling multi-départements, Company Architect (M1), recettes exportables, Agent Évolutionnaire, expansion.

### KPIs cibles

| Métrique | Seuil | Justification |
|---|---|---|
| Score qualité par output | Défini par archétype, configurable par agent dans M8. Seuils calibrés empiriquement sur le département pilote. | Chaque archétype a ses propres critères (Chercheur : pertinence/couverture, Producteur : tests/conventions, Contrôleur : taux faux positifs) |
| Contrainte de rentabilité | Chaque workflow doit démontrer un avantage mesurable (temps, coût, ou qualité) vs exécution manuelle. Seuil configurable par département, calibré sur les premières exécutions réelles. | La rentabilité varie selon le domaine et la complexité |
| Temps d'exécution workflow standard | < 10 min | Au-delà, l'avantage de vitesse disparaît pour les cas courants |
| Taux de retry par agent | < 15% | Au-dessus, le prompt ou les outils nécessitent un ajustement |
| Disponibilité mémoire pertinente | > 90% recall | Les chunks remontés doivent être pertinents pour la tâche |

## Classification du projet

| Attribut | Valeur |
|---|---|
| **Type** | Application web (SPA, 4 espaces UI MVP : Dashboard, Config, Chat, Trace — Topology View M9 ajoutée en Growth) |
| **Domaine** | AI Infrastructure / Agent Orchestration |
| **Complexité** | Haute — orchestration multi-agents, RAG, méta-agent, architecture inter-départements |
| **Contexte** | Greenfield — nouveau projet, pas de codebase existant |
| **Orchestration** | Framework supportant checkpointing, human-in-the-loop natif, audit trail et scatter-gather. Choix actuel : LangGraph (mars 2026). L'architecture est découplée du framework via une couche d'abstraction (M3 Workflow Engine). CrewAI reste disponible comme accélérateur de prototypage. |
| **Stack** | PostgreSQL/pgvector + FastAPI + React + MCP + Docker Compose |
| **Déploiement** | Self-hosted, local-first |
| **Sécurité** | Authentification locale, chiffrement des données sensibles at-rest, isolation des namespaces mémoire par département, audit trail de toutes les actions |
| **Contrainte économique** | Estimation pré-exécution obligatoire (Mode Dry Run) |

## Critères de Succès

### Succès Utilisateur (John)

- **Usage quotidien réel** : ≥ 10 tâches/semaine confiées au Pôle Dev, dont au moins 3 de complexité non-triviale (impliquant 2+ agents)
- **Confiance proxy** : ≥ 70% des outputs approuvés sans vérification manuelle détaillée à 6 mois
- **Réduction du temps** : les tâches routinières (scaffolding, reviews, recherche) prennent ≤ 30% du temps manuel équivalent
- **Aha moment** : John délègue spontanément une tâche complexe sans hésiter — signe que la confiance est installée

### Succès Business

- **Rentabilité par workflow** : chaque workflow doit démontrer un avantage mesurable (temps, coût ou qualité) vs exécution manuelle. Seuil configurable par département, calibré sur les premières exécutions réelles
- **Scalabilité validée** : le 2ème département (Pôle Design/UX) est opérationnel avec les mêmes patterns que le Pôle Dev, validant l'universalité des 8 archétypes
- **Capitalisation** : la mémoire institutionnelle est interrogeable et fiable — test mensuel : 10 questions factuelles sur les décisions passées, ≥ 8/10 réponses correctes
- **Vision 12 mois** : insights cross-domain (neuroscience × UX, behavioral economics × SEO) avec ≥ 15% d'insights supérieurs à l'expert solo. ≥ 30% à 18 mois

### Succès Technique

- **Temps d'exécution** : workflow standard < 10 min
- **Taux de retry** : < 15% par agent, avec tendance décroissante sur 30j glissants pendant 3 mois consécutifs (learning rate)
- **Retry-mémoire** : < 5% des retries sur un pattern déjà rencontré (capitalisation via M4)
- **Recall mémoire** : > 90% — benchmark mensuel : 20 requêtes types, mesure du % de chunks pertinents dans le top-5
- **Tâches complexes** : ≥ 50% des tâches impliquant 3+ agents produisent un livrable grade A ou B
- **Diversité des contrôles** : les Contrôleurs utilisent des modèles/paramètres différents des Producteurs
- **Pipeline cross-domain** : à 6 mois, RAG scientifique couvre ≥ 3 domaines avec ≥ 100 docs indexés/domaine, Cross-Pollinator produit ≥ 1 insight/semaine en mode autonome

### Résultats Mesurables

| Métrique | Seuil | Horizon | Méthode de mesure |
|---|---|---|---|
| Tâches/semaine (dont non-triviales) | ≥ 10 (≥ 3 complexes) | MVP | Compteur M6 Dashboard |
| Approbation sans vérification | ≥ 70% | 6 mois | Ratio approve/review dans M7 |
| Livrables grade A/B (3+ agents) | ≥ 50% | MVP | Scoring M8 |
| Retry par agent | < 15%, tendance ↓ | MVP | Monitoring M3 |
| Retry sur pattern connu | < 5% | 3 mois | Corrélation M3 × M4 |
| Recall mémoire (top-5) | > 90% | MVP | Benchmark mensuel M4 |
| Mémoire institutionnelle | ≥ 8/10 questions | Mensuel | Test factuel M4 |
| Workflow standard | < 10 min | MVP | Timer M3 |
| RAG cross-domain | ≥ 3 domaines, 100 docs | 6 mois | Index M4 |
| Insights cross-domain | ≥ 15% > expert | 12 mois | Évaluation John |

## Périmètre Produit

### MVP — Sprints 0 à 3

**Infrastructure (Sprint 0) :**
- PostgreSQL/pgvector + FastAPI + React + Docker Compose
- M3 Workflow Engine (abstraction LangGraph)
- M4 Memory Manager (namespaces, TTL, reranking)
- Bus d'événements inter-modules

**Département Dev complet (Sprints 1-2) :**
- 9 agents : Dev Lead, Code Researcher, Architect Analyst, Code Producer, Code Reviewer, Test Engineer, CI/CD Watcher, Doc Writer, Sprint Reporter
- M2 Agent Registry (templates + instances)
- M5 Tool Hub + MCP
- M8 Agent Configurator (prompts, contrats, modèles LLM)

**Cockpit UI (Sprint 3) :**
- M6 Dashboard (métriques, alertes, statuts)
- M7 Chat Interface (interaction directe, human-in-the-loop)
- M12 Trace Explorer (debug, chaîne de traçabilité)
- M11 Scheduler
- Agent Playground (test d'agents en isolation)

**Critère de validation MVP :** John utilise quotidiennement le Pôle Dev pour de vraies tâches de son agence.

### Growth — Sprints 4 à 5

- **Sprint 4 — 2ème département : Pôle Design/UX** (6 agents : Design Lead, Trend Watcher, Moodboard Curator, UX Architect, UI Producer, Design Reviewer) — test de validation critique de l'universalité des archétypes
- **Sprint 5 — Company Architect (M1)** : processus guidé en 7 étapes pour créer de nouveaux départements
- M9 Topology View (visualisation graphe agents/workflows)
- M10 Reporting Engine
- Recrutement dynamique (scatter-gather)
- Tool Scout (hybride LLM + audit déterministe)
- Event Hooks (règles configurables sur le bus d'événements inter-départements)
- Agent Debugger (agent actif qui analyse les traces et propose des diagnostics automatiques)

### Vision — Sprint 6+

- Cross-Pollinator : synthèse cross-domain via RAG scientifique
- Agent Évolutionnaire : auto-amélioration des prompts par A/B testing
- Recettes exportables (templates de départements)
- Expansion multi-départements : Juridique, Fiscal, SEO
- Marketplace de templates communautaire (long terme)

## User Journeys

### Journey 1 — John, le Chef d'orchestre proactif (Happy Path)

**Opening Scene :** Lundi matin, 8h30. John ouvre Agentive avec son café. Le Dashboard l'accueille avec un résumé généré par le Sprint Reporter : 14 tâches complétées ce week-end, 2 en attente de validation, 1 alerte de qualité sur un output du Code Producer (score B- au lieu du B minimum). Aucune urgence.

**Rising Action :** John consulte la file d'attente. Il a 3 tâches à lancer pour la semaine : un nouveau module API pour un client, un refactoring identifié par l'Architect Analyst, et la mise à jour des dépendances flaggée par le CI/CD Watcher. Il ouvre le Chat, tape "Lance le scaffolding du module API paiements pour le projet Acme — specs dans le brief client". Le Dev Lead prend le relais, décompose la tâche, assigne l'Architect Analyst pour l'analyse des dépendances, puis le Code Producer démarre. John passe aux validations en attente.

**Climax :** En 45 minutes, le module API est scaffoldé, testé, documenté. John consulte le Trace Explorer pour vérifier la chaîne de décisions de l'Architect Analyst — les choix d'architecture sont cohérents, les patterns suivent les conventions du projet. Il approuve sans modifier. Il réalise qu'il vient de déléguer ce qui lui aurait pris une demi-journée, et il a confiance dans le résultat.

**Resolution :** À 10h, les 3 tâches de la semaine sont lancées ou en cours. John passe à ses réunions clients. En fin de journée, le Dashboard montre 2 tâches terminées, 1 en cours. Il valide les outputs depuis son téléphone via le Chat. La semaine est déjà productive.

**Capabilities révélées :** Dashboard avec résumé intelligent, Chat comme interface principale, Trace Explorer pour l'audit, workflow multi-agents autonome, validation asynchrone.

### Journey 2 — John, le Diagnosticien (Edge Case / Erreur)

**Opening Scene :** Mercredi, 14h. John reçoit une notification : le Code Reviewer a rejeté un output du Code Producer pour la 3ème fois sur le même module. Le taux de retry a explosé à 40%. Quelque chose ne va pas.

**Rising Action :** John ouvre le Trace Explorer depuis l'alerte. Il remonte la chaîne : le Code Producer génère du code qui utilise un pattern deprecated — il s'appuie sur un chunk de mémoire obsolète datant de 3 semaines. Le Code Reviewer, sur un modèle différent, détecte correctement le problème mais ne peut pas le résoudre. John identifie la cause racine en 2 clics : un chunk mémoire avec un TTL trop long dans le namespace "conventions-projet".

**Climax :** Depuis le Trace Explorer, John clique sur "Ouvrir dans Config". Il passe en mode expert, ajuste le TTL du namespace, purge le chunk obsolète, et relance le workflow depuis le Chat. Cette fois, le Code Producer utilise les conventions à jour. Le Code Reviewer valide au premier passage.

**Resolution :** John ajoute une règle dans M8 : alerte automatique quand le retry d'un agent dépasse 25% sur 24h. Il note que le système a besoin d'un mécanisme de détection de mémoire stale — une idée pour le backlog. Le taux de retry redescend à 8% en fin de journée.

**Capabilities révélées :** Notifications proactives, Trace Explorer avec navigation contextuelle, mode expert Config, purge mémoire, relance workflow, configuration de seuils d'alerte, lien direct Trace → Config → Chat.

### Journey 3 — John, l'Architecte de département (Configuration)

**Opening Scene :** Sprint 4. Le Pôle Dev tourne depuis 2 mois. John est confiant. Il lance la création du Pôle Design/UX — son 2ème département. Il ouvre le Company Architect (M1) et sélectionne "Créer un département".

**Rising Action :** Le wizard guidé démarre la phase Discovery : "Décrivez le métier cible". John décrit le workflow Design/UX de son agence : veille tendances → moodboards → wireframes → UI → review. Le Company Architect mappe automatiquement les rôles sur les archétypes : Veilleur+Chercheur pour le Trend Watcher, Producteur pour l'UI Producer, Contrôleur pour le Design Reviewer. John valide le mapping, ajuste le Moodboard Curator de "Producteur" à "Analyste" — il agrège et analyse plus qu'il ne produit.

**Climax :** Phase Tool Binding. Le Tool Scout recherche les outils nécessaires : accès API Dribbble, Behance, Pinterest pour la veille ; intégration Figma pour les maquettes. Il trouve un serveur MCP Figma existant, évalue sa couverture (85%), et signale qu'il manque un outil pour le moodboard automatique — il rédige la spec de l'outil manquant. John valide les outils existants, met la spec en backlog.

**Resolution :** En une session de 2h, le département Design/UX est configuré : 6 agents, leurs prompts, leurs contrats, leurs namespaces mémoire, leurs workflows. John lance un workflow test. Le Design Lead orchestre une veille tendances → le Trend Watcher ramène 15 sources pertinentes → le Moodboard Curator les classe par pertinence. Le pattern marche. Les archétypes sont universels.

**Capabilities révélées :** Company Architect wizard 7 étapes, mapping archétypes, Tool Scout avec recherche + spécification, configuration de namespaces mémoire par département, workflow test, validation d'universalité.

### Journey 4 — Sophie, Collaboratrice agence (Consultation MVP/V1)

**Opening Scene :** Sophie est chef de projet chez John. Elle gère le client Acme. Ce matin, le client demande une mise à jour sur l'avancement du module API. Sophie n'a pas accès aux détails techniques, mais John lui a donné un accès Agentive en lecture.

**Rising Action :** Sophie ouvre le Dashboard avec son compte (permissions : lecture seule, département Dev). Elle voit le statut du projet Acme : 3 tâches terminées, 1 en cours, 1 en attente de validation par John. Elle consulte les outputs : la documentation API générée par le Doc Writer, le rapport de tests du Test Engineer. Elle ne peut pas modifier ni relancer de workflows — elle consulte.

**Climax :** Sophie télécharge le rapport de tests et la documentation API pour les envoyer au client. Les documents sont propres, structurés, prêts à partager. Elle n'a pas eu besoin de déranger John pour obtenir l'information.

**Resolution :** Le client est impressionné par la rapidité et la qualité de la documentation. Sophie utilise Agentive comme sa source de vérité projet. Elle demande à John d'avoir aussi accès au Chat en lecture pour suivre les conversations avec les agents.

**Capabilities révélées :** Multi-utilisateurs avec permissions par rôle, accès en lecture par département, export d'outputs, Dashboard filtré par projet/département.

### Journey 5 — Marc, Freelance externe (Accès limité V1/V2)

**Opening Scene :** Marc est un développeur freelance engagé par l'agence de John pour 3 mois. John lui donne un accès Agentive limité : il peut interagir avec le Code Producer et le Code Reviewer via le Chat, mais uniquement sur le projet qui lui est assigné.

**Rising Action :** Marc ouvre le Chat. Il voit uniquement le projet "Refonte Dashboard Client XYZ". Il demande au Code Producer de scaffolder un composant React. L'agent lui répond, produit le code, et le Code Reviewer le valide. Marc itère directement avec les agents — c'est plus rapide que de coder seul, les conventions du projet sont automatiquement respectées.

**Climax :** Marc tente d'accéder au Dashboard global — accès refusé. Il essaie de consulter la mémoire d'un autre projet — namespace bloqué. L'isolation fonctionne. Il peut travailler efficacement sur son périmètre sans risquer de voir ou modifier quoi que ce soit en dehors.

**Resolution :** Marc livre en avance. Il a utilisé les agents comme des assistants, pas comme des remplaçants. John consulte le Trace Explorer pour auditer les interactions de Marc — tout est tracé. À la fin de la mission, l'accès de Marc est révoqué, mais la mémoire des conventions et patterns qu'il a co-créés reste capitalisée dans M4.

**Capabilities révélées :** Permissions granulaires (par projet, par agent, par action), isolation namespace, audit trail des utilisateurs externes, révocation d'accès, capitalisation mémoire post-mission.

### Synthèse des Capabilities par Journey

| Capability | J1 | J2 | J3 | J4 | J5 |
|---|---|---|---|---|---|
| Dashboard intelligent | ✓ | | | ✓ | |
| Chat comme interface principale | ✓ | ✓ | | | ✓ |
| Trace Explorer (debug + audit) | ✓ | ✓ | | | ✓ |
| Workflow multi-agents autonome | ✓ | | ✓ | | ✓ |
| Notifications/alertes proactives | | ✓ | | | |
| Navigation contextuelle (Trace→Config→Chat) | | ✓ | | | |
| Mode wizard + mode expert | | ✓ | ✓ | | |
| Company Architect (M1) | | | ✓ | | |
| Tool Scout | | | ✓ | | |
| Multi-utilisateurs + permissions | | | | ✓ | ✓ |
| Isolation namespace par projet | | | | | ✓ |
| Export d'outputs | | | | ✓ | |
| Audit trail utilisateurs | | | | | ✓ |

## Exigences Domaine

### Compliance & Sécurité

- **Isolation des données** : chaque département a son namespace mémoire isolé. Les utilisateurs externes (freelances, collaborateurs) ne doivent jamais accéder aux données hors de leur périmètre
- **Audit trail** : toute action (agent ou humain) est tracée — obligation pour la confiance et le debug
- **Chiffrement at-rest** : données sensibles (clés API LLM, credentials outils MCP, données clients)
- **Gestion des clés API** : rotation, stockage sécurisé, pas de hardcoding

### Contraintes Techniques

- **Coût LLM** : chaque appel a un coût. Le Dry Run mode est obligatoire pour estimer avant d'exécuter. Budget caps par département/workflow
- **Rate limiting** : les providers LLM ont des limites. Le système doit gérer les queues, retries avec backoff, et la répartition entre modèles
- **Latence variable** : les LLMs ont des temps de réponse imprévisibles. Les workflows doivent être asynchrones par design
- **Taille de contexte** : la saturation du contexte LLM est le trigger du recrutement dynamique — contrainte domaine fondamentale

### Patterns Domaine

- **Prompt engineering** : les prompts sont du code — versioning, testing, rollback nécessaires
- **Évaluation non-déterministe** : les outputs LLM varient. Les quality gates doivent être tolérants aux variations acceptables
- **Hallucination** : le RAG réduit mais n'élimine pas le risque. Les Contrôleurs sont la couche de défense
- **Vendor lock-in LLM** : abstraction obligatoire (M3) pour changer de provider sans refactoring

### Risques & Mitigations

| Risque | Impact | Mitigation |
|---|---|---|
| Coûts LLM incontrôlés | Budget explosé | Dry Run + budget caps + alertes |
| Hallucination en cascade | Output faux propagé dans le workflow | Contrôleurs sur modèle différent + validation humaine |
| Mémoire polluée | Agents basés sur des données obsolètes/fausses | TTL + scoring temporel + purge manuelle |
| Provider LLM down | Département bloqué | Fallback multi-provider dans M3 |
| Prompt injection via outils | Sécurité compromise | Sandbox exécution + audit Tool Scout |

## Innovation & Novel Patterns

### Innovations MVP (Sprint 0-3) — Fondations

| # | Innovation | Description |
|---|---|---|
| 1 | **Orchestration Hybride** | 70% routage déterministe / 30% jugement LLM. Escalade par score de confiance, apprentissage automatique des règles à partir des décisions LLM |
| 2 | **Mise en Place Automatique** | Pré-vérification active avant chaque workflow : outils disponibles, mémoire chargée, budget estimé, risques identifiés |
| 3 | **Mémoire Proactive (Push)** | M4 injecte proactivement les chunks pertinents (seuil similarité > 0.85, budget 15% tokens, marquage explicite `[CONTEXTE MÉMORISÉ]`, opt-out pour Contrôleurs) |
| 4 | **Review Conversationnelle** | Le Contrôleur retourne des commentaires localisés avec niveaux de sévérité (bloquant/suggestion/question), pas un verdict binaire. Le Producteur itère de façon ciblée |
| 5 | **Contrats Élastiques** | Noyau obligatoire (champs minimum) + zone flexible enrichissable par l'agent émetteur. Le récepteur consomme les extras s'ils sont présents |
| 6 | **Métriques Contextuelles** | Dashboard-in-Trace — métriques en overlay dans le Trace Explorer (temps, coût, score, comparaison historique) |
| 7 | **Dry Run Prédictif** | Simulation complète du workflow : chemins probables, agents sollicités, estimation tokens, risques identifiés (recrutement dynamique, etc.) |
| 8 | **Résumés de Passage** | Chaque agent produit un condensé structuré de son output pour le suivant dans la chaîne, réduisant la consommation de tokens sans perte d'information critique |

### Innovations Growth (Sprint 4-5) — Calibrées sur données réelles

| # | Innovation | Description |
|---|---|---|
| 9 | **Registre d'Anticorps** | Index de patterns d'erreurs connus dans les Contrôleurs. Détection instantanée (coût LLM = 0 pour erreurs connues). Le registre grandit avec chaque erreur nouvelle |
| 10 | **Workflows Adaptatifs** | Supergraphe avec branches activables/désactivables par l'Orchestrateur selon la complexité réelle. Tâches simples = chemin court, tâches critiques = double-review |
| 11 | **Mode Autonomie Graduée** | 3 niveaux (défaut/confiance/vacances). Circuit-breaker : 3 échecs consécutifs → retour niveau 1. Tâches client-facing exclues de l'auto-approbation. Rollback en lot |
| 12 | **Agents Proactifs** | Les agents recommandent des tâches au lieu d'attendre les commandes. Section "Recommandations" dans le Dashboard |
| 13 | **Auto-Graduation** | Les agents gagnent progressivement le droit de bypasser la validation humaine par type de tâche. Graduation sur 50 tâches consécutives grade A/B, validée par John |
| 14 | **Audit Trail comme Dataset** | Analyse périodique des patterns succès/échec pour recommandations d'amélioration de prompts dans M8. Bootstrap de l'Agent Évolutionnaire |

### Innovations Vision (Sprint 6+) — Système mature

| # | Innovation | Description |
|---|---|---|
| 15 | **Peer Rating System** | Les agents notent la qualité de leurs inputs mutuels via micro-prompt dédié. Heatmap relationnelle dans le Dashboard. Relations "froides" = alerte maintenance |
| 16 | **Agent Momentum Tracking** | Score de momentum sur 20 dernières exécutions (fenêtre glissante). Momentum positif → tâches plus ambitieuses. Momentum négatif → réduction charge + diagnostic |
| 17 | **Indice de Santé Départementale** | Indice de Shannon H = -Σ(pi × ln(pi)). H élevé = charge bien répartie. H bas = concentration = risque de point de défaillance unique |
| 18 | **Agent Polyvalence Contrôlée** | Archétype primaire + secondaires activables sous conditions (confiance > seuil, complexité < seuil). Réduit le ping-pong inter-agents pour tâches simples |
| 19 | **Prompt Portability Testing** | Test automatique de portabilité des prompts sur 2-3 providers lors de la configuration. Identification des prompts "fragiles" dépendants du provider |
| 20 | **Cross-Pollinator** | Synthèse cross-domain via RAG scientifique multi-domaines (neuroscience × UX, behavioral economics × SEO) |
| 21 | **Agent Évolutionnaire** | Auto-amélioration des prompts par A/B testing, bootstrappé par les données de l'audit trail |

### Garde-fous Innovation

- **Push Memory** : seuil similarité > 0.85, budget tokens 15%, marquage explicite, opt-out Contrôleurs
- **Orchestration Hybride** : escalade automatique si confiance < seuil, apprentissage des règles validé par John
- **Autonomie Graduée** : circuit-breaker (3 échecs → niveau 1), tâches client-facing exclues, rollback en lot
- **Séquençage strict** : 8 innovations MVP, 6 Growth, 7 Vision — chaque horizon nécessite les données du précédent

## Exigences Web App

### Architecture Frontend

| Aspect | Décision |
|---|---|
| **Type** | SPA (Single Page Application) |
| **Framework** | React |
| **Espaces UI** | 4 espaces MVP : Dashboard, Config, Chat, Trace (M12 Trace Explorer). Topology View (M9) ajoutée en Growth comme 5ème espace. |
| **Mode UI** | Wizard guidé (principal) + Mode expert (avancé) |
| **Routing** | Client-side routing, 4 routes principales |
| **Streaming** | SSE (Server-Sent Events) pour les réponses agents en temps réel |

### Browser Support

| Navigateur | Priorité | Support |
|---|---|---|
| Chrome (dernières 2 versions) | P0 — développement principal | Complet |
| Firefox (dernières 2 versions) | P1 — testé avant chaque release | Complet |
| Safari (dernières 2 versions) | P1 — testé avant chaque release | Complet |
| Edge | P2 — basé sur Chromium, support implicite | Non testé explicitement |
| Mobile browsers | P2 — responsive mais pas optimisé | Layout adaptatif |

### Responsive Design

- **Desktop-first** — Agentive est un outil de travail, utilisé principalement sur desktop
- **Responsive adaptatif** — les 4 espaces s'adaptent aux écrans tablette/mobile pour consultation
- **Chat Mobile** — le Chat (M7) doit être pleinement fonctionnel sur mobile (validation en déplacement)
- **Dashboard Mobile** — consultation des métriques et statuts, pas d'édition
- **Config & Trace** — desktop uniquement recommandé, layout simplifié sur mobile (Topology M9 également desktop-only quand livrée en Growth)

### Performance Targets

| Métrique | Cible | Justification |
|---|---|---|
| First Contentful Paint | < 1.5s | App interne, réseau local |
| Time to Interactive | < 3s | Réactivité immédiate attendue |
| SSE latency | < 200ms premier token | Le streaming doit paraître instantané |
| Dashboard refresh | < 2s | Les métriques doivent être fraîches |
| Bundle size | < 500KB gzipped | Self-hosted, réseau local |

### Accessibilité

- **Niveau WCAG 2.1 AA minimum**
- Contraste suffisant sur tous les composants
- Navigation clavier complète dans les 4 espaces
- Labels ARIA sur les éléments interactifs
- Support lecteur d'écran pour le Dashboard et le Chat
- Focus visible sur tous les éléments interactifs

## Scoping & Développement Phasé

### Stratégie MVP

**Philosophie :** MVP de viabilité technique — prouver que l'orchestration multi-agents avec mémoire partagée et quality gates fonctionne pour de vraies tâches de développement. Pas de validation marché (John est le marché).

**Ressources :**
- 1 développeur (John) + Claude Code comme accélérateur
- Sprints de 2 semaines
- Vélocité estimée : ~1 epic/jour avec Claude Code
- Dogfooding dès que le squelette M3+M4 fonctionne

**Critère de succès MVP :** John utilise quotidiennement le Pôle Dev pour de vraies tâches de son agence.

### MVP Feature Set (Phase 1 — Sprints 0-3)

**Journeys supportés :** J1 (Chef d'orchestre), J2 (Diagnosticien)

**Must-Have :**

| Module | Fonctionnalité | Justification |
|---|---|---|
| M3 Workflow Engine | Abstraction LangGraph, orchestration hybride (déterministe + LLM), checkpointing | Pièce centrale — sans M3, rien ne fonctionne |
| M4 Memory Manager | Namespaces, TTL, reranking, Push Memory | Les agents sans mémoire = chatbots stateless |
| M2 Agent Registry | Templates + instances, 9 agents Pôle Dev | Pas d'agents = pas de département |
| M5 Tool Hub | Intégration MCP, outils dev (git, filesystem, terminal) | Les agents sans outils = cerveaux sans mains |
| M8 Agent Configurator | Prompts, contrats élastiques, modèles LLM, mode wizard + expert | Configuration et itération des agents |
| M7 Chat Interface | Interaction directe, human-in-the-loop, SSE streaming | Interface principale d'interaction |
| M6 Dashboard | Métriques, alertes, statuts, résumé Sprint Reporter | Cockpit de pilotage |
| M12 Trace Explorer | Debug, chaîne de traçabilité, métriques contextuelles | Indispensable pour le diagnostic (J2) |
| M11 Scheduler | Planification de workflows récurrents | CI/CD Watcher, veille automatique |
| Agent Playground | Test d'agents en isolation (entrée manuelle, exécution, inspection) | Essentiel pour itérer sur les prompts sans lancer un workflow complet |
| Infra | PostgreSQL/pgvector, FastAPI, React, Docker Compose, bus événements | Fondation technique |

**Innovations MVP intégrées :** Orchestration Hybride, Mise en Place, Push Memory, Review Conversationnelle, Contrats Élastiques, Métriques Contextuelles, Dry Run Prédictif, Résumés de Passage

**Ce qui peut être manuel au MVP :**
- Gestion des permissions → John seul utilisateur, pas besoin de RBAC
- Reporting → John consulte le Dashboard, pas besoin de M10
- Topologie → John connaît ses agents, pas besoin de visualisation graphe

### Phase 2 — Growth (Sprints 4-5)

**Journeys ajoutés :** J3 (Architecte), J4 (Sophie — collaboratrice)

| Fonctionnalité | Justification |
|---|---|
| Pôle Design/UX (6 agents) | Validation universalité des archétypes |
| Company Architect (M1) | Processus guidé de création de départements |
| Multi-utilisateurs + permissions basiques | Sophie (lecture), accès par département |
| M9 Topology View | Nécessaire quand 2+ départements coexistent |
| M10 Reporting Engine | Rapports automatisés pour Sophie/clients |
| Tool Scout | Recherche d'outils pour le 2ème département |
| Recrutement dynamique | Calibré sur les données d'usage du MVP |
| Event Hooks | Règles configurables sur le bus d'événements inter-départements |
| Agent Debugger | Agent actif qui analyse les traces et propose des diagnostics automatiques |

**Innovations Growth :** Registre d'Anticorps, Workflows Adaptatifs, Autonomie Graduée, Agents Proactifs, Auto-Graduation, Audit Trail comme Dataset

### Phase 3 — Vision (Sprint 6+)

**Journey ajouté :** J5 (Marc — freelance)

| Fonctionnalité | Justification |
|---|---|
| Permissions granulaires (projet/agent/action) | Accès freelances, isolation namespace |
| Cross-Pollinator | RAG scientifique multi-domaines |
| Agent Évolutionnaire | A/B testing des prompts |
| Recettes exportables | Templates de départements réutilisables |
| Expansion départements | Juridique, Fiscal, SEO |

### Risques & Mitigations

**Risque technique #1 (CRITIQUE) — M3 Workflow Engine :**
- **Risque :** L'abstraction LangGraph est mal calibrée → impossible de supporter checkpointing, scatter-gather, et human-in-the-loop sans couplage fort
- **Mitigation :** Sprint 0 = spike technique dédié à M3. Implémenter un workflow minimal (2 agents, 1 quality gate) de bout en bout avant de construire quoi que ce soit d'autre. Si le spike échoue, pivoter l'abstraction avant d'investir davantage
- **Fallback :** Si LangGraph ne convient pas, l'abstraction M3 permet de swapper vers une solution custom ou un autre framework sans refactoring des agents

**Risque technique #2 — Qualité des prompts :**
- **Risque :** Les prompts des 9 agents Dev ne produisent pas des outputs de qualité suffisante
- **Mitigation :** Itération rapide via M8 + Review Conversationnelle. Le Pôle Dev est choisi précisément parce que John peut évaluer la qualité avec un œil critique

**Risque ressource — Développeur solo :**
- **Risque :** Burnout, perte de focus, scope creep
- **Mitigation :** Sprints courts (2 semaines), epic/jour avec Claude Code, dogfooding précoce (motivation par l'usage réel). Le scope MVP est strict — 10 modules, pas 12

**Contingence — MVP minimum absolu :**
Si les ressources sont plus limitées que prévu, le MVP minimum absolu = M3 + M4 + M2 + M7 (Chat) + 3 agents (Dev Lead, Code Producer, Code Reviewer). Pas de Dashboard, pas de Trace Explorer. John interagit uniquement via le Chat. C'est moins confortable mais ça prouve la viabilité.

## Functional Requirements

### 1. Orchestration & Workflows

- **FR1:** John peut créer un workflow en définissant les agents participants, l'ordre d'exécution, et les conditions de branchement
- **FR2:** Le système peut exécuter un workflow multi-agents de bout en bout avec checkpointing (reprise en cas d'interruption)
- **FR3:** L'Orchestrateur peut router automatiquement les tâches simples via des règles déterministes sans appel LLM
- **FR4:** L'Orchestrateur peut escalader une décision de routage vers le LLM quand la confiance du routage déterministe est insuffisante
- **FR5:** Le système peut exécuter une Mise en Place automatique avant chaque workflow (vérification outils, mémoire, budget, risques)
- **FR6:** Le système peut estimer le coût, le temps, et le chemin d'exécution probable d'un workflow avant lancement (Dry Run Prédictif)
- **FR7:** John peut interrompre, relancer, ou annuler un workflow en cours d'exécution
- **FR8:** Le système peut planifier l'exécution récurrente de workflows via le Scheduler (M11)

### 2. Agents & Configuration

- **FR9:** John peut créer un agent à partir d'un template d'archétype (Orchestrateur, Chercheur, Analyste, Producteur, Stratège, Contrôleur, Veilleur, Communicateur)
- **FR10:** John peut configurer l'identité, le system prompt, les contrats input/output, le modèle LLM, et la politique d'erreur d'un agent
- **FR11:** John peut configurer un agent via un mode wizard guidé ou un mode expert direct
- **FR12:** Le système peut distinguer agent-template (définition) et agent-instance (exécution en cours)
- **FR13:** Les agents peuvent communiquer via des contrats élastiques (noyau obligatoire + zone flexible enrichissable)
- **FR14:** Le Contrôleur peut retourner une review conversationnelle avec commentaires localisés et niveaux de sévérité
- **FR15:** Le Contrôleur peut utiliser un modèle LLM ou des paramètres différents de l'agent qu'il contrôle

### 3. Mémoire & Connaissances

- **FR16:** Le système peut stocker et retrouver des chunks mémoire via recherche vectorielle (pgvector)
- **FR17:** John peut définir des namespaces mémoire isolés par département et par projet
- **FR18:** Le système peut appliquer un TTL par chunk et archiver automatiquement les données expirées
- **FR19:** Le système peut scorer la pertinence des chunks en tenant compte de la décroissance temporelle
- **FR20:** Le système peut injecter proactivement les chunks pertinents dans le contexte d'un agent (Push Memory) avec marquage explicite
- **FR21:** John peut purger manuellement des chunks mémoire spécifiques

### 4. Outils & Intégrations

- **FR22:** Le système peut connecter des outils MCP aux agents via le Tool Hub (M5)
- **FR23:** John peut assigner des outils spécifiques à chaque agent
- **FR24:** Le système peut exécuter les outils dans un environnement sandboxé

### 5. Interface Utilisateur & Interaction

- **FR25:** John peut interagir avec les agents via une interface Chat avec streaming SSE
- **FR26:** John peut valider, rejeter, ou modifier un output d'agent dans le Chat (human-in-the-loop)
- **FR27:** John peut consulter un Dashboard avec métriques, alertes, statuts par département et par projet
- **FR28:** John peut consulter le résumé automatique généré par le Sprint Reporter dans le Dashboard
- **FR29:** John peut naviguer du Trace Explorer vers la Config d'un agent ou vers le Chat en un clic (navigation contextuelle)
- **FR30:** John peut tracer l'exécution complète d'un workflow dans le Trace Explorer (sources → données → raisonnement → décision)
- **FR31:** John peut voir les métriques en overlay sur chaque nœud du Trace Explorer (métriques contextuelles)
- **FR32:** Le système peut envoyer des notifications proactives quand un seuil d'alerte est dépassé
- **FR33:** John peut configurer les seuils d'alerte par agent et par métrique

### 6. Sécurité & Audit

- **FR34:** Le système peut tracer toute action (agent ou humain) dans un audit trail persistant
- **FR35:** Le système peut chiffrer les données sensibles at-rest (clés API, credentials, données clients)
- **FR36:** John peut gérer les clés API des providers LLM (ajout, rotation, suppression)
- **FR37:** Le système peut appliquer des budget caps par département et par workflow
- **FR38:** Le système peut gérer les rate limits des providers LLM (queues, retries avec backoff, répartition)

### 7. Monitoring & Qualité

- **FR39:** Le système peut mesurer et afficher le score qualité de chaque output d'agent
- **FR40:** Le système peut mesurer le taux de retry par agent et sa tendance temporelle
- **FR41:** Le système peut identifier les retries sur des patterns déjà rencontrés (corrélation retry × mémoire)
- **FR42:** Le système peut exécuter un benchmark mensuel de recall mémoire (20 requêtes, % chunks pertinents dans top-5)

### 8. Département Dev — Pôle Pilote

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

## Non-Functional Requirements

### Performance

| NFR | Seuil | Contexte |
|---|---|---|
| NFR1: Temps de réponse API backend | < 500ms (p95) pour les endpoints non-LLM | Les actions UI (navigation, config) doivent être instantanées |
| NFR2: Latence premier token SSE | < 200ms après envoi de la requête au provider | Le streaming doit paraître immédiat |
| NFR3: Temps d'exécution workflow standard | < 10 min bout en bout | Au-delà, l'avantage de vitesse disparaît |
| NFR4: Temps de boot Docker Compose | < 60s pour l'ensemble du stack | Relance rapide en développement et debug |
| NFR5: Recherche vectorielle M4 | < 200ms pour une requête avec reranking sur 10k chunks | La Push Memory ne doit pas ralentir le démarrage d'un workflow |

### Sécurité

| NFR | Seuil | Contexte |
|---|---|---|
| NFR6: Chiffrement at-rest | AES-256 pour clés API, credentials, données clients | Standard minimal |
| NFR7: Isolation namespace | Aucun cross-read possible entre namespaces sans autorisation explicite | Critique pour multi-utilisateurs (Growth) |
| NFR8: Audit trail | 100% des actions tracées, rétention ≥ 90 jours | Aucune action invisible — base de confiance |
| NFR9: Gestion des secrets | Aucune clé API en clair dans le code, les logs, ou les traces | Vault ou variables d'environnement chiffrées |
| NFR10: Sandbox outils MCP | Exécution isolée, aucun accès réseau non-autorisé, timeout configurable | Les outils ne doivent pas compromettre le système hôte |

### Fiabilité

| NFR | Seuil | Contexte |
|---|---|---|
| NFR11: Reprise sur interruption | Tout workflow interrompu peut reprendre à partir du dernier checkpoint | Pas de perte de travail en cas de crash |
| NFR12: Graceful degradation LLM | Si un provider est down, fallback automatique sur un provider alternatif dans M3 | Aucun blocage département |
| NFR13: Persistance données | Aucune perte de données en cas de redémarrage Docker | Volumes persistants PostgreSQL |
| NFR14: Gestion des timeouts | Timeout configurable par agent et par outil, avec retry automatique et backoff exponentiel | Les LLMs peuvent être lents — le système ne doit pas bloquer |

### Observabilité

| NFR | Seuil | Contexte |
|---|---|---|
| NFR15: Traçabilité bout en bout | Chaque output traçable jusqu'à ses sources (prompt, mémoire, outils, inputs) | Le Trace Explorer en dépend |
| NFR16: Logs structurés | Tous les logs au format JSON structuré avec correlation ID par workflow | Debug multi-agents = besoin de corrélation |
| NFR17: Métriques temps réel | Dashboard refresh < 2s, métriques à jour à ≤ 30s de latence | Cockpit de pilotage fiable |
| NFR18: Alertes | Notification < 60s après détection d'un seuil dépassé | Réactivité pour le Journey Diagnosticien |

### Intégration

| NFR | Seuil | Contexte |
|---|---|---|
| NFR19: Protocole MCP | Compatibilité avec les serveurs MCP standard (stdio et SSE) | Écosystème d'outils |
| NFR20: Multi-provider LLM | Support simultané de ≥ 2 providers LLM (Anthropic, OpenAI minimum) | Résilience + diversité des contrôles |
| NFR21: API interne | Tous les modules communiquent via des APIs REST internes documentées | Découplage inter-modules |
