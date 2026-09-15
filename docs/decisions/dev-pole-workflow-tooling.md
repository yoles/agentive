# Outiller l'accès au moteur de workflow pour le Pôle Dev (Story 5.1)

**Status:** Accepted (Sprint 2)
**Date:** 2026-09-14
**Story:** 5.1 — Dev Lead, orchestrateur
**Origine:** question portée à John par la Story 5.1, relevée en `intent_gap` par la revue de code

## Décision

**Le Dev Lead reste sans outil en Sprint 2 (`tools: []`), et aucun serveur MCP
interne n'est écrit pour exposer le moteur de workflow.**

Le *mécanisme* d'assignation déclarative est livré, exercé et bloquant : le
catalogue déclare des noms d'outils, le provisioning les résout, et un nom
introuvable ou ambigu fait échouer le provisioning en nommant l'outil et
l'action à faire. C'est la *liste* qui est vide, pas la plomberie.

## Problème

L'AC1 demande que les outils du Dev Lead « incluent accès workflow engine +
memory namespaces Dev ». Le second est exprimable en configuration
(`push_memory.namespace`, vérifié au lancement par la Mise en Place). Le
premier n'a aucun chemin légal aujourd'hui.

## Options

### A. Serveur MCP **stdio** interne — impossible

`SandboxProfile.unshare_net = True` par défaut (`infra/mcp/sandbox.py`) : le
sous-processus n'a pas de réseau, donc pas de Postgres. Un serveur qui expose
le moteur de workflow sans accès à la base n'expose rien.

### B. Serveur MCP **SSE** interne — possible, et rejetée

Le transport SSE **contourne le sandbox** (décision #3 de la Story 2.6) et
rouvre le défer **D63** (allowlist réseau SSE), ouvert depuis l'Epic 3.

Rejetée parce que le coût n'est pas le travail d'écriture, c'est l'arbitrage
de sécurité : donner à un agent LLM un canal non sandboxé vers le moteur qui
exécute les agents est une décision qui se prend pour elle-même, avec son
propre cadrage et sa propre revue. Une story dont l'objet est de livrer une
capacité produit (« le Dev Lead décompose une demande ») ne doit pas la
prendre en passant, comme effet de bord d'une case à cocher.

### C. Ne rien outiller, livrer le mécanisme — **retenue**

Ce que le Sprint 2 attend réellement du Dev Lead, c'est un **plan de
délégation** : une demande en langage naturel entre, un plan sort, un humain
le lit. Cette boucle ne nécessite aucun outil — le run s'arrête sur le node
Dev Lead et son plan est lu par John en `curl`.

Les Stories 5.2 (filesystem + ripgrep) et 5.5 (GitHub) ne sont **pas
bloquées** : elles exerceront le mécanisme livré ici avec une liste non vide.

> ⚠️ **AMENDÉ PAR LA STORY 5.2 (2026-09-15).** Ce paragraphe affirmait que
> leurs serveurs MCP « sont **tiers**, ils s'enregistrent normalement ».
> C'était une hypothèse, pas un état relevé : l'image backend est
> `python:3.14-slim` + `bubblewrap` + `curl` — **ni Node, ni `npx`, ni
> `ripgrep`** — donc aucun serveur tiers n'était enregistrable en l'état. La
> Story 5.2 a écrit un serveur stdio **interne** en Python
> (`infra/mcp/servers/code_search.py`) et documenté l'arbitrage dans
> [`dev-pole-code-search-server.md`](./dev-pole-code-search-server.md). La
> Story 5.5 (GitHub) hérite du même constat et devra le trancher pour
> elle-même — elle a besoin du **réseau**, que `unshare_net=True` ferme.
> Corrigé ici plutôt que laissé : un ADR qui reste faux fait prendre des
> décisions sur un état qui n'existe pas, ce qui est exactement ce qui s'est
> produit.

## Conséquences

- Le `tools: []` du Dev Lead est un état **documenté** (`docs/runbooks/pole-dev.md` § 3),
  pas un oubli. Le provisioning rend une ligne explicite « aucun déclaré »,
  pour qu'il ne soit pas confondu avec « outils non considérés ».
- **D63 reste ouverte** et n'est pas élargie par cette story.
- Quand le besoin d'outiller le moteur deviendra réel — c'est-à-dire quand un
  agent devra *lancer* ou *piloter* un run, pas en décrire un —, il prendra sa
  propre story, avec l'arbitrage sandbox comme objet principal.
- **Le `tools: []` du Dev Lead reste la décision de cet ADR**, et la Story 5.2
  ne le change pas : c'est le *Code Researcher* qui porte la première liste non
  vide du dépôt. Ce que la 5.2 invalide est le paragraphe « serveurs tiers »
  ci-dessus, pas la décision elle-même.
