# Présentation d’Agentive en entretien

## Pitch court — 30 secondes

Agentive est une plateforme web qui permet de créer et de piloter des équipes d’agents IA spécialisés, organisées comme de véritables départements. L’idée est de dépasser le simple chatbot : chaque agent possède un rôle précis, des outils, une mémoire et des règles de collaboration. Ensemble, ils peuvent prendre en charge un processus métier complet, tout en laissant à l’utilisateur le contrôle, la validation et la visibilité sur ce qui a été fait. Le premier cas d’usage visé est un pôle de développement logiciel, avant d’étendre le modèle à d’autres métiers comme le design, le SEO, le juridique ou la fiscalité.

## Présentation — 1 à 2 minutes

Agentive part d’un constat simple : les solutions actuelles permettent de créer des agents IA, mais elles donnent rarement les moyens de les organiser en une équipe cohérente, fiable et pilotable.

Le projet propose donc un **« Company Builder » pour agents IA**. L’utilisateur peut créer des agents à partir de grands profils — par exemple orchestrateur, chercheur, analyste, producteur ou contrôleur — puis définir leur mission, le modèle d’IA qu’ils utilisent, les outils auxquels ils ont accès et les informations qu’ils peuvent mémoriser. L’objectif est de composer progressivement un département numérique capable d’exécuter un processus de bout en bout.

Dans le cas du pôle Dev, un agent peut analyser une demande, un autre rechercher l’information utile, un autre produire du code et un contrôleur vérifier le résultat avec un regard différent. L’utilisateur reste dans la boucle pour approuver les décisions sensibles. La plateforme doit également conserver la mémoire des projets et des décisions passées, mesurer la qualité et le coût des exécutions, et permettre de comprendre comment un résultat a été obtenu.

L’ambition à terme est de rendre ce modèle réutilisable dans plusieurs métiers. Chaque département garde ses agents, ses workflows et sa mémoire, tout en pouvant partager certaines connaissances avec les autres. Agentive devient ainsi une couche d’organisation et de gouvernance au-dessus des modèles d’IA : elle transforme plusieurs intelligences spécialisées en un système opérationnel commun.

## À quoi sert concrètement le produit ?

Agentive vise à aider une agence, une petite entreprise ou un indépendant à :

- automatiser des tâches complexes qui nécessitent plusieurs compétences ;
- transformer des processus récurrents en workflows d’agents spécialisés ;
- capitaliser sur l’historique, les documents et les décisions grâce à une mémoire partagée ;
- connecter les agents à des outils réels, comme un dépôt de code, un terminal ou des services externes ;
- conserver un contrôle humain sur les validations importantes ;
- suivre la qualité, le coût et l’origine des résultats produits.

La valeur recherchée ne se limite donc pas à générer du contenu plus vite. Elle consiste à **déléguer un processus complet de manière contrôlée, traçable et améliorable dans le temps**.

## La partie technique, expliquée simplement

Agentive est une application web auto-hébergeable. L’interface est développée avec React et TypeScript, le backend avec Python et FastAPI, et les données sont stockées dans PostgreSQL.

La plateforme s’appuie sur quatre briques principales :

- **un registre d’agents**, pour créer des modèles d’agents, les configurer et en produire des instances figées pour une mission ;
- **un moteur d’orchestration**, basé sur LangGraph, pour enchaîner les agents, reprendre une exécution interrompue et intégrer des validations humaines ;
- **une mémoire vectorielle**, basée sur PostgreSQL et pgvector, pour retrouver les informations pertinentes et gérer leur durée de vie ;
- **un hub d’outils compatible MCP**, pour donner aux agents des capacités d’action tout en encadrant leur exécution.

L’architecture est pensée pour rester indépendante d’un fournisseur d’IA particulier : elle peut utiliser plusieurs modèles, notamment ceux d’OpenAI et d’Anthropic, avec un mécanisme de repli. Elle met aussi l’accent sur la sécurité et l’exploitation : chiffrement des données sensibles, isolation des espaces mémoire, journalisation, métriques, exécution des outils en environnement limité et déploiement reproductible avec Docker.

## État actuel du projet

Le projet est encore en construction. Les fondations techniques sont en place, ainsi que :

- la création et la configuration d’agents à partir de huit archétypes ;
- la distinction entre un modèle d’agent et une instance utilisée lors d’une mission ;
- la connexion et l’assignation d’outils via MCP ;
- un Playground pour tester un agent isolément et revoir son comportement ;
- le stockage et la recherche de mémoire, son isolation par espace, son expiration et la prise en compte de sa pertinence dans le temps ;
- les bases de sécurité, d’observabilité et de déploiement self-hosted.

La prochaine grande étape est de relier ces briques dans de vrais workflows multi-agents, puis de construire le pôle Dev complet et son cockpit : Chat, Dashboard et exploration des traces.

## Formulation personnelle possible

> « Avec Agentive, j’ai voulu explorer comment passer d’un agent IA isolé à une véritable organisation d’agents. Le projet permet de définir des rôles spécialisés, de leur attribuer des outils et une mémoire, puis de les faire collaborer dans des processus contrôlés. Le premier objectif est de créer un pôle Dev capable de traiter des tâches réelles, avec validation humaine et traçabilité. Techniquement, c’est une application React et FastAPI, auto-hébergeable, qui combine orchestration, mémoire vectorielle et intégration d’outils. À terme, l’ambition est de pouvoir reproduire ce modèle pour différents métiers et de construire des départements IA configurables. »

## Idée centrale à retenir

**Agentive ne cherche pas seulement à créer de meilleurs chatbots ; le projet cherche à organiser des agents IA spécialisés en équipes capables de réaliser, mémoriser et expliquer un travail complet.**
