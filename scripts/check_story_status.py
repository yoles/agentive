#!/usr/bin/env python3
"""Vérifie que l'en-tête `Status:` de chaque story correspond à sa clé dans
`sprint-status.yaml`.

Pourquoi ce script existe. Ce défaut est apparu DEUX fois :
`3-5-push-memory-proactif.md` affichait `review` alors que le sprint plan le
marquait `done` (corrigé à la main par la rétrospective Epic 3), puis
`4-3-orchestration-hybride.md` exactement pareil une epic plus tard. La
rétrospective Epic 3 avait corrigé l'instance sans poser de garde-fou ; le
défaut est donc revenu à l'identique. C'est la démonstration la plus nette du
dossier qu'une action de rétro qui traite un symptôme sans toucher au système
ne vaut rien — d'où ce script plutôt qu'une bonne intention (action 7 de la
rétrospective Epic 4).

Le fichier de story est l'artefact qu'un humain lit ; `sprint-status.yaml` est
celui que le workflow lit. Quand les deux divergent, l'un des deux ment, et
rien ne dit lequel.

Usage : `make check-stories` (ou `python3 scripts/check_story_status.py`).
Sortie 0 si tout concorde, 1 sinon avec le détail des divergences.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = REPO_ROOT / "_bmad-output" / "implementation-artifacts"
SPRINT_STATUS = ARTIFACTS / "sprint-status.yaml"

#: Une clé de story : `<epic>-<num>-<slug>`. Exclut les clés `epic-N` et
#: `epic-N-retrospective`, qui n'ont pas de fichier de story.
STORY_KEY = re.compile(r"^\s{2}(\d+-\d+-[a-z0-9-]+):\s*([a-z-]+)\s*$")
#: `Status: <valeur>` dans les premières lignes d'un fichier de story.
STATUS_LINE = re.compile(r"^Status:\s*(\S+)\s*$", re.MULTILINE)


def main() -> int:
    if not SPRINT_STATUS.exists():
        print(f"introuvable : {SPRINT_STATUS}", file=sys.stderr)
        return 1

    divergences: list[str] = []
    casse: list[str] = []
    orphelins: list[str] = []
    checked = 0

    for line in SPRINT_STATUS.read_text(encoding="utf-8").splitlines():
        match = STORY_KEY.match(line)
        if not match:
            continue
        key, yaml_status = match.group(1), match.group(2)
        story_file = ARTIFACTS / f"{key}.md"
        if not story_file.exists():
            # Légitime tant que la story est `backlog` : elle n'existe alors
            # que dans `epics.md`. À tout autre statut, le fichier manque.
            if yaml_status != "backlog":
                orphelins.append(f"  {key}: {yaml_status} — aucun fichier {key}.md")
            continue
        header = STATUS_LINE.search(story_file.read_text(encoding="utf-8"))
        if header is None:
            orphelins.append(f"  {key}: fichier présent mais sans en-tête `Status:`")
            continue
        checked += 1
        file_status = header.group(1)
        # Comparaison insensible à la casse : `Done` et `done` sont le MÊME
        # statut, pas une contradiction. La casse est signalée à part, comme
        # un défaut de forme — confondre les deux ferait crier ce contrôle
        # sur du cosmétique et apprendrait à l'ignorer.
        if file_status.casefold() != yaml_status.casefold():
            divergences.append(f"  {key}: fichier={file_status!r} ≠ sprint-status={yaml_status!r}")
        elif file_status != file_status.lower():
            casse.append(f"  {key}: en-tête {file_status!r}, convention attendue {file_status.lower()!r}")

    if divergences or casse or orphelins:
        if divergences:
            print("Divergences de statut story ↔ sprint-status :", file=sys.stderr)
            print("\n".join(divergences), file=sys.stderr)
        if casse:
            print("Casse non conforme (même statut, forme différente) :", file=sys.stderr)
            print("\n".join(casse), file=sys.stderr)
        if orphelins:
            print("Incohérences de présence :", file=sys.stderr)
            print("\n".join(orphelins), file=sys.stderr)
        print(
            f"\n{len(divergences)} divergence(s), {len(casse)} défaut(s) de casse, "
            f"{len(orphelins)} incohérence(s) sur {checked} story(ies) vérifiée(s).",
            file=sys.stderr,
        )
        return 1

    print(f"✅ {checked} stories vérifiées, statuts cohérents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
