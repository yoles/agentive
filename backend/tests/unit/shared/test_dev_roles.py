"""Jeu fermé des rôles du Pôle Dev — Story 5.1 T1.4.

Ce qui est vérifié ici n'est pas décoratif : l'AC3 (« les agents assignés
logiques ») n'est vérifiable qu'à travers ce jeu fermé, et le prompt du Dev
Lead est rendu depuis lui.
"""

from __future__ import annotations

from agentive_backend.shared.contracts.dev_roles import (
    DEV_LEAD_ROLE,
    DEV_ROLE_DESCRIPTIONS,
    DEV_ROLES,
    unknown_roles,
    validate_delegation_plan,
)


def test_the_eight_delegable_roles_are_declared_once_each() -> None:
    assert len(DEV_ROLES) == 8
    assert len(set(DEV_ROLES)) == 8


def test_the_dev_lead_is_not_one_of_the_delegable_roles() -> None:
    """Se déléguer à soi-même est une boucle, pas un plan."""
    assert DEV_LEAD_ROLE not in DEV_ROLES


def test_every_role_carries_a_description() -> None:
    """Le prompt route sur les descriptions, pas sur les slugs — un rôle sans
    description produirait une ligne vide dans le prompt du Dev Lead."""
    assert set(DEV_ROLE_DESCRIPTIONS) == set(DEV_ROLES)
    assert all(DEV_ROLE_DESCRIPTIONS[role].strip() for role in DEV_ROLES)


def test_known_roles_are_not_reported_unknown() -> None:
    assert unknown_roles(DEV_ROLES) == []


def test_unknown_roles_are_sorted_and_deduplicated() -> None:
    """Un message d'erreur doit être reproductible d'une exécution à l'autre."""
    assert unknown_roles(["zeta", "alpha", "zeta", "code_producer"]) == ["alpha", "zeta"]


def test_a_non_string_role_is_reported_rather_than_dropped() -> None:
    """`None` dans `target_role` est une anomalie au même titre qu'un slug
    inconnu. La faire disparaître du rapport serait la dégradation silencieuse
    que la revue de la Story 5.0 a corrigée sur `_to_lc_messages`."""
    reported = unknown_roles([None, 42, "code_producer"])
    # Le label porte le TYPE en plus de la valeur : c'est ce qui empêche un
    # `null` de se confondre avec la chaîne littérale `"None"`.
    assert any("None" in item for item in reported)
    assert any("42" in item for item in reported)
    assert len(reported) == 2


# ─── Cohérence d'un plan de délégation (AC3) ─────────────────────────


def _plan(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "done",
        "plan": [
            {"id": "s1", "title": "Explorer"},
            {"id": "s2", "title": "Implémenter"},
        ],
        "delegations": [
            {"subtask_id": "s1", "target_role": "code_researcher"},
            {"subtask_id": "s2", "target_role": "code_producer"},
        ],
    }
    base.update(overrides)
    return base


def test_a_coherent_plan_has_no_problem() -> None:
    assert validate_delegation_plan(_plan()) == []


def test_a_role_outside_the_closed_set_is_named() -> None:
    plan = _plan(delegations=[{"subtask_id": "s1", "target_role": "devops_wizard"}])
    problems = validate_delegation_plan(plan)
    assert any("devops_wizard" in problem for problem in problems)


def test_a_delegation_to_an_unknown_subtask_is_named() -> None:
    plan = _plan(
        delegations=[
            {"subtask_id": "s1", "target_role": "code_researcher"},
            {"subtask_id": "s9", "target_role": "code_producer"},
        ]
    )
    assert any("s9" in problem for problem in validate_delegation_plan(plan))


def test_a_subtask_nobody_takes_is_named() -> None:
    plan = _plan(delegations=[{"subtask_id": "s1", "target_role": "code_researcher"}])
    assert any("s2" in problem for problem in validate_delegation_plan(plan))


