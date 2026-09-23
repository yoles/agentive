"""Politique de sandbox **par serveur** MCP — Story 5.2 (défer D62).

Le constat qui rend ce module nécessaire, relevé dans le dépôt et non supposé :
``grep -rn "profile=" src`` ne rendait AUCUN appelant. Ni ``McpToolExecutor``,
ni ``ToolHubService``, ni ``mise_en_place._check_tools`` ne passaient de
``profile`` à ``call_tool`` / ``discover_tools`` — donc ``SandboxProfile()``
par défaut s'appliquait partout, et ses ``ro_binds`` sont
``('/usr', '/etc', '/lib', '/lib64', '/bin', '/sbin')``. **Aucun répertoire de
projet n'était visible dans le sandbox**, et ``/app/.venv`` non plus : sous
bwrap, un serveur de lecture de code ne verrait pas le code à lire et ne
pourrait même pas démarrer. ``architecture.md:551`` prescrit l'inverse mot pour
mot : « profile sandbox par outil MCP : … mount filesystem read-only …,
whitelist explicite des binaires accessibles ».

Ce que fait :func:`apply_sandbox_policy`
----------------------------------------
Il rend le couple ``(profil, connection_config effectif)`` pour un serveur
donné, et il est appelé sur **les deux** chemins — la découverte
(``discover_tools``) et l'exécution (``call_tool``). Passer le profil à un
seul des deux livrerait un serveur qui s'enregistre et ne s'exécute pas, ou
l'inverse.

Pourquoi il RÉÉCRIT la configuration de connexion
-------------------------------------------------
``tool_servers.connection_config`` est une colonne. Les racines autorisées
sont une **règle de sécurité** : elles viennent de ``Settings``
(``AGENTIVE_DEV_CODE_ROOTS``), et ce module les réinjecte dans l'``argv`` à
chaque appel. Conséquence directe et voulue : un ``UPDATE`` sur cette colonne
ne peut pas élargir l'allowlist du serveur, parce que ce qui y est écrit est
écrasé avant le spawn.

Il réécrit aussi ``env``, mais PAS pour la raison que cette docstring donnait
avant la revue de la Story 5.2. La version d'origine affirmait qu'un serveur
stdio sans ``env`` déclaré « hérite de TOUT l'environnement du backend ».
**C'est faux sur le chemin de production, et c'était le piège #12 de la
story.** ``infra/mcp/client.py`` passe ``StdioServerParameters(env=None)``, et
le SDK MCP applique alors ``get_default_environment()`` — une allowlist, qui
rend ``{'HOME', 'PATH'}``. ``sandboxed_subprocess``, citée comme le mécanisme
fautif, n'est appelée par aucun code de production. Sous bwrap, ``--clearenv``
ferme le sujet de toute façon.

La vraie raison de réécrire ``env`` est la même que pour ``args`` : rendre la
colonne **non-autoritaire**. Un ``UPDATE`` qui y écrirait un jeton est écrasé
avant le spawn. C'est une propriété d'intégrité de configuration, pas un
correctif de fuite.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Final, Literal

from agentive_backend.infra.mcp.sandbox import SandboxProfile
from agentive_backend.shared.config import settings

#: Le module lancé en ``python -m`` par le serveur de lecture de code. C'est
#: aussi la SIGNATURE qui permet de reconnaître ce serveur dans un
#: ``connection_config`` : il n'existe pas d'autre marqueur fiable (le ``name``
#: de la row est choisi par le catalogue et peut être renommé).
CODE_SEARCH_MODULE: Final = "agentive_backend.infra.mcp.servers.code_search"

#: Ce qu'il faut ro-binder EN PLUS des racines pour que l'interpréteur démarre.
#: `/usr`, `/lib`, `/lib64` portent la libc et le binaire Python système ; le
#: venv, lui, vit sous une racine (``/app/.venv``) et n'a donc pas à être
#: listé. ``/etc`` n'y est PAS : le serveur n'a besoin ni de résolution DNS
#: (pas de réseau) ni de ``/etc/passwd``, et ``/etc`` est exactement le genre
#: de répertoire qu'un agent de lecture n'a aucune raison de voir.
_INTERPRETER_RO_BINDS: Final[tuple[str, ...]] = ("/usr", "/lib", "/lib64")

#: `PATH` seul suffit : la commande est un chemin ABSOLU d'interpréteur (cf
#: :func:`code_search_command`), donc rien n'est résolu par ``PATH`` — il reste
#: pour les rares sous-outils de la stdlib qui le consultent.
_CODE_SEARCH_ENV_PASSTHROUGH: Final[tuple[str, ...]] = ("PATH",)


def code_search_command() -> str:
    """L'interpréteur qui lance le serveur — chemin ABSOLU, et vérifié.

    Sous bwrap, ``--clearenv`` ne laisse que ``PATH``, dont la valeur vient du
    parent : lancer ``"python"`` dépendrait alors d'un ``PATH`` qui pointe vers
    le venv, ce qui n'est vrai que par chance. ``sys.executable`` est le venv
    du backend lui-même (``/app/.venv/bin/python`` dans le container), et il
    est sous la racine ``/app``, donc ro-bindé par le profil.

    C'est le point exact où un serveur « enregistré avec succès » peut se
    révéler impossible à lancer : ``connect_server`` traduit un
    ``FileNotFoundError`` de spawn en ``DependencyError`` 503, donc l'échec
    arrive à la DÉCOUVERTE et non au milieu d'un run.

    **« et vérifié » est désormais vrai** (revue 5.2) : la version d'origine
    rendait ``sys.executable`` sans aucun contrôle, alors que sa propre
    docstring annonçait une vérification. Un interpréteur gelé ou embarqué peut
    rendre une chaîne vide, et l'erreur ne surgissait qu'en aval, sous la forme
    d'un ``ValueError`` de ``client.py`` qui ne nomme rien.
    """
    command = sys.executable
    if not command or not Path(command).is_absolute():
        raise RuntimeError(
            "`sys.executable` ne rend pas un chemin absolu d'interpréteur "
            f"({command!r}) — le serveur de lecture de code ne peut pas être lancé. "
            "Interpréteur embarqué ou gelé ?"
        )
    if not Path(command).exists():
        raise RuntimeError(
            f"l'interpréteur {command} n'existe pas. Une base réutilisée sur une machine "
            "où le venv vit ailleurs enregistre une commande qui n'existe plus — c'est le "
            "mode de panne que `docs/decisions/dev-pole-code-search-server.md` nomme."
        )
    return command


def code_search_args(roots: tuple[str, ...] | None = None) -> list[str]:
    """L'``argv`` complet du serveur, bornes comprises.

    Les bornes voyagent par ``argv`` et non par l'environnement pour la même
    raison que les racines : ``--clearenv`` sous bwrap et le filtrage
    ``env_passthrough`` sous ``setrlimit`` feraient disparaître une variable,
    et le serveur retomberait en silence sur ses défauts de module.
    """
    effective = roots if roots is not None else settings.dev_code_roots
    args = ["-m", CODE_SEARCH_MODULE]
    for root in effective:
        args.extend(["--root", root])
    args.extend(["--max-read-bytes", str(settings.dev_code_max_read_bytes)])
    args.extend(["--max-results", str(settings.dev_code_max_results)])
    args.extend(["--max-depth", str(settings.dev_code_max_depth)])
    return args


def code_search_connection_config(roots: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Le ``connection_config`` à ENREGISTRER pour ce serveur.

    Identique à ce que :func:`apply_sandbox_policy` recalculera à chaque
    appel. Le persister quand même a une valeur : la row dit ce que le serveur
    est, et un opérateur qui lit la table voit la commande réelle plutôt
    qu'une indirection.
    """
    return {
        "command": code_search_command(),
        "args": code_search_args(roots),
        "env": _code_search_env(),
    }


