# Serveur MCP de lecture de code pour le Pôle Dev (Story 5.2)

**Status:** Accepted (Sprint 2)
**Date:** 2026-09-15
**Story:** 5.2 — Code Researcher, exploration codebase
**Amende:** [`dev-pole-workflow-tooling.md`](./dev-pole-workflow-tooling.md) (Story 5.1)

## Décision

**Le Pôle Dev lit le codebase via un serveur MCP stdio écrit dans ce dépôt**
(`infra/mcp/servers/code_search.py`), et non via un serveur tiers.

**Sa politique d'accès vit dans `Settings` (`AGENTIVE_DEV_CODE_ROOTS`), pas
dans une colonne** — divergence assumée par rapport à la forme littérale du
défer **D62**.

## Problème 1 — aucun serveur tiers n'est enregistrable

L'AC1 demande « outils MCP filesystem (read-only) et grep/ripgrep ». L'ADR de
la Story 5.1 affirme que les serveurs de la Story 5.2 « sont **tiers**, ils
s'enregistrent normalement ». **C'était une hypothèse, pas un état relevé.**

| Chemin | État vérifié dans le dépôt |
|---|---|
| Un serveur MCP enregistré, quel qu'il soit | ❌ aucun — le seul du dépôt était `tests/fixtures/mcp_mock_server.py` |
| `@modelcontextprotocol/server-filesystem` (Node) | ❌ l'image backend est `python:3.14-slim` + `bubblewrap` + `curl` : pas de Node, pas de `npx` |
| Un serveur MCP tiers pour `ripgrep` | ⚠️ aucun dans le jeu de référence ; un paquet npm tiers non audité, dans un dépôt qui fait tourner gitleaks et SOPS, est un arbitrage de chaîne d'approvisionnement — pas un détail d'implémentation |
| Un serveur **stdio interne en Python** | ✅ et le blocage de la Story 5.1 ne s'applique pas : c'est l'absence de RÉSEAU (`unshare_net=True`) qui rendait impossible un serveur exposant le moteur de workflow, parce qu'il lui fallait Postgres. Un serveur de lecture de code a besoin du **disque** |
| Le SDK `mcp` côté serveur | ✅ `mcp>=1.27.0` est en dépendance de **production** (`pyproject.toml`), et `mcp.server.lowlevel` + `stdio_server` sont déjà exercés par six tests d'intégration du Tool Hub |

### Options

**A. Installer Node + `@modelcontextprotocol/server-filesystem` — rejetée.**
Ajoute un runtime entier à l'image pour quatre fonctions de lecture, et fait
dépendre une frontière de sécurité (l'allowlist de répertoires) d'un paquet
npm tiers dont la politique de refus n'est ni lisible ni testable ici.

**B. Installer le binaire `ripgrep` — rejetée.** Une dépendance système pour
ce que `re` fait sur un dépôt de cette taille. Le `search_content` livré rend
chemin + numéro de ligne + ligne, ce que l'AC demande.

**C. Serveur stdio interne en Python — retenue.** Trois raisons, chacune
suffisante :

1. **Aucune dépendance nouvelle**, ni système ni Python.
2. **Lecture seule par construction** : aucun mode d'ouverture en écriture,
   aucun `subprocess`, aucune exécution, aucun outil générique. La contrainte
   « outils en lecture seule en Sprint 2 » (`docs/runbooks/rejeu-et-outils.md`)
   cesse d'être procédurale — elle est gardée par un test structurel qui
   analyse l'AST du module.
3. **L'allowlist de répertoires vit DANS le serveur**, ce qui est la seule
   frontière qui reste quand le sandbox tombe en repli `setrlimit`.

Ce troisième point est le moins évident et le plus important : `sandbox.py`
écrit lui-même que le repli `setrlimit` **n'isole ni le réseau ni le
filesystem**. C'est l'état de tout poste où bwrap ne fonctionne pas — y
compris l'image de dev de ce projet, où le binaire existe mais où le noyau
refuse les user namespaces non privilégiés. Un « ça marche en local » ne dit
donc rien de la production, et dans le cas dégradé la garde du serveur n'est
pas une ceinture en plus des bretelles : c'est la seule des deux qui tienne.

### Conséquence : l'ADR de la Story 5.1 est amendé

Sa phrase « les Stories 5.2 (filesystem + ripgrep) et 5.5 (GitHub) ne sont pas
bloquées : leurs serveurs MCP sont **tiers** » est corrigée sur place. Un ADR
qui reste faux est pire qu'un ADR absent — il fait prendre des décisions sur
un état qui n'existe pas, ce qui est exactement ce qui s'est produit ici.

La Story 5.5 (GitHub) hérite du même constat et devra le trancher pour
elle-même : elle a besoin du **réseau**, que `unshare_net=True` ferme.

## Problème 2 — aucun appelant ne passait de `SandboxProfile`

Relevé, et re-vérifié avant d'implémenter : `grep -rn "profile=" src` ne
rendait aucun appelant. Ni `McpToolExecutor`, ni `ToolHubService`, ni
`mise_en_place._check_tools` ne passaient de `profile` à `call_tool` /
`discover_tools`. Le défaut de `SandboxProfile` ne ro-bind que
`/usr /etc /lib /lib64 /bin /sbin` : **aucun répertoire de projet n'est
visible dans le sandbox**, et `/app/.venv` non plus. Sous bwrap, le serveur
n'aurait vu ni le code à lire, ni son propre interpréteur.

C'est le défer **D62**, et `architecture.md:551` le prescrit mot pour mot :
« profile sandbox par outil MCP : … mount filesystem read-only …, whitelist
explicite des binaires accessibles ».

### Ce qui est livré

`infra/mcp/policy.py` rend le couple `(profil, connection_config effectif)`
pour un serveur donné, et il est appelé sur **les trois** chemins :
l'enregistrement (`ToolHubService.connect_server`), le ping de lancement
(`mise_en_place._check_tools`) et l'exécution (`McpToolExecutor`,
`ToolHubService.invoke_tool`). En passer un et pas l'autre livrerait un
serveur qui s'enregistre et ne s'exécute pas — ou pire, un ping qui passe pour
une exécution qui échoue, c'est-à-dire un échec déplacé loin de sa cause.