def test_an_invalid_status_is_named() -> None:
    assert any("status" in problem for problem in validate_delegation_plan(_plan(status="ok")))


def test_a_failed_status_must_carry_nothing() -> None:
    """Rendre un plan ET déclarer l'échec est une contradiction, pas un
    demi-succès."""
    problems = validate_delegation_plan(_plan(status="failed"))
    assert any("failed" in problem for problem in problems)
    assert (
        validate_delegation_plan(
            {
                "status": "failed",
                "plan": [],
                "delegations": [],
                "blocking_question": "Quel module de paiement ? Le dépôt en contient deux.",
            }
        )
        == []
    )


def test_a_failed_status_without_a_blocking_question_is_refused() -> None:
    """La règle dure du prompt a DEUX moitiés : tableaux vides ET
    `blocking_question` présente.

    Seule la première était vérifiée — un refus sans raison énoncée passait
    pour conforme, et ce test-ci assertait `== []` sur exactement cette
    sortie, verrouillant le trou au lieu de l'attraper.
    """
    problems = validate_delegation_plan({"status": "failed", "plan": [], "delegations": []})
    assert any("blocking_question" in problem for problem in problems)

    # Une question vide ou blanche ne compte pas non plus.
    for empty in ("", "   "):
        assert any(
            "blocking_question" in problem
            for problem in validate_delegation_plan(
                {
                    "status": "failed",
                    "plan": [],
                    "delegations": [],
                    "blocking_question": empty,
                }
            )
        )


def test_a_done_status_with_an_empty_plan_is_not_a_decomposition() -> None:
    """Aucune borne de cardinalité n'était vérifiée : `done` + plan vide +
    zéro délégation était déclaré conforme, alors que le prompt impose
    « Découpe en 2 à 6 sous-tâches »."""
    problems = validate_delegation_plan({"status": "done", "plan": [], "delegations": []})
    assert any("au moins une sous-tâche" in problem for problem in problems)


def test_two_delegations_on_the_same_subtask_are_refused() -> None:
    """`targeted` est un `set` : un doublon y était absorbé sans un mot, et
    les Stories 5.2 → 5.6 auraient mis deux exécutants sur une tâche."""
    problems = validate_delegation_plan(
        {
            "status": "done",
            "plan": [{"id": "s1", "title": "t"}],
            "delegations": [
                {"subtask_id": "s1", "target_role": "code_producer"},
                {"subtask_id": "s1", "target_role": "code_reviewer"},
            ],
        }
    )
    assert any("plusieurs fois" in problem for problem in problems)


def test_a_null_role_does_not_collide_with_the_literal_string_none() -> None:
    """`repr(None)` vaut `'None'` : sans le type dans le label, un
    `target_role: null` et la chaîne `"None"` se dédupliquaient l'un l'autre
    et l'un des deux disparaissait du rapport."""
    rogue = unknown_roles([None, "None"])
    assert len(rogue) == 2


def test_duplicate_subtask_ids_are_named() -> None:
    plan = _plan(plan=[{"id": "s1"}, {"id": "s1"}])
    assert any("unique" in problem for problem in validate_delegation_plan(plan))


def test_a_raw_text_fallback_is_reported_not_crashed() -> None:
    """`agent_node` retombe sur `{RAW_OUTPUT_KEY: text}` quand le modèle n'a
    pas rendu de JSON. Ce cas doit produire un diagnostic, pas une exception."""
    problems = validate_delegation_plan({"raw_output": "Bonjour, voici mon plan…"})
    assert problems
    assert validate_delegation_plan("pas un objet") == [
        "la sortie n'est pas un objet JSON (reçu str)"
    ]
    assert validate_delegation_plan(None)


def test_malformed_entries_do_not_raise() -> None:
    plan = _plan(plan=["pas un dict", {"id": ""}], delegations=[None, 42])
    assert validate_delegation_plan(plan)
