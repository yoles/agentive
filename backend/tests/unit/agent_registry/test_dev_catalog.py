"""Catalogue déclaratif du Pôle Dev — Story 5.1 T1 (AC1).

Deux familles de tests :
* le LOADER (erreurs de forme, substitution de jetons, unicité des clés) ;
* le CATALOGUE LIVRÉ (`dev_lead.yaml`), dont les propriétés sont des AC — un
  `output_contract.core` incomplet rendrait illégal tout branchement derrière
  ce node (validation de la Story 4.1), et un `push_memory.namespace` sans
  déclaration de namespace ferait refuser chaque lancement de run
  (`mise_en_place._check_namespaces`, Story 4.5 AC2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentive_backend.features.agent_registry.dev_catalog import (
    DEV_ROLES_TOKEN,
    catalog_namespaces,
    load_dev_catalog,
    render_dev_roles,
)
from agentive_backend.shared.contracts.dev_roles import DEV_ROLES

_MINIMAL = """
key: probe
name: Probe
archetype: orchestrateur
system_prompt: |-
  Tu es un agent de test.
input_contract:
  core: {objective: string}
  extras: {}
output_contract:
  core: {plan: array}
  extras: {}
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    (tmp_path / name).write_text(body, encoding="utf-8")
    return tmp_path


# ─── Loader ──────────────────────────────────────────────────────────


def test_a_minimal_definition_loads(tmp_path: Path) -> None:
    catalog = load_dev_catalog(_write(tmp_path, "probe.yaml", _MINIMAL))
    assert set(catalog) == {"probe"}
    assert catalog["probe"].archetype == "orchestrateur"


def test_the_catalog_is_immutable_once_loaded(tmp_path: Path) -> None:
    """Mirror de `load_registry` (P-12) : une mutation accidentelle d'un
    catalogue long-vécu doit être un TypeError, pas une convention."""
    catalog = load_dev_catalog(_write(tmp_path, "probe.yaml", _MINIMAL))
    with pytest.raises(TypeError):
        catalog["probe"] = catalog["probe"]  # type: ignore[index]


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    body = _MINIMAL + "\nmystere: 1\n"
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_dev_catalog(_write(tmp_path, "probe.yaml", body))


def test_a_malformed_yaml_names_its_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"probe\.yaml"):
        load_dev_catalog(_write(tmp_path, "probe.yaml", "key: [unclosed\n"))


def test_a_non_mapping_document_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="expected mapping"):
        load_dev_catalog(_write(tmp_path, "probe.yaml", "- just\n- a list\n"))


def test_an_empty_directory_is_refused(tmp_path: Path) -> None:
    """Un catalogue vide qui se charge silencieusement livrerait un
    provisioning qui ne provisionne rien en rapportant un succès."""
    with pytest.raises(RuntimeError, match="no agent definition"):
        load_dev_catalog(tmp_path)


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="is not a directory"):
        load_dev_catalog(tmp_path / "absent")


def test_a_duplicate_key_across_two_files_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", _MINIMAL)
    _write(tmp_path, "b.yaml", _MINIMAL.replace("name: Probe", "name: Probe Two"))
    with pytest.raises(RuntimeError, match="duplicate key"):
        load_dev_catalog(tmp_path)


def test_the_roles_token_is_rendered(tmp_path: Path) -> None:
    body = _MINIMAL.replace("Tu es un agent de test.", f"Rôles :\n  {DEV_ROLES_TOKEN}")
    catalog = load_dev_catalog(_write(tmp_path, "probe.yaml", body))
    prompt = catalog["probe"].system_prompt
    assert DEV_ROLES_TOKEN not in prompt
    for role in DEV_ROLES:
        assert role in prompt


def test_an_unknown_token_is_refused_rather_than_sent_to_the_model(tmp_path: Path) -> None:
    """Un prompt portant un trou est pire qu'un chargement qui échoue."""
    body = _MINIMAL.replace("Tu es un agent de test.", "Rôles : ${DEV_ROLZ}")
    with pytest.raises(RuntimeError, match=r"unknown placeholder \$\{DEV_ROLZ\}"):
        load_dev_catalog(_write(tmp_path, "probe.yaml", body))


