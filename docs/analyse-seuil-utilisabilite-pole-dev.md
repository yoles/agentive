---
date: '2026-09-08'
author: Bob (SM)
type: analyse
status: informatif (ne remplace pas epics.md ni sprint-status.yaml)
---

# À partir de quand le Pôle Dev IA devient utilisable pour du vrai code ?

## Réponse

**Epic 5 (Dev Department)**, mais il est bloqué tant qu'**Epic 4 (Workflow Orchestration Engine)** n'est pas terminé. Aucun des deux n'a démarré au 2026-09-07.

## État des lieux (source : `sprint-status.yaml`, 2026-09-07)

| Epic | Statut | Contenu |
|---|---|---|
| 1 — Foundation | done | Scaffolding, event bus, LLM abstraction, auth |
| 2 — Agent Platform | done | Créer/configurer des agents, Tool Hub, sandbox, Playground (test en isolation) |
| 3 — Memory | in-progress | 5/6 stories done ; 3.5 (Push Memory) en review ; 3.6 (purge/embedding hybride) backlog, non bloquant |
| **4 — Workflow Orchestration Engine** | backlog | Exécution multi-agents bout en bout, checkpointing, orchestration hybride |
| **5 — Dev Department (Pôle Pilote)** | backlog | Les 9 agents Dev réels : Dev Lead, Code Researcher, Architect Analyst, Code Producer, Code Reviewer, Test Engineer, CI/CD Watcher, Doc Writer, Sprint Reporter |

## Pourquoi Epic 5 est le seuil, pas avant

- Epic 2 (fait) permet de créer et tester un agent individuellement dans le Playground, mais pas de faire collaborer plusieurs agents sur une vraie tâche.
- Epic 4 est le moteur qui fait tourner un workflow multi-agents (checkpointing, routage, reprise sur erreur). Sans lui, il n'y a pas de workflow au sens équipe.
- Epic 5 définit les 9 agents du Pôle Dev. Sa dépendance déclarée dans `epics.md` est explicite : *"Dépendance : Epics 2, 3, 4 (consomme toute la plateforme)"*.
- Le critère MVP de l'epic est écrit noir sur blanc : **"John utilise quotidiennement le Pôle Dev (dogfooding)"**, ce qui correspond exactement au critère d'utilisabilité recherché.

## Nuance pratique

Epic 6 (Chat) n'est pas un prérequis strict. Une fois Epic 4 et Epic 5 terminés, un workflow peut être déclenché via l'API ou le Playground étendu, sans interface conversationnelle. Le Chat rend l'usage confortable mais ne le débloque pas.

## Chemin restant

1. Clore 3.5 (review → done) ; 3.6 optionnelle, non bloquante → `epic-3` done
2. **Epic 4** en entier (7 stories : création workflow, exécution + checkpointing, orchestration hybride, dry run prédictif, mise en place automatique, interruption/relance/fallback LLM, résumés de passage)
3. **Epic 5** en entier (6 stories : les 9 agents Dev)

Vu le rythme observé sur Epics 1 à 3 (create-story → dev-story → code-review adversarial 3 couches, souvent plusieurs passes avant `done`), c'est un travail conséquent, pas une formalité.