def code_search_profile(roots: tuple[str, ...] | None = None) -> SandboxProfile:
    """Le ``SandboxProfile`` du serveur de lecture de code.

    ``ro_binds`` = les racines autorisées + le strict nécessaire à
    l'interpréteur, et **rien d'autre** : c'est ce que le test de T2.4 assère
    sur l'``argv`` bwrap, parce que c'est la seule façon de couvrir ce chemin
    sans binaire ``bwrap`` (absent de cet environnement, d'où les 4 tests
    skippés à chaque passe).

    ``unshare_net`` reste à ``True`` : lire du code ne demande pas de réseau,
    et le blocage de la Story 5.1 (un serveur exposant le moteur de workflow
    avait besoin de Postgres) ne s'applique donc pas ici.
    """
    effective = roots if roots is not None else settings.dev_code_roots
    ro_binds: list[str] = []
    for path in (*effective, *_INTERPRETER_RO_BINDS):
        if path not in ro_binds:
            ro_binds.append(path)
    return SandboxProfile(
        unshare_net=True,
        ro_binds=tuple(ro_binds),
        env_passthrough=_CODE_SEARCH_ENV_PASSTHROUGH,
    )


def is_code_search_server(connection_config: dict[str, Any] | None) -> bool:
    """``True`` si cette configuration lance le serveur de lecture de code.

    **Reconnaissance stricte sur la TÊTE de l'``argv``** (revue 5.2). La
    version d'origine testait ``CODE_SEARCH_MODULE in args`` — une simple
    appartenance, sans contrainte de position : une row dont les ``args``
    contenaient la chaîne n'importe où était reconnue comme le serveur de
    lecture de code et recevait son profil (donc ``/app`` ro-bindé), tout en
    gardant le ``command`` qu'elle voulait.
    """
    if not isinstance(connection_config, dict):
        return False
    args = connection_config.get("args")
    if not isinstance(args, list) or len(args) < 2:
        return False
    return bool(args[0] == "-m" and args[1] == CODE_SEARCH_MODULE)


