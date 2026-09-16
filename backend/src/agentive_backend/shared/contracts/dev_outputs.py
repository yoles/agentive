"""Contrats de sortie des agents du Pôle Dev — Story 5.3 T4 (FR43).

**Pourquoi ce module existe.** La Story 5.1 a posé, sur demande de sa revue,
le seul contrôle de contrat de sortie du dépôt : un plan de délégation
incohérent est *nommé* dans ``metrics.per_node[].contract_problems`` au lieu
d'être stocké, streamé et rapporté comme un succès ordinaire. La docstring de
``_delegation_plan_problems`` annonçait alors que « un agent des Stories 5.2 →
5.6 qui déclare le même contrat sera vérifié sans une ligne de plus ». La
Story 5.2 a déclaré quatre clés et **n'a rien branché dessus** : son contrat
était donc déclaré, jamais tenu. Ce module généralise le point d'application
existant plutôt que d'en ouvrir un second.

**Le déclencheur est le contrat DÉCLARÉ, jamais le nom de l'agent.** C'est ce
qui rend le mécanisme héritable : un template qui déclare
``{approach, tradeoffs, risks, test_strategy}`` dans son ``output_contract.core``
est jugé contre ce contrat, qu'il s'appelle ``architect_analyst`` ou autrement.

**Pourquoi dans ``shared/contracts/``.** ``features.workflow_engine`` (qui
exécute) ne peut pas importer ``features.agent_registry`` (qui déclare) —
``.import-linter`` Contract 1. ``shared.contracts`` est la seule porte
sanctionnée, et c'est déjà la raison d'être de :mod:`.dev_roles`.

**Portée, et ce qui reste hors périmètre.** On vérifie ici les RÈGLES DURES
que les prompts énoncent, pas un JSON Schema général : la validation complète
contre ``output_contract`` reste déférée (posture Sprint 1, ``agent_node.
_best_effort_json``). On ne vérifie pas non plus l'intégrité
*référentielle* d'un ``approach_ref`` contre la sortie réelle de l'Analyste :
un validateur ne voit que la sortie du node courant, et coupler celui-ci à la
topologie du graphe pour un seul contrôle serait payer très cher une
vérification que le test E2E fait mieux — il lit les deux sorties depuis le
moteur.

**Signaler, ne pas corriger** (posture reprise de :mod:`.dev_roles`).
Réécrire une sortie incohérente serait décider à la place de l'agent ; la
laisser passer en silence serait la dégradation muette que ce dépôt refuse.
Entre les deux, on nomme.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final

from agentive_backend.shared.contracts.dev_roles import validate_delegation_plan

#: L'enveloppe que ``agent_node`` pose quand la réponse du modèle n'est pas du
#: JSON exploitable. Dupliquée depuis ``engine/agent_node.RAW_OUTPUT_KEY``
#: plutôt qu'importée : ``shared/`` ne peut pas dépendre de ``features/``.
RAW_OUTPUT_KEY: Final = "_raw"

#: Les deux seuls statuts qu'un agent du pôle peut déclarer. Même jeu que
#: celui des règles de routage ``terminal-output-status`` /
#: ``failed-output-status``.
_TERMINAL_STATUSES: Final = ("done", "failed")

_COMPLEXITY_LEVELS: Final = ("low", "medium", "high")
_SEVERITY_LEVELS: Final = ("low", "medium", "high")
_TEST_LEVELS: Final = ("unit", "integration", "e2e")


def _is_filled(value: object) -> bool:
    """``True`` si ``value`` est une chaîne qui dit réellement quelque chose.

    Un champ présent mais blanc est le contournement le moins cher d'une règle
    de présence, et c'est celui qu'un modèle produit spontanément quand il n'a
    rien à dire.
    """
    return isinstance(value, str) and bool(value.strip())


def _status_problems(status: object) -> list[str]:
    if status not in _TERMINAL_STATUSES:
        return [f"status invalide : {status!r} (attendu 'done' ou 'failed')"]
    return []


def _blocking_question_problems(output: Mapping[str, Any]) -> list[str]:
    if not _is_filled(output.get("blocking_question")):
        return [
            "un status 'failed' doit porter une `blocking_question` non vide — "
            "un refus sans question est un refus sans recours"
        ]
    return []


def _entries(value: object) -> list[Mapping[str, Any]]:
    """Les entrées exploitables d'un tableau d'objets, les autres ignorées ici.

    Les formes aberrantes sont signalées par l'appelant, qui sait quel champ
    il inspecte : les faire remonter deux fois produirait deux messages pour
    un seul défaut.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def validate_architect_approach(output: object) -> list[str]:
    """Les incohérences d'une approche d'Architect Analyst. Vide = conforme.

    Les règles vérifiées sont celles que le prompt énonce comme dures, et
    l'AC3 de la Story 5.3 en impose deux qui ne vont pas de soi :

    * la **complexité estimée** est un champ, pas une tournure de phrase ;
    * **au moins une alternative écartée** avec sa raison. Sans cette règle,
      un Analyste qui recommande la première idée venue rend une sortie
      formellement conforme — le champ ``tradeoffs`` existe, il est juste vide
      de tout arbitrage, et c'est précisément ce que l'AC demande de constater.

    Posture défensive assumée : ``output`` vient d'un ``_best_effort_json`` sur
    du texte de modèle. Toute forme est possible, aucune ne lève.
    """
    if not isinstance(output, Mapping):
        return [f"la sortie n'est pas un objet JSON (reçu {type(output).__name__})"]

    problems = _status_problems(output.get("status"))
    approach = output.get("approach")
    tradeoffs = output.get("tradeoffs")
    risks = output.get("risks")
    test_strategy = output.get("test_strategy")

    if output.get("status") == "failed":
        if (
            (isinstance(approach, Mapping) and approach)
            or (isinstance(tradeoffs, list) and tradeoffs)
            or (isinstance(risks, list) and risks)
            or (isinstance(test_strategy, Mapping) and test_strategy)
        ):
            problems.append(
                "un status 'failed' doit rendre `approach`, `tradeoffs`, `risks` et "
                "`test_strategy` vides — une approche à moitié rendue est pire qu'un refus"
            )
        problems.extend(_blocking_question_problems(output))
        return problems

    problems.extend(_approach_problems(approach))
    problems.extend(_tradeoff_problems(tradeoffs))
    problems.extend(_risk_problems(risks))
    problems.extend(_test_strategy_problems(test_strategy))
    return problems


