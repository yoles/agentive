"""Serveur MCP stdio — lecture de code, **en lecture seule par construction**.

Story 5.2 (FR51). Premier serveur MCP de production du dépôt, et donc le
premier consommateur réel de la boucle d'outils livrée par la Story 5.0 et du
mécanisme d'assignation déclarative livré par la Story 5.1.

Pourquoi un serveur **interne**
-------------------------------
L'AC1 de la story demande « filesystem (read-only) et grep/ripgrep ». Aucun
serveur tiers n'est enregistrable en l'état : l'image backend est
``python:3.14-slim`` + ``bubblewrap`` + ``curl`` — ni Node, ni ``npx``, ni
``ripgrep``. Le SDK ``mcp>=1.27.0`` est en dépendance de **production** et
``mcp.server.lowlevel`` + ``stdio_server`` sont déjà exercés par
``tests/fixtures/mcp_mock_server.py`` : écrire le serveur ici n'ajoute ni
dépendance système ni paquet tiers. Le dossier complet est dans
``docs/decisions/dev-pole-code-search-server.md``.

Trois propriétés que ce module tient, et qu'un serveur tiers ne tiendrait pas
-----------------------------------------------------------------------------
1. **Lecture seule par construction.** Aucun mode d'ouverture en écriture,
   aucun ``subprocess``, aucune exécution, aucun outil générique. La
   contrainte « outils en lecture seule en Sprint 2 »
   (``docs/runbooks/rejeu-et-outils.md``) cesse d'être procédurale.
2. **L'allowlist de répertoires vit DANS le serveur.** Elle est la seule
   frontière qui reste quand le sandbox tombe en repli ``setrlimit`` — ce
   repli n'isole ni le réseau ni le filesystem, ``infra/mcp/sandbox.py`` le
   dit lui-même, et c'est l'état de tout poste où ``bwrap`` ne fonctionne pas.
   Le ``SandboxProfile`` dérivé par :mod:`agentive_backend.infra.mcp.policy`
   est la SECONDE moitié de la garde, pas la première.
3. **Les racines arrivent par ``argv``, jamais par la base.** C'est
   l'appelant qui les réécrit à chaque appel depuis ``Settings``
   (``AGENTIVE_DEV_CODE_ROOTS``), donc un ``UPDATE`` sur
   ``tool_servers.connection_config`` ne peut pas élargir l'allowlist.

Lancement::

    python -m agentive_backend.infra.mcp.servers.code_search --root /app

Outils exposés : ``list_directory``, ``read_file``, ``find_files``,
``search_content``. Ce dernier est le « grep/ripgrep » de l'AC1, en ``re``
pur : installer un binaire ``ripgrep`` pour ce que la stdlib fait sur un dépôt
de cette taille serait une dépendance système pour rien.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

#: Nom du serveur annoncé au protocole. Distinct du nom de la row
#: ``tool_servers`` (qui est choisi par le catalogue) : celui-ci est
#: l'identité du programme, celui-là l'identité de l'enregistrement.
SERVER_NAME: Final = "agentive-code-search"
SERVER_VERSION: Final = "1.0.0"

# ─── Bornes de sortie, appliquées À LA SOURCE ────────────────────────────
#
# `MAX_TOOL_RESULT_CHARS` (infra/mcp/tool_executor.py) tronque déjà ce qui
# part dans le prompt — mais APRÈS avoir tout lu. Un `search_content` sur un
# monorepo paierait la lecture entière pour rendre 8 000 caractères. Ces
# bornes-ci coupent le TRAVAIL, pas seulement la sortie.
#
# Et chaque troncature est DITE dans le résultat (`truncated: true`). Un
# résultat coupé en silence fait croire au modèle qu'il a tout vu, et un
# Chercheur qui rend « je n'ai rien trouvé » sur une troncature est pire
# qu'un Chercheur qui échoue.
DEFAULT_MAX_READ_BYTES: Final = 6_000
DEFAULT_MAX_RESULTS: Final = 100
DEFAULT_MAX_DEPTH: Final = 12

#: Plafonds durs, non réglables : ils protègent le processus lui-même, pas la
#: taille de la réponse. Un réglage d'exploitation qui les dépasserait rendrait
#: la borne inopérante tout en donnant l'illusion qu'elle existe (même posture
#: que les `le=` des plafonds de la Story 5.0).
HARD_MAX_READ_BYTES: Final = 200_000
HARD_MAX_RESULTS: Final = 1_000
HARD_MAX_DEPTH: Final = 40
#: Nombre maximum de fichiers OUVERTS par un `search_content`, quelle que
#: soit la quantité de résultats demandée : c'est le coût de balayage, et il
#: est distinct du nombre de résultats rendus.
MAX_FILES_SCANNED: Final = 4_000
#: Un fichier plus gros que ça n'est pas fouillé (binaire, artefact de build,
#: dump). Le signaler coûterait plus cher que le sauter.
MAX_SCAN_FILE_BYTES: Final = 1_000_000
#: Une ligne de correspondance plus longue que ça est coupée : un minifié sur
#: une seule ligne remplirait la réponse à lui seul.
MAX_MATCH_LINE_CHARS: Final = 400
#: Borne sur la LONGUEUR du motif regex. Elle ne borne QUE la longueur, et la
#: revue 5.2 a corrigé la prétention d'en face : `(a+)+$` fait 7 caractères et
#: suffit à faire tourner `re` indéfiniment. Ce qui borne réellement les
#: dégâts d'un motif pathologique est le plafond mur de `call_tool`
#: (`AGENTIVE_TOOL_CALL_TIMEOUT_S`), qui tue le sous-processus — pas cette
#: constante. `_NESTED_QUANTIFIER` ci-dessous en refuse la forme la plus
#: courante ; c'est une heuristique, pas une preuve, et elle est nommée comme
#: telle.
MAX_PATTERN_CHARS: Final = 500

#: Heuristique anti-backtracking : un quantificateur appliqué à un groupe qui
#: en contient déjà un (`(a+)+`, `(a*)*`, `(\w+)+`). Refusée à la source parce
#: que `re` n'a pas de timeout et que le coût est exponentiel. Ne prétend PAS
#: détecter tous les motifs pathologiques.
_NESTED_QUANTIFIER: Final = re.compile(r"\([^()]*[+*][^()]*\)\s*[+*]")

#: Refusé à la LECTURE **et** filtré des RÉSULTATS DE RECHERCHE.
#:
#: Raison, nommée ici parce qu'elle n'est pas évidente : sous le repli
#: `setrlimit` il n'y a AUCUNE isolation filesystem, donc cette liste est la
#: seule frontière qui existe à l'intérieur d'une racine autorisée. Le
#: container ne monte aujourd'hui que `./backend` (`docker-compose.yml`), ce
#: qui met `.env` hors de portée **par accident de configuration** et non par
#: conception — un opérateur qui élargirait la racine annulerait la protection
#: sans le savoir. Cette liste, elle, voyage avec le serveur.
#:
#: **Comparée en minuscules** (revue 5.2) : `fnmatch` passe par
#: `os.path.normcase`, qui est l'identité sous POSIX — `Secrets/` et
#: `DEPLOY.PEM` étaient donc lisibles. Une deny-list sensible à la casse sur
#: un filesystem sensible à la casse n'est pas une deny-list.
#:
#: Les motifs couvrent des CLASSES, pas des noms précis : `.env*` (et non le
#: couple `.env` + `.env.*`, qui laissait passer `.envrc` et `.env_backup`),
#: `*.env` pour les `staging.env`, et les porteurs de secrets que la première
#: version ne nommait pas (`.netrc`, `.npmrc`, `credentials`, `*.p12`…).
DENIED_NAME_PATTERNS: Final[tuple[str, ...]] = (
    ".env*",
    "*.env",
    ".git",
    ".ssh",
    ".aws",
    ".gnupg",
    "*.pem",
    "*.key",
    "*.crt",
    "*.p12",
    "*.pfx",
    "*.jks",
    "id_*",
    "*.sqlite",
    "*.sqlite3",
    "secrets",
    "secrets.*",
    ".secrets*",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.*",
)

#: Répertoires sautés au PARCOURS — et c'est une liste de BRUIT, pas une
#: frontière de sécurité. La distinction est le point : un chemin de cette
#: liste demandé explicitement reste lisible (ce n'est pas un secret), alors
#: qu'un chemin de `DENIED_NAME_PATTERNS` est refusé même nommé.
#:
#: Sans elle, le défaut de production est piégeux : la racine par défaut est
#: `/app`, `docker-compose.yml` y monte `backend_venv:/app/.venv`, et
#: `os.walk` trie les noms — donc `.venv` passe AVANT `src`, `scripts` et
#: `tests`. Un `search_content` depuis la racine brûlait tout son budget de
#: balayage dans le venv et rendait un résultat vide (revue 5.2).
SKIPPED_DIR_PATTERNS: Final[tuple[str, ...]] = (
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "site-packages",
)

#: Extensions jamais fouillées par `search_content` : binaires et artefacts.
#: Les `.pyc` du dépôt contiennent des octets de contrôle que `splitlines()`
#: et l'itérateur de fichier ne découpent pas de la même façon — les fouiller
#: rendait des numéros de ligne non reproductibles, en plus de gaspiller le
#: budget.
SKIPPED_SUFFIXES: Final[tuple[str, ...]] = (
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".o",
    ".a",
    ".zip",
    ".gz",
    ".tar",
    ".whl",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
)


class PathRefusedError(ValueError):
    """Un chemin sort des racines autorisées, ou touche la deny-list.

    ``ValueError`` à dessein : le SDK MCP traduit une exception levée dans un
    handler en ``CallToolResult.isError=True``, c'est-à-dire en **résultat
    d'erreur exploitable** plutôt qu'en plantage du run — la règle posée par
    la Story 5.0 T4.3. Le modèle peut corriger son chemin ; il ne peut rien
    faire d'un run mort.
    """


@dataclass(frozen=True)
class CodeSearchLimits:
    """Les trois bornes réglables (T1.6), déjà clampées."""

    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_results: int = DEFAULT_MAX_RESULTS
    max_depth: int = DEFAULT_MAX_DEPTH

    @classmethod
    def clamped(cls, *, max_read_bytes: int, max_results: int, max_depth: int) -> CodeSearchLimits:
        return cls(
            max_read_bytes=max(1, min(max_read_bytes, HARD_MAX_READ_BYTES)),
            max_results=max(1, min(max_results, HARD_MAX_RESULTS)),
            # `max(1, …)` et non `max(0, …)` : `Settings` impose `ge=1`, mais un
            # `--max-depth 0` passé par argv rendait un parcours qui ne descend
            # jamais, sans que ce soit documenté nulle part (revue 5.2).
            max_depth=max(1, min(max_depth, HARD_MAX_DEPTH)),
        )


@dataclass(frozen=True)
class CodeSearchGuard:
    """La garde de chemin — moitié « serveur » de l'AC1.

    ``Path.resolve()`` SUIT les symlinks, et c'est précisément ce qui rend le
    contrôle vrai plutôt que cosmétique : un ``..``, un chemin absolu hors
    racine et un lien vers ``/etc`` échouent tous les trois, par le même test.
    """

    roots: tuple[Path, ...]

    @classmethod
    def from_paths(cls, paths: Sequence[str | Path]) -> CodeSearchGuard:
        resolved: list[Path] = []
        for raw in paths:
            # Revue 5.2 — une racine RELATIVE (ou vide) était résolue contre le
            # cwd du processus, que le modèle ne contrôle ni n'observe, alors
            # que la docstring du paramètre `--root` affirmait l'inverse.
            # `Path("")` vaut `.`, donc le cas vide passait aussi.
            text = str(raw).strip()
            if not text:
                raise ValueError("racine autorisée vide (--root)")
            if not Path(text).is_absolute():
                raise ValueError(
                    f"racine autorisée relative : {text!r}. Le répertoire courant du "
                    "sous-processus sandboxé n'est pas une notion contrôlée — utiliser un "
                    "chemin absolu."
                )
            root = Path(text).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"racine autorisée inexistante ou non répertoire : {root}")
            if root == Path(root.anchor):
                # `/` comme racine autorisée annule l'allowlist tout en ayant
                # l'air d'en être une. Refusé à la source plutôt que découvert
                # le jour où un agent lit `/proc/self/environ`.
                raise ValueError(
                    f"racine autorisée trop large : {root}. Une allowlist qui contient la "
                    "racine du filesystem n'est pas une allowlist."
                )
            # Revue 5.2 — `denied_component` n'inspecte que le chemin RELATIF à
            # la racine, donc une racine `/app/secrets` rendait tout son
            # sous-arbre lisible : aucun composant relatif ne porte alors le nom
            # interdit. Le contrôle manquait à l'endroit où il coûte le moins.
            denied = next((part for part in root.parts if _name_is_denied(part)), None)
            if denied is not None:
                raise ValueError(
                    f"racine autorisée en deny-list : {root} traverse {denied!r}. "
                    "Une racine qui contient elle-même un nom interdit annule la deny-list "
                    "pour tout son sous-arbre."
                )
            resolved.append(root)
        if not resolved:
            raise ValueError("au moins une racine autorisée est requise (--root)")
        return cls(roots=tuple(resolved))

    @property
    def primary(self) -> Path:
        """La racine contre laquelle un chemin RELATIF est interprété."""
        return self.roots[0]

    def resolve(self, raw: str) -> Path:
        """Le chemin absolu correspondant à ``raw``, ou :class:`PathRefusedError`.

        Un chemin relatif est interprété depuis :attr:`primary` — jamais
        depuis le répertoire courant du processus, qui n'est pas une notion
        que le modèle contrôle ni observe.
        """
        if not isinstance(raw, str):
            raise PathRefusedError(
                f"chemin invalide : attendu une chaîne, reçu {type(raw).__name__}"
            )
        if not raw.strip():
            raise PathRefusedError("chemin vide")
        candidate = Path(raw.strip())
        if not candidate.is_absolute():
            candidate = self.primary / candidate
        try:
            target = candidate.resolve()
        except OSError as exc:  # pragma: no cover — ELOOP, ENAMETOOLONG…
            raise PathRefusedError(f"chemin illisible : {exc.strerror or exc}") from exc
        root = self.root_of(target)
        if root is None:
            raise PathRefusedError(
                f"chemin hors des racines autorisées : {raw!r} résout vers {target}. "
                f"Racines autorisées : {', '.join(str(r) for r in self.roots)}."
            )
        denied = self.denied_component(target, root)
        if denied is not None:
            raise PathRefusedError(
                f"chemin refusé par la deny-list : {raw!r} traverse {denied!r}. "
                "Ce serveur ne lit ni secrets, ni clés, ni l'historique git."
            )
        return target

    def root_of(self, target: Path) -> Path | None:
        """La racine qui contient ``target``, ou ``None``."""
        for root in self.roots:
            if target == root or root in target.parents:
                return root
        return None

    @staticmethod
    def denied_component(target: Path, root: Path) -> str | None:
        """Le premier composant de ``target`` sous ``root`` qui est en deny-list."""
        try:
            relative = target.relative_to(root)
        except ValueError:  # pragma: no cover — appelé après `root_of`
            return None
        for part in relative.parts:
            if _name_is_denied(part):
                return part
        return None

    def is_visible(self, target: Path) -> bool:
        """``True`` si ``target`` peut apparaître dans un RÉSULTAT.

        Même prédicat que :meth:`resolve`, mais sans exception : un parcours
        saute ce qu'il ne doit pas montrer, là où une demande explicite est
        refusée en le disant. Les deux passent par ``resolve()``, donc un
        symlink qui sort d'une racine est écarté des résultats aussi.
        """
        try:
            resolved = target.resolve()
        except OSError:  # pragma: no cover — lien cassé pendant le parcours
            return False
        root = self.root_of(resolved)
        if root is None:
            return False
        return self.denied_component(resolved, root) is None

    def display(self, target: Path) -> str:
        """Le chemin ABSOLU, tel quel.

        L'AC2 exige que la sortie du Chercheur cite « le chemin exact » de
        chaque fichier, et le test de l'AC3 vérifie ces chemins SUR LE DISQUE.
        Rendre un chemin relatif à une racine que le modèle ne connaît pas
        rendrait cette vérification impossible.
        """
        return str(target)


# ─── Implémentations ─────────────────────────────────────────────────────


def _iter_files(
    guard: CodeSearchGuard, start: Path, *, max_depth: int, budget: int, report: ScanReport
) -> Iterator[Path]:
    """Parcours borné en profondeur, filtré par la deny-list, sans symlink.

    ``followlinks=False`` (le défaut de ``os.walk``) empêche une boucle de
    liens de faire tourner le parcours indéfiniment ; :meth:`is_visible`
    referme le cas du lien qui ne boucle pas mais sort d'une racine.

    **Tout arrêt anticipé est consigné dans ``report``** — c'est ce qui manquait
    (revue 5.2). Un ``return`` nu est indiscernable d'une fin de parcours
    normale, et l'appelant rendait alors ``truncated: false``.
    """
    start_depth = len(start.parts)
    scanned = 0

    def _on_error(_exc: OSError) -> None:
        # `os.walk` avale les `EACCES` par défaut : un sous-arbre interdit
        # disparaissait du résultat sans laisser de trace.
        report.unreadable_dirs += 1

    for dirpath, dirnames, filenames in os.walk(start, followlinks=False, onerror=_on_error):
        here = Path(dirpath)
        depth = len(here.parts) - start_depth
        if depth >= max_depth:
            if dirnames:
                report.depth_limited = True
            dirnames[:] = []
        # Élaguer AVANT de descendre : ne pas entrer dans `.git` coûte moins
        # cher que d'en filtrer chaque fichier, et le résultat est le même.
        # `_dir_is_skipped` retire le BRUIT (`.venv`, `node_modules`) — sans
        # lui, le budget partait entier dans le venv monté sous `/app`.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not _name_is_denied(name)
            and not _dir_is_skipped(name)
            and guard.is_visible(here / name)
        )
        for name in sorted(filenames):
            if _name_is_denied(name):
                continue
            candidate = here / name
            if not guard.is_visible(candidate):
                continue
            scanned += 1
            if scanned > budget:
                report.budget_exhausted = True
                return
            report.files_scanned = scanned
            yield candidate


def _name_is_denied(name: str) -> bool:
    """Deny-list, comparée en MINUSCULES (revue 5.2).

    ``fnmatch`` normalise par ``os.path.normcase``, identité sous POSIX :
    ``Secrets/`` et ``DEPLOY.PEM`` passaient au travers.
    """
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in DENIED_NAME_PATTERNS)


def _dir_is_skipped(name: str) -> bool:
    """Bruit de parcours — PAS une frontière de sécurité (cf `SKIPPED_DIR_PATTERNS`)."""
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in SKIPPED_DIR_PATTERNS)


def _suffix_is_skipped(name: str) -> bool:
    return name.lower().endswith(SKIPPED_SUFFIXES)


@dataclass
class ScanReport:
    """Pourquoi un parcours s'est arrêté — et c'est la moitié du résultat.

    Le module s'interdit en tête de fichier de couper en silence ; il le
    faisait pourtant sur QUATRE chemins (revue 5.2) : budget de balayage
    épuisé, profondeur atteinte, fichier trop gros, répertoire illisible.
    Chacun rendait ``truncated: false`` sur un résultat incomplet, c'est-à-dire
    « je n'ai rien trouvé » là où il fallait lire « je n'ai pas tout regardé ».
    """

    files_scanned: int = 0
    budget_exhausted: bool = False
    depth_limited: bool = False
    unreadable_dirs: int = 0
    skipped_large: int = 0
    skipped_unreadable: int = 0

    @property
    def incomplete(self) -> bool:
        return (
            self.budget_exhausted
            or self.depth_limited
            or self.unreadable_dirs > 0
            or self.skipped_large > 0
            or self.skipped_unreadable > 0
        )

    def reasons(self) -> list[str]:
        out: list[str] = []
        if self.budget_exhausted:
            out.append(f"budget de balayage épuisé ({MAX_FILES_SCANNED} fichiers)")
        if self.depth_limited:
            out.append("profondeur maximale atteinte")
        if self.unreadable_dirs:
            out.append(f"{self.unreadable_dirs} répertoire(s) illisible(s)")
        if self.skipped_large:
            out.append(f"{self.skipped_large} fichier(s) trop volumineux")
        if self.skipped_unreadable:
            out.append(f"{self.skipped_unreadable} fichier(s) illisible(s)")
        return out


def _start_dir(guard: CodeSearchGuard, path: str | None) -> Path:
    # Revue 5.2 — `if path` traitait `""` comme « absent » et listait la racine
    # primaire sans rien dire, alors que `read_file("")` refusait. Deux
    # réponses pour la même saisie.
    if path is not None and not isinstance(path, str):
        raise PathRefusedError(f"`path` doit être une chaîne, reçu {type(path).__name__}")
    if path is not None and not path.strip():
        raise PathRefusedError("`path` est vide — l'omettre pour partir de la racine autorisée")
    target = guard.resolve(path) if path is not None else guard.primary
    if not target.is_dir():
        raise PathRefusedError(f"{guard.display(target)} n'est pas un répertoire")
    return target


def _glob_matches(relative: str, name: str, pattern: str) -> bool:
    """Sémantique GLOB, et non ``fnmatch`` (revue 5.2).

    ``fnmatch`` traduit ``*`` en ``.*``, qui traverse les ``/`` : le
    ``**/router.py`` donné en exemple dans la description de l'outil ne
    matchait AUCUN fichier de premier niveau, et il n'existait aucun motif
    exprimant « ce répertoire seulement ». ``PurePath.full_match`` implémente
    la sémantique que la description promet.

    La règle, et elle est prévisible :

    - motif **sans** ``/`` → comparé au **nom** du fichier, où qu'il soit sous
      la racine de recherche (``*.py``, ``router.py``) ;
    - motif **avec** ``/`` → comparé au **chemin relatif** à cette racine, où
      ``*`` ne traverse pas les ``/`` et ``**/`` descend (``**/router.py``,
      ``pkg/*.py``).

    Pour restreindre la portée, c'est le paramètre ``path`` qui sert.
    """
    try:
        if "/" in pattern:
            return PurePosixPath(relative).full_match(pattern)
        return PurePosixPath(name).full_match(pattern)
    except ValueError:
        return False


def _trim_to_utf8_boundary(chunk: bytes) -> bytes:
    """Recule jusqu'à une frontière de caractère UTF-8.

    ``offset`` et ``max_bytes`` sont en OCTETS : un caractère multi-octets à
    cheval sur la coupure devenait ``U+FFFD`` des DEUX côtés — fin de la page
    N et début de la page N+1 (revue 5.2). Sur un dépôt dont les docstrings
    sont en français, chaque frontière perdait un caractère. Reculer d'au plus
    3 octets suffit ; un contenu binaire (erreur ailleurs qu'en toute fin) est
    rendu tel quel, parce que le remplacement y est l'information utile.
    """
    for back in range(4):
        end = len(chunk) - back
        if end <= 0:
            break
        candidate = chunk[:end]
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.end < len(candidate):
                return chunk
            continue
        return candidate
    return chunk


def _completeness(
    *, result_truncated: bool, what: str, report: ScanReport | None
) -> dict[str, Any]:
    """``truncated`` + la clé ``note``, qui n'apparaît QUE s'il y a troncature.

    Le point corrigé par la revue 5.2 : ``truncated`` ne reflétait que le
    plafond de RÉSULTATS. Un parcours coupé par le budget de balayage, par la
    profondeur, par un fichier trop gros ou par un ``EACCES`` rendait
    ``truncated: false`` sur un résultat incomplet.
    """
    reasons: list[str] = []
    if result_truncated:
        reasons.append(what)
    if report is not None:
        reasons.extend(report.reasons())
    if not reasons:
        return {"truncated": False}
    return {
        "truncated": True,
        "note": (
            "RÉSULTAT TRONQUÉ — " + " ; ".join(reasons) + ". Ce n'est PAS un résultat complet."
        ),
    }


def tool_list_directory(
    guard: CodeSearchGuard, limits: CodeSearchLimits, path: str | None
) -> dict[str, Any]:
    target = _start_dir(guard, path)
    entries: list[dict[str, Any]] = []
    truncated = False
    # Revue 5.2 — `sorted(target.iterdir())` matérialisait le répertoire
    # ENTIER (avec un `stat()` par entrée) avant de couper à `max_results` :
    # la borne coupait la sortie, pas le travail. On borne d'abord le nombre
    # d'entrées lues, puis on trie ce qu'on a.
    scan_budget = max(limits.max_results * 10, 1_000)
    children: list[Path] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if len(children) >= scan_budget:
                    truncated = True
                    break
                children.append(Path(entry.path))
    except OSError as exc:
        raise PathRefusedError(f"répertoire illisible : {exc.strerror or exc}") from exc
    children.sort(key=lambda p: (not p.is_dir(), p.name))
    for child in children:
        if _name_is_denied(child.name) or not guard.is_visible(child):
            continue
        if len(entries) >= limits.max_results:
            truncated = True
            break
        is_dir = child.is_dir()
        entries.append(
            {
                "path": guard.display(child),
                "name": child.name,
                "type": "directory" if is_dir else "file",
                "size_bytes": (None if is_dir else _size_of(child)),
            }
        )
    return {
        "path": guard.display(target),
        "entries": entries,
        "count": len(entries),
        **_completeness(
            result_truncated=truncated,
            what=f"seules les {limits.max_results} premières entrées",
            report=None,
        ),
    }


def tool_read_file(
    guard: CodeSearchGuard,
    limits: CodeSearchLimits,
    path: str,
    offset: int = 0,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    target = guard.resolve(path)
    if target.is_dir():
        raise PathRefusedError(
            f"{guard.display(target)} est un répertoire — utiliser `list_directory`"
        )
    if not target.is_file():
        raise PathRefusedError(f"fichier inexistant : {guard.display(target)}")
    budget = (
        limits.max_read_bytes
        if max_bytes is None
        else max(1, min(int(max_bytes), limits.max_read_bytes))
    )
    start = max(0, int(offset))
    try:
        total = target.stat().st_size
    except OSError as exc:
        raise PathRefusedError(f"fichier illisible : {exc.strerror or exc}") from exc
    # Revue 5.2 — un `offset` au-delà de la fin rendait `content: ""` avec
    # `truncated: false`, indiscernable d'un fichier vide. C'est une erreur que
    # le modèle peut corriger : la lui dire.
    if start > total:
        raise PathRefusedError(
            f"offset {start} au-delà de la fin de {guard.display(target)} ({total} octets)"
        )
    try:
        with target.open("rb") as handle:  # "rb" : jamais de mode d'écriture.
            handle.seek(start)
            chunk = handle.read(budget)
    except OSError as exc:
        raise PathRefusedError(f"fichier illisible : {exc.strerror or exc}") from exc
    if start + len(chunk) < total:
        chunk = _trim_to_utf8_boundary(chunk)
    truncated = start + len(chunk) < total
    return {
        "path": guard.display(target),
        "offset": start,
        "bytes_returned": len(chunk),
        "total_bytes": total,
        "next_offset": (start + len(chunk)) if truncated else None,
        # `errors="replace"` : un fichier binaire rend des caractères de
        # remplacement plutôt qu'une erreur. Le modèle voit alors qu'il a
        # ouvert quelque chose qui n'est pas du texte, ce qui est exactement
        # l'information utile. La coupure, elle, tombe sur une frontière de
        # caractère (`_trim_to_utf8_boundary`).
        "content": chunk.decode("utf-8", errors="replace"),
        **_completeness(
            result_truncated=truncated,
            what="rappeler `read_file` avec `offset=next_offset` pour la suite",
            report=None,
        ),
    }


def tool_find_files(
    guard: CodeSearchGuard,
    limits: CodeSearchLimits,
    pattern: str,
    path: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise PathRefusedError("`pattern` est obligatoire (glob, par ex. `**/router.py`)")
    start = _start_dir(guard, path)
    budget = _result_budget(limits, max_results)
    glob = pattern.strip()
    matches: list[str] = []
    truncated = False
    report = ScanReport()
    for candidate in _iter_files(
        guard, start, max_depth=limits.max_depth, budget=MAX_FILES_SCANNED, report=report
    ):
        relative = candidate.relative_to(start).as_posix()
        if not _glob_matches(relative, candidate.name, glob):
            continue
        if len(matches) >= budget:
            truncated = True
            break
        matches.append(guard.display(candidate))
    return {
        "root": guard.display(start),
        "pattern": glob,
        "matches": matches,
        "count": len(matches),
        "files_scanned": report.files_scanned,
        **_completeness(
            result_truncated=truncated,
            what=f"seuls les {budget} premiers fichiers",
            report=report,
        ),
    }


def _is_scannable(candidate: Path, report: ScanReport) -> bool:
    """Ce fichier peut-il être fouillé — et sinon, l'incomplétude est consignée."""
    if _suffix_is_skipped(candidate.name):
        return False
    # Revue 5.2 — un FIFO rend `st_size == 0`, passait le test de taille, et
    # `open("r")` bloquait indéfiniment en attente d'un écrivain.
    # `tool_read_file` filtrait déjà ce cas ; le balayage non.
    if not candidate.is_file():
        return False
    size = _size_of(candidate)
    if size is None:
        report.skipped_unreadable += 1
        return False
    if size > MAX_SCAN_FILE_BYTES:
        report.skipped_large += 1
        return False
    return True


def _search_one_file(
    guard: CodeSearchGuard,
    candidate: Path,
    regex: re.Pattern[str],
    matches: list[dict[str, Any]],
    budget: int,
) -> bool:
    """Ajoute les correspondances de ``candidate``. Rend ``True`` si le budget est atteint."""
    with candidate.open("r", encoding="utf-8", errors="replace") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not regex.search(line):
                continue
            if len(matches) >= budget:
                return True
            text = line.rstrip("\n")
            matches.append(
                {
                    "path": guard.display(candidate),
                    "line": lineno,
                    "text": text[:MAX_MATCH_LINE_CHARS],
                    "line_truncated": len(text) > MAX_MATCH_LINE_CHARS,
                }
            )
    return False


def _compile_search_pattern(pattern: str) -> re.Pattern[str]:
    if not isinstance(pattern, str) or not pattern.strip():
        raise PathRefusedError("`pattern` est obligatoire (expression régulière Python)")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise PathRefusedError(
            f"motif trop long ({len(pattern)} > {MAX_PATTERN_CHARS} caractères) — "
            "un motif pathologique bloque le serveur, `re` n'a pas de timeout"
        )
    if _NESTED_QUANTIFIER.search(pattern):
        raise PathRefusedError(
            "motif à quantificateurs imbriqués (par ex. `(a+)+`) — le backtracking y est "
            "exponentiel et `re` n'a pas de timeout. Réécrire le motif sans groupe quantifié "
            "contenant lui-même un quantificateur."
        )
    try:
        return re.compile(pattern)
    # `OverflowError` sur `a{4294967296}` (13 caractères, sous le plafond de
    # longueur) échappait au `except re.error` et sortait du contrat
    # `PathRefusedError` que ce module documente (revue 5.2).
    except (re.error, OverflowError, RecursionError) as exc:
        raise PathRefusedError(f"expression régulière invalide : {exc}") from exc


def tool_search_content(
    guard: CodeSearchGuard,
    limits: CodeSearchLimits,
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    max_results: int | None = None,
) -> dict[str, Any]:
    regex = _compile_search_pattern(pattern)
    start = _start_dir(guard, path)
    budget = _result_budget(limits, max_results)
    file_glob = (glob or "").strip()
    matches: list[dict[str, Any]] = []
    truncated = False
    report = ScanReport()
    for candidate in _iter_files(
        guard, start, max_depth=limits.max_depth, budget=MAX_FILES_SCANNED, report=report
    ):
        if file_glob and not _glob_matches(
            candidate.relative_to(start).as_posix(), candidate.name, file_glob
        ):
            continue
        if not _is_scannable(candidate, report):
            continue
        try:
            truncated = _search_one_file(guard, candidate, regex, matches, budget)
        except OSError:
            # Un fichier devenu illisible pendant le balayage n'est pas une
            # raison de faire échouer toute la recherche — mais l'incomplétude
            # qui en résulte est DITE.
            report.skipped_unreadable += 1
            continue
        if truncated:
            break
    return {
        "root": guard.display(start),
        "pattern": pattern,
        "file_glob": file_glob or None,
        "matches": matches,
        "count": len(matches),
        "files_scanned": report.files_scanned,
        **_completeness(
            result_truncated=truncated,
            what=f"seules les {budget} premières correspondances",
            report=report,
        ),
    }


def _result_budget(limits: CodeSearchLimits, requested: int | None) -> int:
    if requested is None:
        return limits.max_results
    return max(1, min(int(requested), limits.max_results))


def _size_of(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover — course avec une suppression
        return None


# ─── Surface MCP ─────────────────────────────────────────────────────────

_PATH_DESCRIPTION = (
    "Chemin absolu, ou relatif à la racine autorisée. Les chemins hors racine, "
    "les `..` et les liens symboliques qui sortent sont refusés."
)


def tool_definitions() -> list[types.Tool]:
    """Les 4 outils, tous en LECTURE. Aucun `exec`, aucun `env`, aucune écriture."""
    return [
        types.Tool(
            name="list_directory",
            description="Liste le contenu d'un répertoire du codebase (fichiers et sous-dossiers).",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string", "description": _PATH_DESCRIPTION}},
                "required": [],
            },
        ),
        types.Tool(
            name="read_file",
            description=(
                "Lit un fichier texte du codebase. La lecture est bornée : si `truncated` "
                "vaut true, rappeler avec `offset=next_offset` pour la suite. Un "
                "`offset` au-delà de la fin du fichier est refusé plutôt que rendu vide."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": _PATH_DESCRIPTION},
                    "offset": {"type": "integer", "description": "Octet de départ (défaut 0)."},
                    "max_bytes": {"type": "integer", "description": "Octets max à lire."},
                },
                "required": ["path"],
            },
        ),
        types.Tool(
            name="find_files",
            description=(
                "Cherche des fichiers par motif glob. Un motif sans `/` porte sur le NOM "
                "(`*.yaml` partout sous la racine) ; un motif avec `/` porte sur le "
                "chemin relatif (`**/router.py`, `pkg/*.py`). "
                "Préférer cet outil à une lecture exhaustive pour localiser du code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Motif glob. Sans `/`, il porte sur le nom du fichier "
                            "(`*.yaml`). Avec `/`, sur le chemin relatif, où `*` ne "
                            "traverse pas les `/` et `**/` descend (`**/service.py`)."
                        ),
                    },
                    "path": {"type": "string", "description": _PATH_DESCRIPTION},
                    "max_results": {"type": "integer", "description": "Nombre max de fichiers."},
                },
                "required": ["pattern"],
            },
        ),
        types.Tool(
            name="search_content",
            description=(
                "Cherche une expression régulière dans le contenu des fichiers (équivalent "
                "grep/ripgrep). Rend le chemin, le numéro de ligne et la ligne."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Expression régulière Python."},
                    "path": {"type": "string", "description": _PATH_DESCRIPTION},
                    "glob": {
                        "type": "string",
                        "description": "Restreint aux fichiers correspondant à ce glob.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Nombre max de correspondances.",
                    },
                },
                "required": ["pattern"],
            },
        ),
    ]


def _as_int(value: Any, field: str, *, default: int) -> int:
    """Coercition d'un entier DANS le contrat d'erreur du module (revue 5.2).

    ``int(arguments.get("offset") or 0)`` levait un ``ValueError`` ou un
    ``OverflowError`` nu sur ``"abc"`` / ``1e400`` — hors du
    :class:`PathRefusedError` que ce module documente comme son seul mode de
    refus exploitable.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise PathRefusedError(f"`{field}` doit être un entier, reçu un booléen")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PathRefusedError(f"`{field}` doit être un entier : {exc}") from exc


def _opt_int(value: Any, field: str) -> int | None:
    return None if value is None else _as_int(value, field, default=0)


def dispatch(
    guard: CodeSearchGuard, limits: CodeSearchLimits, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Route un appel d'outil. Lève :class:`PathRefusedError` ou ``ValueError``.

    Séparé de :func:`build_server` pour être testable sans protocole : les
    tests unitaires de la garde de chemin et des bornes appellent ici.
    """
    if name == "list_directory":
        return tool_list_directory(guard, limits, arguments.get("path"))
    if name == "read_file":
        path = arguments.get("path")
        if not isinstance(path, str):
            raise PathRefusedError("`path` est obligatoire")
        return tool_read_file(
            guard,
            limits,
            path,
            offset=_as_int(arguments.get("offset"), "offset", default=0),
            max_bytes=(
                None
                if arguments.get("max_bytes") is None
                else _as_int(arguments.get("max_bytes"), "max_bytes", default=0)
            ),
        )
    if name == "find_files":
        return tool_find_files(
            guard,
            limits,
            str(arguments.get("pattern") or ""),
            path=arguments.get("path"),
            max_results=_opt_int(arguments.get("max_results"), "max_results"),
        )
    if name == "search_content":
        return tool_search_content(
            guard,
            limits,
            str(arguments.get("pattern") or ""),
            path=arguments.get("path"),
            glob=arguments.get("glob"),
            max_results=_opt_int(arguments.get("max_results"), "max_results"),
        )
    raise ValueError(f"outil inconnu : {name}")


def build_server(guard: CodeSearchGuard, limits: CodeSearchLimits) -> Server[Any, Any]:
    """Le serveur MCP, prêt à pomper du JSON-RPC sur stdio."""
    server: Server[Any, Any] = Server(name=SERVER_NAME, version=SERVER_VERSION)

    # `type: ignore[no-untyped-call]` : le SDK MCP n'annote pas le décorateur
    # `list_tools()`. Le ignore est NOMMÉ et borné à cette ligne, pas posé sur
    # le module — même posture que le `construct_object` de `dev_catalog.py`.
    @server.list_tools()  # type: ignore[no-untyped-call]
    async def list_tools() -> list[types.Tool]:
        return tool_definitions()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Exécute un outil, ou LÈVE.

        Lever est le contrat : le SDK traduit l'exception en
        ``CallToolResult.isError=True``, que ``infra/mcp/client.py`` remonte
        en ``MCPToolError`` et que ``McpToolExecutor`` rend au modèle avec son
        message. Un refus de chemin est donc une information que l'agent peut
        exploiter — il corrige son chemin — et non un run mort.
        """
        payload = await asyncio.to_thread(dispatch, guard, limits, name, dict(arguments or {}))
        return [
            types.TextContent(
                type="text", text=json.dumps(payload, ensure_ascii=False, default=str)
            )
        ]

    return server


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="code_search",
        description="Serveur MCP stdio de lecture de code (Agentive, Story 5.2).",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "Répertoire autorisé. Répétable. AUCUN chemin hors de ces racines n'est "
            "lisible, symlinks compris. Réécrit à chaque appel depuis "
            "`AGENTIVE_DEV_CODE_ROOTS` par infra/mcp/policy.py."
        ),
    )
    parser.add_argument("--max-read-bytes", type=int, default=DEFAULT_MAX_READ_BYTES)
    parser.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    parser.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH)
    return parser.parse_args(list(argv) if argv is not None else None)


async def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    guard = CodeSearchGuard.from_paths(args.root)
    limits = CodeSearchLimits.clamped(
        max_read_bytes=args.max_read_bytes,
        max_results=args.max_results,
        max_depth=args.max_depth,
    )
    server = build_server(guard, limits)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":  # pragma: no cover — lancé en sous-processus
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