def test_json_braces_in_a_prompt_are_left_alone(tmp_path: Path) -> None:
    """Les prompts du pôle montrent leur contrat de sortie en JSON littéral —
    c'est pourquoi le jeton est `${...}` et non `{...}`."""
    body = _MINIMAL.replace("Tu es un agent de test.", 'Réponds par {"status": "done"}')
    catalog = load_dev_catalog(_write(tmp_path, "probe.yaml", body))
    assert '{"status": "done"}' in catalog["probe"].system_prompt


def test_conflicting_namespace_declarations_are_refused(tmp_path: Path) -> None:
    """Deux agents, un même nom de namespace, deux types : créer « le premier
    rencontré » ferait dépendre le résultat de l'ordre alphabétique."""
    ns = "\nnamespaces:\n  - {name: partage, type: %s}\n"
    _write(tmp_path, "a.yaml", _MINIMAL + ns % "metier")
    _write(
        tmp_path,
        "b.yaml",
        _MINIMAL.replace("key: probe", "key: probe2").replace("name: Probe", "name: Probe 2")
        + ns % "operationnelle",
    )
    with pytest.raises(RuntimeError, match="conflicting declarations"):
        catalog_namespaces(load_dev_catalog(tmp_path))


def test_render_dev_roles_lists_every_role_once() -> None:
    rendered = render_dev_roles()
    assert rendered.count("\n") == len(DEV_ROLES) - 1
    for role in DEV_ROLES:
        assert f"`{role}`" in rendered


# ─── Le catalogue réellement livré ───────────────────────────────────


def test_the_shipped_catalog_loads() -> None:
    catalog = load_dev_catalog()
    assert "dev_lead" in catalog


def test_dev_lead_is_built_on_the_orchestrateur_archetype() -> None:
    """AC1 — « créé depuis l'archétype Orchestrateur »."""
    assert load_dev_catalog()["dev_lead"].archetype == "orchestrateur"


def test_dev_lead_output_contract_declares_plan_delegations_and_status() -> None:
    """`status` n'est pas optionnel : la règle `terminal-output-status` route
    dessus, et la Story 4.1 refuse toute condition d'edge portant une variable
    absente de l'`output_contract.core` de l'émetteur."""
    core = load_dev_catalog()["dev_lead"].output_contract.core
    assert set(core) == {"plan", "delegations", "status"}


def test_dev_lead_input_contract_matches_the_key_its_prompt_reads() -> None:
    """Rien ne valide `input_contract` au runtime — la cohérence entre le
    contrat déclaré, le `curl` du runbook et la clé lue par le prompt est
    entièrement à la charge de l'auteur du template."""
    definition = load_dev_catalog()["dev_lead"]
    assert set(definition.input_contract.core) == {"objective"}
    assert "`objective`" in definition.system_prompt


def test_dev_lead_prompt_enumerates_every_delegable_role() -> None:
    """AC3 — le modèle ne peut assigner que ce que son prompt lui a nommé."""
    prompt = load_dev_catalog()["dev_lead"].system_prompt
    for role in DEV_ROLES:
        assert role in prompt


def test_dev_lead_declares_the_namespace_its_push_memory_points_at() -> None:
    """Sans cette déclaration, le provisioning ne créerait pas le namespace et
    la Mise en Place refuserait CHAQUE lancement de run (Story 4.5 AC2)."""
    definition = load_dev_catalog()["dev_lead"]
    assert definition.push_memory is not None
    declared = {requirement.name for requirement in definition.namespaces}
    assert definition.push_memory.namespace in declared


def test_dev_lead_temperature_is_lower_than_the_engine_default() -> None:
    """Une décomposition doit être reproductible d'un run à l'autre : c'est
    ce que les 3 cas de l'AC3 vérifient."""
    params = load_dev_catalog()["dev_lead"].llm_params
    assert params is not None
    assert params.temperature < 0.7


def test_dev_lead_does_not_pin_an_llm_model() -> None:
    """Choix explicite : la whitelist `LLMModel` est restée aux modèles de
    Sprint 1 alors que `agent_node.DEFAULT_LLM_MODEL` vaut `claude-sonnet-4-6`.
    Ne rien fixer laisse le défaut du moteur s'appliquer. Ce test est le
    rappel : le jour où la Story 5.4 élargira la whitelist (FR15 exige des
    modèles DIFFÉRENTS entre Contrôleur et Producteur), il faudra statuer ici.
    """
    assert load_dev_catalog()["dev_lead"].llm_model is None