def _approach_problems(approach: object) -> list[str]:
    if not isinstance(approach, Mapping):
        return [f"approach absent ou mal formé (reçu {type(approach).__name__})"]

    problems: list[str] = []
    if not _is_filled(approach.get("summary")):
        problems.append("approach.summary doit dire en une phrase ce qui est proposé")
    complexity = approach.get("complexity")
    if complexity not in _COMPLEXITY_LEVELS:
        problems.append(
            f"approach.complexity invalide : {complexity!r} "
            f"(attendu l'un de {', '.join(_COMPLEXITY_LEVELS)})"
        )

    steps = approach.get("steps")
    entries = _entries(steps)
    if not isinstance(steps, list) or not steps:
        problems.append(
            "approach.steps doit porter au moins une étape — une approche sans étape "
            "n'est pas exécutable par un Producteur"
        )
        return problems
    if len(entries) != len(steps):
        problems.append("chaque entrée de approach.steps doit être un objet")
    ids = [step.get("id") for step in entries]
    filled = [step_id for step_id in ids if _is_filled(step_id)]
    if len(filled) != len(entries) or len(set(filled)) != len(filled):
        # Les `id` d'étape sont ce que le Producteur référence dans
        # `code_diffs[].approach_ref` : deux homonymes rendent la traçabilité
        # de l'AC3 fausse sans que rien ne casse.
        problems.append(
            "chaque entrée de approach.steps doit porter un `id` non vide et unique — "
            "c'est la clé que `code_diffs[].approach_ref` référence"
        )
    return problems


def _tradeoff_problems(tradeoffs: object) -> list[str]:
    if not isinstance(tradeoffs, list):
        return [f"tradeoffs absent ou mal formé (reçu {type(tradeoffs).__name__})"]

    problems: list[str] = []
    entries = _entries(tradeoffs)
    if len(entries) != len(tradeoffs):
        problems.append("chaque entrée de tradeoffs doit être un objet")

    chosen = [item for item in entries if item.get("chosen") is True]
    rejected = [item for item in entries if item.get("chosen") is False]
    if len(chosen) != 1:
        problems.append(
            f"tradeoffs doit retenir exactement une option (`chosen: true`), {len(chosen)} trouvée(s)"
        )
    if not rejected:
        problems.append(
            "tradeoffs doit porter au moins une alternative ÉCARTÉE (`chosen: false`) — "
            "une recommandation sans option rejetée n'est pas un arbitrage"
        )
    for item in entries:
        if not _is_filled(item.get("option")):
            problems.append("chaque tradeoff doit nommer son `option`")
        if not _is_filled(item.get("rationale")):
            problems.append(
                f"le tradeoff {item.get('option')!r} n'énonce aucune raison — "
                "« écartée » sans « pourquoi » ne documente rien"
            )
    return problems


