"""AR44 / règle d'or #9 — test STRUCTUREL : toute surface qui compose un
prompt enveloppe ses entrées externes (Story 5.0 AC3).

Pourquoi ce test existe plutôt qu'une consigne. Le dépôt compte plusieurs
constructeurs de prompt, et l'omission sur l'un d'eux — `PlaygroundService`,
qui envoie un corps HTTP verbatim au LLM — a survécu **deux epics entières**.
Elle était consignée deux fois (`epics.md`, Change Log de la Story 4.2),
portée par aucune story, et n'a été retrouvée que par hasard, par le finding
BS4 de la revue du lot 4.9→4.13.

La Story 5.0 ajoute la **quatrième** surface (l'exécuteur d'outils). Une
consigne de plus n'aurait rien changé : c'est la quatrième fois qu'on
l'écrit. Ce test fait échouer la suite dès qu'une cinquième apparaît sans se
déclarer — ce qui est le seul mécanisme dont on ait la preuve qu'il marche.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "agentive_backend"

#: Modules qui composent un prompt à partir d'entrées externes et DOIVENT
#: donc envelopper. Ajouter une surface sans l'inscrire ici fait échouer le
#: test ci-dessous — c'est le point.
WRAPPING_SURFACES = {
    "features/workflow_engine/engine/agent_node.py",
    "features/workflow_engine/engine/hybrid_router.py",
    "features/workflow_engine/engine/handoff.py",
    "infra/mcp/tool_executor.py",
}

#: Modules qui appellent un LLM SANS envelopper, avec la raison et le
#: dossier qui les porte. Toute entrée ici est une dette, pas une dispense.
KNOWN_UNWRAPPED = {
    # `PlaygroundService` envoie `json.dumps(arguments)` — un corps HTTP
    # entièrement contrôlé par l'appelant — verbatim au LLM. C'est une AC
    # VIOLÉE de la Story 2.2 (`done`), retrouvée par la revue 4.9→4.13 (BS4)
    # et portée depuis par la **Story 9.7**. Elle reste listée ici tant que
    # 9.7 n'est pas faite, pour que ce test dise la vérité sur l'état réel
    # plutôt que sur l'état souhaité.
    "features/playground/service.py": "Story 9.7",
}

#: Modules qui atteignent le LLM sans composer de prompt : la plomberie du
#: routeur, les politiques d'erreur, et la boucle d'outils elle-même — qui
#: transporte des messages composés ailleurs et n'en fabrique aucun.
NOT_PROMPT_BUILDERS = {
    "shared/llm/router.py",
    "shared/llm/tool_loop.py",
    "features/workflow_engine/domain/error_policy.py",
    "features/workflow_engine/domain/provider_chain.py",
}

#: Les façons d'atteindre un LLM dans ce dépôt. Le détecteur ne cherchait que
#: `.complete(` — ce qui était exact tant qu'il n'existait qu'une seule voie.
#:
#: La Story 5.0 en a ouvert une seconde et ne l'a pas déclarée (revue P20) :
#: `run_tool_loop(complete=...)` reçoit son appelant par INJECTION, donc un
#: module qui compose un prompt et le confie à la boucle ne contient aucun
#: `.complete(` et restait totalement invisible. La cinquième surface pouvait
#: donc apparaître sans faire échouer la moitié « non classée » — précisément
#: ce que ce fichier dit exister pour empêcher, écrit par la story qui a
#: introduit l'indirection.
LLM_ENTRY_POINTS = (
    ".complete(",
    "run_tool_loop(",
)


def _modules_issuing_completions() -> set[str]:
    found = set()
    for path in SRC.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if any(entry in source for entry in LLM_ENTRY_POINTS):
            found.add(str(path.relative_to(SRC)))
    return found


def test_every_prompt_building_surface_is_accounted_for() -> None:
    """Aucune surface ne peut apparaître sans être classée.

    C'est la moitié qui mord : un futur développeur qui ajoute un
    constructeur de prompt doit venir ici et choisir explicitement entre
    « ça enveloppe » et « c'est une dette portée par telle story ». Il ne
    peut pas simplement ne rien faire.
    """
    classified = WRAPPING_SURFACES | set(KNOWN_UNWRAPPED) | NOT_PROMPT_BUILDERS
    unclassified = _modules_issuing_completions() - classified

    assert not unclassified, (
        "Nouvelle surface appelant un LLM, non classée : "
        f"{sorted(unclassified)}. Si elle compose un prompt à partir d'entrées "
        "externes, elle DOIT appeler wrap_external_input et rejoindre "
        "WRAPPING_SURFACES. Sinon, justifiez-la dans NOT_PROMPT_BUILDERS ou "
        "KNOWN_UNWRAPPED avec la story qui porte la dette."
    )


@pytest.mark.parametrize("relative", sorted(WRAPPING_SURFACES))
def test_a_declared_wrapping_surface_really_wraps(relative: str) -> None:
    """L'autre moitié : être inscrit dans la liste ne suffit pas, il faut
    que le code le fasse."""
    source = (SRC / relative).read_text(encoding="utf-8")

    assert "wrap_external_input(" in source, (
        f"{relative} est déclaré comme enveloppant ses entrées externes mais "
        "n'appelle jamais wrap_external_input."
    )


def test_the_detector_sees_every_way_this_repo_reaches_a_model() -> None:
    """Le détecteur lui-même est testé (revue P20).

    Sa faiblesse n'était pas une surface oubliée mais une VOIE oubliée : il
    cherchait un littéral, et la Story 5.0 a ajouté une indirection que ce
    littéral ne rencontre jamais. Ce test épingle l'ensemble des voies
    connues, pour qu'en ajouter une troisième sans l'inscrire se voie.
    """
    # `shared/llm/tool_loop.py` est la preuve vivante du trou : il compose et
    # transporte tout prompt multi-tours du dépôt, et ne contient pas
    # `.complete(`. Il DOIT être découvert — puis classé.
    discovered = _modules_issuing_completions()

    assert "shared/llm/tool_loop.py" in discovered, (
        "le détecteur ne voit pas la boucle d'outils : la voie "
        "`run_tool_loop(` a disparu de LLM_ENTRY_POINTS."
    )
    assert "features/playground/service.py" in discovered
    assert "features/workflow_engine/engine/agent_node.py" in discovered


@pytest.mark.parametrize(("relative", "story"), sorted(KNOWN_UNWRAPPED.items()))
def test_a_known_gap_is_still_a_gap_and_not_silently_fixed(relative: str, story: str) -> None:
    """Quand la dette est fermée, ce test échoue — et c'est voulu.

    Il force à retirer l'entrée de `KNOWN_UNWRAPPED` et à la promouvoir dans
    `WRAPPING_SURFACES`, plutôt que de laisser une liste d'exceptions décrire
    un état qui n'existe plus. Une liste d'exceptions périmée est exactement
    ce qui a permis à l'omission d'origine de passer inaperçue si longtemps.
    """
    source = (SRC / relative).read_text(encoding="utf-8")

    assert "wrap_external_input(" not in source, (
        f"{relative} enveloppe désormais ses entrées — {story} est faite ? "
        "Déplacez-le de KNOWN_UNWRAPPED vers WRAPPING_SURFACES."
    )
