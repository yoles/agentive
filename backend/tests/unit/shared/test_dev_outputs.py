"""Contrats de sortie des agents Producteur du Pôle Dev — Story 5.3 T4.

Ce module garde une propriété que le dépôt n'avait jamais tenue : **un
contrat DÉCLARÉ est un contrat VÉRIFIÉ**. La validation JSON Schema générale
contre ``output_contract`` reste déférée (posture Sprint 1) ; ce qui est
vérifié ici, ce sont les règles dures que les prompts énoncent — celles dont
la violation produit une sortie qui *ressemble* à un livrable et qui n'en est
pas un.

La 5.1 a livré ce mécanisme pour le Dev Lead après que la revue eut trouvé
que ``validate_delegation_plan`` n'avait **aucun appelant de production**. La
5.2 a déclaré quatre clés et n'a rien branché dessus. Ces tests existent pour
que la troisième fois n'ait pas lieu.
"""

from __future__ import annotations

from typing import Any

from agentive_backend.shared.contracts.dev_outputs import (
    contract_problems,
    validate_architect_approach,
    validate_producer_output,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Échantillons — construits À LA MAIN, jamais dérivés du validateur
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# La revue de la 5.2 a dû réécrire huit tests qui dérivaient leurs échantillons
# de leurs propres motifs — ils passaient quoi que fasse le code. Les deux
# littéraux ci-dessous sont donc écrits en toutes lettres.


def _approach() -> dict[str, Any]:
    return {
        "status": "done",
        "summary": "Ajouter le module M13 en suivant le gabarit des modules existants.",
        "approach": {
            "summary": "Créer le package avec router/service/schemas, brancher le repo partagé.",
            "complexity": "medium",
            "steps": [
                {"id": "a1", "title": "Créer le package", "rationale": "gabarit M2-M12"},
                {"id": "a2", "title": "Brancher le repo", "rationale": "règle d'or #4"},
            ],
        },
        "tradeoffs": [
            {
                "option": "Suivre le gabarit existant",
                "pros": ["cohérence"],
                "cons": ["peu de marge"],
                "chosen": True,
                "rationale": "douze modules l'utilisent déjà",
            },
            {
                "option": "Repartir d'une structure neuve",
                "pros": ["liberté"],
                "cons": ["divergence"],
                "chosen": False,
                "rationale": "aucun bénéfice qui paie la divergence",
            },
        ],
        "risks": [
            {
                "risk": "Collision de nom d'event",
                "severity": "medium",
                "mitigation": "valider le préfixe avec validate_event_type",
            }
        ],
        "test_strategy": {
            "levels": ["unit", "integration"],
            "focus": ["le routeur refuse un payload inconnu"],
        },
    }


def _production() -> dict[str, Any]:
    return {
        "status": "done",
        "summary": "Squelette du module M13.",
        "code_diffs": [
            {
                "path": "backend/src/agentive_backend/features/m13/router.py",
                "diff": "+from fastapi import APIRouter\n",
                "approach_ref": "a1",
                "rationale": "étape a1 : créer le package",
            }
        ],
        "tests": [
            {
                "path": "backend/tests/unit/m13/test_router.py",
                "level": "unit",
                "content": "def test_x() -> None: ...",
                "covers": ["a1"],
            }
        ],
        "docs_snippets": [{"target": "docs/runbooks/m13.md", "content": "# M13"}],
        "unaddressed": [],
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Architect Analyst
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_a_conforming_approach_reports_nothing() -> None:
    assert validate_architect_approach(_approach()) == []


def test_an_approach_without_a_rejected_alternative_is_named() -> None:
    """AC3 — « au moins une alternative écartée avec sa raison ».

    Sans cette règle, un Analyste qui recommande la première idée venue rend
    une sortie formellement conforme : le champ `tradeoffs` existe, il est
    juste vide de tout arbitrage.
    """
    output = _approach()
    output["tradeoffs"] = [tradeoff for tradeoff in output["tradeoffs"] if tradeoff["chosen"]]
    problems = validate_architect_approach(output)
    assert problems
    assert any("alternative" in problem for problem in problems)


def test_an_approach_that_chooses_nothing_is_named() -> None:
    """Un arbitrage sans option retenue n'est pas un arbitrage."""
    output = _approach()
    for tradeoff in output["tradeoffs"]:
        tradeoff["chosen"] = False
    assert any("exactement une" in problem for problem in validate_architect_approach(output))


def test_an_approach_that_chooses_twice_is_named() -> None:
    output = _approach()
    for tradeoff in output["tradeoffs"]:
        tradeoff["chosen"] = True
    assert any("exactement une" in problem for problem in validate_architect_approach(output))


def test_a_rejected_alternative_without_a_reason_is_named() -> None:
    """« Écartée » sans « pourquoi » ne documente rien."""
    output = _approach()
    output["tradeoffs"][1]["rationale"] = "   "
    assert any("raison" in problem for problem in validate_architect_approach(output))


def test_a_missing_complexity_estimate_is_named() -> None:
    """AC3 nomme la complexité estimée explicitement."""
    output = _approach()
    del output["approach"]["complexity"]
    assert any("complexity" in problem for problem in validate_architect_approach(output))


def test_an_out_of_range_complexity_is_named() -> None:
    output = _approach()
    output["approach"]["complexity"] = "trivial"
    assert any("complexity" in problem for problem in validate_architect_approach(output))


def test_an_approach_without_steps_is_named() -> None:
    output = _approach()
    output["approach"]["steps"] = []
    assert any("étape" in problem for problem in validate_architect_approach(output))


def test_two_steps_sharing_an_id_are_named() -> None:
    """Les `id` d'étape sont ce que le Producteur référence.

    Deux étapes homonymes rendent un `approach_ref` ambigu, donc la traçabilité
    de l'AC3 fausse — sans que rien ne casse.
    """
    output = _approach()
    output["approach"]["steps"][1]["id"] = "a1"
    assert any("unique" in problem for problem in validate_architect_approach(output))


def test_a_risk_without_mitigation_is_named() -> None:
    output = _approach()
    output["risks"][0]["mitigation"] = ""
    assert any("mitigation" in problem for problem in validate_architect_approach(output))


def test_a_failed_approach_must_carry_a_blocking_question() -> None:
    """Mirror exact de la règle du Dev Lead : un refus sans question est un
    refus sans recours."""
    output = {
        "status": "failed",
        "approach": {},
        "tradeoffs": [],
        "risks": [],
        "test_strategy": {},
    }
    assert any("blocking_question" in problem for problem in validate_architect_approach(output))
    output["blocking_question"] = "Quel module doit être étendu ?"
    assert validate_architect_approach(output) == []


def test_a_failed_approach_that_still_proposes_something_is_named() -> None:
    output = _approach()
    output["status"] = "failed"
    output["blocking_question"] = "?"
    assert any("failed" in problem for problem in validate_architect_approach(output))


def test_a_non_object_output_never_raises() -> None:
    """Posture défensive : l'entrée vient d'un `_best_effort_json` sur du texte
    de modèle. Toute forme est possible, aucune ne lève."""
    for candidate in (None, [], "texte", 3):
        assert validate_architect_approach(candidate)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Code Producer
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_a_conforming_production_reports_nothing() -> None:
    assert validate_producer_output(_production()) == []


def test_a_diff_without_an_approach_reference_is_named() -> None:
    """AC3 — « le Producteur s'appuie EXPLICITEMENT sur cette approche ».

    C'est la règle qui distingue « il a lu l'approche » de « il a produit du
    code pendant qu'une approche existait ».
    """
    output = _production()
    del output["code_diffs"][0]["approach_ref"]
    assert any("approach_ref" in problem for problem in validate_producer_output(output))


def test_a_diff_with_a_blank_approach_reference_is_named() -> None:
    output = _production()
    output["code_diffs"][0]["approach_ref"] = "  "
    assert any("approach_ref" in problem for problem in validate_producer_output(output))


def test_a_diff_without_a_path_is_named() -> None:
    output = _production()
    output["code_diffs"][0]["path"] = ""
    assert any("path" in problem for problem in validate_producer_output(output))


def test_a_diff_without_content_is_named() -> None:
    """Un `code_diffs[]` vide est un livrable annoncé et non livré."""
    output = _production()
    output["code_diffs"][0]["diff"] = ""
    assert any("diff" in problem for problem in validate_producer_output(output))


def test_a_test_with_an_unknown_level_is_named() -> None:
    output = _production()
    output["tests"][0]["level"] = "smoke"
    assert any("level" in problem for problem in validate_producer_output(output))


def test_a_done_production_that_produced_nothing_and_blocked_on_nothing_is_named() -> None:
    """« Fait », zéro diff, zéro point non traité : la sortie ne décrit aucun
    travail. C'est le mensonge le moins cher à produire."""
    output = _production()
    output["code_diffs"] = []
    output["unaddressed"] = []
    assert any("aucun" in problem for problem in validate_producer_output(output))


def test_a_done_production_with_no_diff_but_a_declared_gap_is_accepted() -> None:
    """La contrepartie : signaler plutôt qu'improviser est le comportement
    EXIGÉ par l'AC3, il ne doit pas être puni."""
    output = _production()
    output["code_diffs"] = []
    output["tests"] = []
    output["unaddressed"] = [
        {"item": "Étape a2", "reason": "l'approche ne nomme aucun repo existant à réutiliser"}
    ]
    assert validate_producer_output(output) == []


def test_an_unaddressed_item_without_a_reason_is_named() -> None:
    output = _production()
    output["code_diffs"] = []
    output["unaddressed"] = [{"item": "Étape a2"}]
    assert any("reason" in problem for problem in validate_producer_output(output))


def test_a_failed_production_must_carry_a_blocking_question() -> None:
    output: dict[str, Any] = {
        "status": "failed",
        "code_diffs": [],
        "tests": [],
        "docs_snippets": [],
    }
    assert any("blocking_question" in problem for problem in validate_producer_output(output))


def test_a_production_that_is_not_an_object_never_raises() -> None:
    for candidate in (None, [], "texte", 3):
        assert validate_producer_output(candidate)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Le dispatch — sur le contrat DÉCLARÉ, jamais sur le nom de l'agent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_the_dispatch_keys_on_the_declared_core_never_on_the_node_name() -> None:
    """C'est la propriété que la revue de la 5.1 a imposée, et c'est elle qui
    fait que 5.4 → 5.6 hériteront du mécanisme sans une ligne."""
    assert (
        contract_problems(_production(), declared_core={"code_diffs", "tests", "docs_snippets"})
        == []
    )
    # Le même objet, jugé contre un AUTRE contrat déclaré, est non conforme.
    assert contract_problems(
        _production(), declared_core={"approach", "tradeoffs", "risks", "test_strategy"}
    )


def test_an_undeclared_contract_is_not_judged() -> None:
    """Un template qui ne déclare aucun des contrats connus n'est pas jugé —
    ni faussement conforme, ni faussement fautif."""
    assert contract_problems({"anything": 1}, declared_core={"anything"}) == []


def test_the_delegation_plan_contract_is_still_dispatched_here() -> None:
    """Le Dev Lead passe désormais par le MÊME point d'entrée.

    Deux dispatchs parallèles auraient divergé au premier ajout ; la 5.1 avait
    posé le point d'application, cette story ne le double pas.
    """
    problems = contract_problems(
        {"status": "done", "plan": [], "delegations": []},
        declared_core={"plan", "delegations", "status"},
    )
    assert problems


def test_a_raw_output_envelope_is_not_re_reported() -> None:
    """Sortie non parsable : déjà signalée par le repli `raw_output` et par la
    règle de routage `no-parsable-output`. Le redire ici dirait deux fois la
    même chose."""
    assert (
        contract_problems(
            {"_raw": "pas du JSON"}, declared_core={"code_diffs", "tests", "docs_snippets"}
        )
        == []
    )


def test_the_registry_has_no_ambiguous_contract() -> None:
    """Aucun contrat déclaré n'est un sous-ensemble d'un autre.

    Le dispatch rend le PREMIER contrat couvert. Si un jeu de clés en
    contenait un autre, l'ordre de déclaration déciderait silencieusement du
    verdict — et le défaut ne se verrait qu'au moment où un agent déclarerait
    les deux. Cette propriété est citée par la docstring de `_VALIDATORS` ;
    ce test est ce qui la rend vraie.
    """
    from agentive_backend.shared.contracts.dev_outputs import declared_contracts

    contracts = declared_contracts()
    for left in contracts:
        for right in contracts:
            if left is not right:
                assert not left <= right, f"{sorted(left)} est couvert par {sorted(right)}"


# ─────────────────────────────────────────────────────────────────────────────
# Revue de la Story 5.3 — les trous que les échantillons nominaux ne voyaient
# pas. Chacun de ces tests est écrit sur la PROPRIÉTÉ gardée, et ses
# échantillons sont posés à la main : les dériver des constantes du validateur
# reproduirait la circularité que la revue de la 5.2 a dû défaire huit fois.
# ─────────────────────────────────────────────────────────────────────────────


def test_a_production_that_declares_no_real_work_cannot_hide_behind_unaddressed() -> None:
    """`unaddressed` doit porter des OBJETS, sinon la règle se contourne.

    La règle « produire, ou dire ce qui a empêché de produire » ne comptait
    que la longueur brute de `unaddressed`. Une liste d'une chaîne suffisait
    donc à la satisfaire — et c'est la sortie que produit spontanément un
    modèle qui n'a rien à dire. Les deux constats sont exigés : la forme des
    entrées ET l'absence de travail décrit.
    """
    problems = validate_producer_output(
        {
            "status": "done",
            "code_diffs": [],
            "tests": [],
            "docs_snippets": [],
            "unaddressed": ["l'étape a3 n'a pas été traitée"],
        }
    )
    assert any("unaddressed doit être un objet" in p for p in problems), problems
    assert any("ne décrit aucun travail" in p for p in problems), problems


def test_a_well_formed_gap_still_stands_in_for_a_production() -> None:
    """La contrepartie : un écart EXPLOITABLE reste une sortie conforme.

    Sans ce test, le précédent pourrait être satisfait en refusant tout
    `code_diffs` vide — ce qui forcerait un Producteur bloqué à improviser,
    l'exact contraire de ce que le prompt lui demande.
    """
    assert (
        validate_producer_output(
            {
                "status": "done",
                "code_diffs": [],
                "tests": [],
                "docs_snippets": [],
                "unaddressed": [{"item": "étape a3", "reason": "le fichier visé n'existe pas"}],
            }
        )
        == []
    )


def test_a_refusal_that_still_hands_over_an_approach_is_named_whatever_its_type() -> None:
    """Sur `failed`, la règle porte sur « a-t-il rendu », pas sur le type.

    Un `approach` livré en CHAÎNE échappait à tout : ni `Mapping` non vide
    (donc pas vu par la branche `failed`), ni soumis à `_approach_problems`
    (la branche `done` est court-circuitée). Le refus à moitié rendu que la
    règle interdit passait par la porte de derrière.
    """
    problems = validate_architect_approach(
        {
            "status": "failed",
            "approach": "voici quand même l'approche complète que je propose",
            "blocking_question": "quel module vise la tâche ?",
        }
    )
    assert any("à moitié rendue" in p for p in problems), problems
    assert any("approach" in p for p in problems), problems


def test_a_clean_refusal_still_passes() -> None:
    """La contrepartie du précédent : un refus qui ne rend RIEN est conforme."""
    assert (
        validate_architect_approach(
            {
                "status": "failed",
                "approach": {},
                "tradeoffs": [],
                "risks": [],
                "test_strategy": {},
                "blocking_question": "le Chercheur n'a rendu aucun fichier exploitable",
            }
        )
        == []
    )


def test_an_approach_without_a_single_risk_is_named() -> None:
    """L'AC3 énumère « la complexité estimée, LES RISQUES et au moins une
    alternative écartée ». Un `risks: []` passait pourtant conforme : la
    boucle de validation ne juge que les risques déclarés, donc zéro risque
    ne violait rien. La règle est posée des deux côtés — ici et dans les
    « RÈGLES DURES » du prompt de l'Analyste.
    """
    problems = validate_architect_approach(
        {
            "status": "done",
            "approach": {
                "summary": "brancher le lecteur sur le port existant",
                "complexity": "low",
                "steps": [{"id": "a1", "title": "brancher"}],
            },
            "tradeoffs": [
                {"option": "port existant", "chosen": True, "rationale": "déjà testé"},
                {"option": "nouveau port", "chosen": False, "rationale": "coût sans gain"},
            ],
            "risks": [],
            "test_strategy": {"levels": ["unit"], "focus": ["le port reste unique"]},
        }
    )
    assert any("au moins un risque" in p for p in problems), problems


def test_a_test_strategy_that_names_no_property_to_keep_is_named() -> None:
    """`focus` n'était testé qu'en TYPE : une liste vide disait « des tests,
    mais sans dire ce qu'ils gardent ». Symétrique de `levels`, qui exige
    déjà au moins une entrée."""
    problems = validate_architect_approach(
        {
            "status": "done",
            "approach": {
                "summary": "brancher le lecteur sur le port existant",
                "complexity": "low",
                "steps": [{"id": "a1", "title": "brancher"}],
            },
            "tradeoffs": [
                {"option": "port existant", "chosen": True, "rationale": "déjà testé"},
                {"option": "nouveau port", "chosen": False, "rationale": "coût sans gain"},
            ],
            "risks": [
                {"risk": "le port change de signature", "severity": "low", "mitigation": "test"}
            ],
            "test_strategy": {"levels": ["unit"], "focus": []},
        }
    )
    assert any("au moins une propriété à garder" in p for p in problems), problems


def test_a_core_that_covers_two_contracts_is_named_not_silently_arbitrated() -> None:
    """Le dispatch compare le contrat au `core` DÉCLARÉ, pas aux autres contrats.

    `test_the_registry_has_no_ambiguous_contract` garde une propriété
    nécessaire mais PAS suffisante : deux contrats dont aucun n'est
    sous-ensemble de l'autre peuvent être couverts tous les deux par un `core`
    qui en est l'union. `output_contract.core` étant librement écrivable par
    `PUT /api/v1/agents/templates/{id}`, le cas est atteignable — et l'ordre
    du registre ne doit pas le trancher à la place de l'auteur du template.
    """
    problems = contract_problems(
        {"status": "done"},
        declared_core={"plan", "delegations", "code_diffs", "tests", "docs_snippets"},
    )
    assert len(problems) == 1, problems
    assert "couvre PLUSIEURS contrats" in problems[0]
    # Et le constat NOMME les deux contrats, sinon il n'est pas actionnable.
    assert "plan" in problems[0] and "code_diffs" in problems[0]


def test_the_raw_envelope_key_still_matches_the_one_the_engine_writes() -> None:
    """`RAW_OUTPUT_KEY` est dupliquée ici plutôt qu'importée (`shared/` ne peut
    pas dépendre de `features/`, Contract 1). Ce test est le seul endroit où
    les deux côtés peuvent être comparés — comme
    `test_no_dev_template_declares_more_tokens_than_the_engine_allows` le fait
    pour `MAX_TOKENS_HARD_CAP`.

    Sans lui, un renommage côté moteur ferait rendre `contract_problems` une
    avalanche de faux constats sur CHAQUE sortie non parsable, en silence.
    """
    from agentive_backend.features.workflow_engine.engine.agent_node import (
        RAW_OUTPUT_KEY as ENGINE_RAW_OUTPUT_KEY,
    )
    from agentive_backend.shared.contracts.dev_outputs import RAW_OUTPUT_KEY

    assert RAW_OUTPUT_KEY == ENGINE_RAW_OUTPUT_KEY