def _risk_problems(risks: object) -> list[str]:
    if not isinstance(risks, list):
        return [f"risks absent ou mal formé (reçu {type(risks).__name__})"]

    problems: list[str] = []
    entries = _entries(risks)
    if len(entries) != len(risks):
        problems.append("chaque entrée de risks doit être un objet")
    for item in entries:
        if not _is_filled(item.get("risk")):
            problems.append("chaque risque doit être énoncé (`risk`)")
        if item.get("severity") not in _SEVERITY_LEVELS:
            problems.append(
                f"severity invalide pour {item.get('risk')!r} : {item.get('severity')!r} "
                f"(attendu l'un de {', '.join(_SEVERITY_LEVELS)})"
            )
        if not _is_filled(item.get("mitigation")):
            problems.append(
                f"le risque {item.get('risk')!r} n'a pas de `mitigation` — "
                "un risque sans parade est une inquiétude, pas une analyse"
            )
    return problems


def _test_strategy_problems(test_strategy: object) -> list[str]:
    if not isinstance(test_strategy, Mapping):
        return [f"test_strategy absent ou mal formé (reçu {type(test_strategy).__name__})"]

    problems: list[str] = []
    levels = test_strategy.get("levels")
    if not isinstance(levels, list) or not levels:
        problems.append("test_strategy.levels doit nommer au moins un niveau de test")
    else:
        unknown = sorted({str(level) for level in levels if level not in _TEST_LEVELS})
        if unknown:
            problems.append(
                f"niveaux de test inconnus : {', '.join(unknown)} "
                f"(attendu parmi {', '.join(_TEST_LEVELS)})"
            )
    if not isinstance(test_strategy.get("focus"), list):
        problems.append("test_strategy.focus doit lister ce que les tests doivent garder")
    return problems


def validate_producer_output(output: object) -> list[str]:
    """Les incohérences d'une sortie de Code Producer. Vide = conforme.

    La règle qui porte l'AC3 est ``code_diffs[].approach_ref`` : c'est elle
    qui distingue « il s'est appuyé sur l'approche » de « il a produit du code
    pendant qu'une approche existait ». Sa contrepartie est tout aussi
    exigée : un Producteur qui ne peut pas couvrir une étape doit le
    **signaler** (``unaddressed``) plutôt qu'improviser — donc une production
    vide ACCOMPAGNÉE d'un écart déclaré est conforme, et c'est seulement
    l'absence des deux qui est un défaut.

    Ce que ce validateur ne fait PAS : vérifier qu'un ``approach_ref`` existe
    réellement dans l'approche reçue. Voir la docstring du module.
    """
    if not isinstance(output, Mapping):
        return [f"la sortie n'est pas un objet JSON (reçu {type(output).__name__})"]

    problems = _status_problems(output.get("status"))
    code_diffs = output.get("code_diffs")
    tests = output.get("tests")
    docs_snippets = output.get("docs_snippets")

    for name, value in (
        ("code_diffs", code_diffs),
        ("tests", tests),
        ("docs_snippets", docs_snippets),
    ):
        if not isinstance(value, list):
            problems.append(f"{name} absent ou mal formé (reçu {type(value).__name__})")

    if output.get("status") == "failed":
        if any(isinstance(value, list) and value for value in (code_diffs, tests, docs_snippets)):
            problems.append(
                "un status 'failed' doit rendre `code_diffs`, `tests` et `docs_snippets` vides"
            )
        problems.extend(_blocking_question_problems(output))
        return problems

    problems.extend(_diff_problems(code_diffs))
    problems.extend(_test_entry_problems(tests))
    unaddressed = output.get("unaddressed")
    problems.extend(_unaddressed_problems(unaddressed))

    has_diffs = isinstance(code_diffs, list) and bool(code_diffs)
    has_gaps = isinstance(unaddressed, list) and bool(unaddressed)
    if not has_diffs and not has_gaps:
        problems.append(
            "un status 'done' sans aucun `code_diffs` et sans aucun `unaddressed` ne décrit "
            "aucun travail — produire, ou dire ce qui a empêché de produire"
        )
    return problems