def apply_sandbox_policy(
    *,
    transport: Literal["stdio", "sse"],
    connection_config: dict[str, Any],
) -> tuple[SandboxProfile | None, dict[str, Any]]:
    """Le profil et la configuration effective pour ce serveur.

    Rend ``(None, connection_config)`` pour tout serveur que la politique ne
    connaît pas : le comportement d'avant cette story, octet pour octet. La
    politique AJOUTE une règle pour un serveur nommé ; elle ne change rien
    pour les autres.
    """
    if transport != "stdio" or not is_code_search_server(connection_config):
        return None, connection_config
    effective = dict(connection_config)
    # Réécrit depuis `Settings` / le processus, jamais relu depuis la colonne :
    # c'est ce qui rend l'allowlist non modifiable par un `UPDATE`.
    #
    # `command` en fait partie depuis la revue 5.2. Il ne l'était PAS, et la
    # docstring de ce module promettait pourtant qu'« un `UPDATE` sur cette
    # colonne ne peut pas élargir l'allowlist » : le binaire réellement exécuté
    # restait décidé par la row, et recevait le profil qui ro-binde les racines.
    effective["command"] = code_search_command()
    effective["args"] = code_search_args()
    effective["env"] = _code_search_env()
    return code_search_profile(), effective


def _code_search_env() -> dict[str, str]:
    """L'``env`` effectif — réécrit depuis le processus, jamais lu de la colonne.

    ⚠️ Corrigé par la revue de la Story 5.2 : ce n'est PAS un correctif de
    fuite d'environnement. Il n'y a pas de fuite — le SDK MCP applique
    ``get_default_environment()`` (allowlist ``{HOME, PATH}``) quand aucun
    ``env`` n'est fourni, et bwrap fait ``--clearenv``. Ce que cette fonction
    garantit est l'intégrité : la colonne ``connection_config.env`` ne décide
    de rien, exactement comme ``args``.
    """
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}


__all__ = [
    "CODE_SEARCH_MODULE",
    "apply_sandbox_policy",
    "code_search_args",
    "code_search_command",
    "code_search_connection_config",
    "code_search_profile",
    "is_code_search_server",
]