Pour tout serveur que la politique ne connaît pas, elle rend `(None, config)` :
le comportement d'avant cette story, octet pour octet.

### Divergence assumée sur D62 : `Settings`, pas une colonne

D62 décrit littéralement une colonne `tools.sandbox_profile JSONB`. **Une
allowlist de chemins qui décide de ce qu'un agent LLM peut lire est une règle
de sécurité : elle doit être lisible dans un diff git et revue comme du code,
pas modifiable par un `UPDATE`.**

La substance de D62 est donc livrée (une politique par serveur, effectivement
appliquée, sur les trois chemins) ; sa forme ne l'est pas. Conséquence
concrète, et c'est ce qui rend la divergence utile plutôt que cosmétique :
`apply_sandbox_policy` **réécrit** l'`argv` et l'`env` du serveur à chaque
appel depuis `Settings`, donc ce que porte `tool_servers.connection_config`
n'élargit rien. Une row trafiquée qui déclarerait `--root /` est sans effet.

D62 reste ouvert pour le cas général (un profil par outil tiers, où la valeur
ne peut pas être connue du code) ; il est fermé pour les serveurs internes.

### ⚠️ Rétracté par la revue de code (2026-09-15) : la « fuite d'environnement »

> La version d'origine de cette section affirmait : « `sandboxed_subprocess` fait
> `env=effective_env or None`, et `None` fait **hériter tout l'environnement du
> backend** — `ANTHROPIC_API_KEY`, les mots de passe Postgres, le reste — sur le
> chemin `setrlimit` ». Elle est conservée ici parce qu'un ADR qui efface son
> erreur est moins utile qu'un ADR qui la montre.

**C'est faux, et vérifié dans le container.** Le spawn de production passe par
`infra/mcp/client.py`, qui pose `StdioServerParameters(env=None)` quand aucun
`env` n'est déclaré ; le SDK MCP applique alors `get_default_environment()`, une
**allowlist** qui rend `{'HOME', 'PATH'}`. La fonction `sandboxed_subprocess`
citée comme le mécanisme fautif n'est appelée par **aucun** code de production
(`grep -rn "sandboxed_subprocess" backend/src/` ne rend que des docstrings). Sous
bwrap, `--clearenv` ferme le sujet de toute façon, et `_build_bwrap_argv`
retombe sur `os.environ` quand `parent_env` vaut `None` : le résultat est
identique avec ou sans `env` déclaré.

Il n'y avait donc **aucune fuite à fermer**, sur aucun des deux chemins. Cette
prémisse venait du piège #12 de la story, qui a été amendé en conséquence.

**Ce que la réécriture de l'`env` apporte réellement** est la même propriété que
pour la commande et l'`argv` : l'**intégrité de la configuration**. La colonne
`connection_config.env` ne décide de rien — un `UPDATE` qui y écrirait un jeton
est écrasé avant le spawn. C'est une garantie utile ; ce n'est pas celle qui
était écrite.

Le serveur n'expose **que** des outils de lecture pour une raison qui, elle,
tenait seule dès le départ : un outil générique d'exécution donnerait à un agent
LLM un moyen d'exécuter du code arbitraire dans le container, fuite ou pas.

## Ce que la décision coûte

- **Un sous-processus par appel d'outil et par ping de Mise en Place.** Il n'y
  a pas de pool persistant (défer D61), donc chaque `call_tool` spawne un
  interpréteur Python. Acceptable pour de la lecture de code ; à revoir si le
  pôle devient bavard en outils.
- **Un code de serveur à maintenir**, là où un serveur tiers serait maintenu
  ailleurs. En contrepartie, sa politique de refus est testable ici, et elle
  l'est : chaque refus (`..`, chemin absolu, symlink sortant, deny-list) a son
  test, et la propriété « lecture seule » est gardée par analyse d'AST plutôt
  que par relecture.
- **La commande du serveur est un chemin absolu d'interpréteur**
  (`sys.executable`, résolu au provisioning). Une base réutilisée sur une
  machine où le venv vit ailleurs enregistre une commande qui n'existe plus :
  l'échec est alors à la **découverte** (`DependencyError` 503), pas au milieu
  d'un run, et `make seed-dev` le dit avec la commande à rejouer.

## Ce que la décision ne fait PAS

- **D63 (allowlist réseau SSE) n'est pas rouverte** : le serveur est en
  **stdio**, donc sandboxé, et ne touche pas à ce débat.
- **Aucun outil d'écriture, nulle part.** La contrainte « lecture seule en
  Sprint 2 » (Story 5.0 AC5) n'est pas assouplie.
- **Le pool persistant (D61) reste ouvert.**