def _diff_problems(code_diffs: object) -> list[str]:
    problems: list[str] = []
    entries = _entries(code_diffs)
    if isinstance(code_diffs, list) and len(entries) != len(code_diffs):
        problems.append("chaque entrée de code_diffs doit être un objet")
    for item in entries:
        label = item.get("path") if _is_filled(item.get("path")) else "<sans chemin>"
        if not _is_filled(item.get("path")):
            problems.append("chaque code_diff doit porter le `path` du fichier visé")
        if not _is_filled(item.get("diff")):
            problems.append(
                f"le code_diff {label!r} ne porte aucun `diff` — un livrable annoncé et non livré"
            )
        if not _is_filled(item.get("approach_ref")):
            # LA règle de l'AC3 : la traçabilité diff → étape d'approche.
            problems.append(
                f"le code_diff {label!r} ne porte pas d'`approach_ref` — chaque modification "
                "doit nommer l'étape de l'approche qui la justifie"
            )
    return problems


def _test_entry_problems(tests: object) -> list[str]:
    problems: list[str] = []
    entries = _entries(tests)
    if isinstance(tests, list) and len(entries) != len(tests):
        problems.append("chaque entrée de tests doit être un objet")
    for item in entries:
        if not _is_filled(item.get("path")):
            problems.append("chaque test doit porter son `path`")
        if item.get("level") not in _TEST_LEVELS:
            problems.append(
                f"level invalide pour le test {item.get('path')!r} : {item.get('level')!r} "
                f"(attendu l'un de {', '.join(_TEST_LEVELS)})"
            )
    return problems


def _unaddressed_problems(unaddressed: object) -> list[str]:
    if unaddressed is None:
        return []
    if not isinstance(unaddressed, list):
        return [f"unaddressed mal formé (reçu {type(unaddressed).__name__})"]
    problems: list[str] = []
    for item in _entries(unaddressed):
        if not _is_filled(item.get("item")):
            problems.append("chaque `unaddressed` doit nommer ce qui n'a pas été traité")
        if not _is_filled(item.get("reason")):
            problems.append(
                f"l'écart {item.get('item')!r} n'a pas de `reason` — signaler sans dire "
                "pourquoi ne vaut pas mieux qu'improviser"
            )
    return problems


#: Contrat déclaré → validateur. Les clés sont les champs que le template doit
#: déclarer dans son ``output_contract.core`` pour que la règle s'applique ;
#: un template qui en déclare un SUR-ENSEMBLE est jugé (le Dev Lead déclare
#: ``status`` en plus de ``plan`` et ``delegations``).
#:
#: Ordre significatif : le PREMIER contrat couvert gagne. Aucun des trois
#: n'est un sous-ensemble d'un autre aujourd'hui, et le test
#: ``test_the_registry_has_no_ambiguous_contract`` garde cette propriété — sans
#: quoi l'ordre déciderait silencieusement d'un verdict.
_VALIDATORS: Final[tuple[tuple[frozenset[str], Callable[[object], list[str]]], ...]] = (
    (frozenset({"plan", "delegations"}), validate_delegation_plan),
    (
        frozenset({"approach", "tradeoffs", "risks", "test_strategy"}),
        validate_architect_approach,
    ),
    (frozenset({"code_diffs", "tests", "docs_snippets"}), validate_producer_output),
)


def declared_contracts() -> tuple[frozenset[str], ...]:
    """Les jeux de clés reconnus, pour les tests et la documentation."""
    return tuple(keys for keys, _ in _VALIDATORS)


def contract_problems(node_output: object, *, declared_core: Iterable[object]) -> list[str]:
    """Les incohérences de ``node_output`` au regard du contrat qu'il DÉCLARE.

    ``declared_core`` est l'ensemble des clés de ``output_contract.core`` du
    template. Un template dont le contrat n'est reconnu par aucune entrée du
    registre n'est pas jugé — ni faussement conforme, ni faussement fautif.

    Une sortie non parsable (enveloppe ``_raw``) ne rend rien : elle est déjà
    signalée par le repli lui-même et par la règle de routage
    ``no-parsable-output`` à 0.9. Le redire ici dirait deux fois la même chose.
    """
    if isinstance(node_output, Mapping) and RAW_OUTPUT_KEY in node_output:
        return []
    core = {key for key in declared_core if isinstance(key, str)}
    for required, validator in _VALIDATORS:
        if required <= core:
            return validator(node_output)
    return []


__all__ = [
    "contract_problems",
    "declared_contracts",
    "validate_architect_approach",
    "validate_producer_output",
]
