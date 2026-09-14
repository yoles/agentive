"""Provisioning du Pôle Dev — logique pure (Story 5.1 T2).

La partie qui parle à Postgres est couverte par
``tests/integration/workflow_engine/test_dev_lead_e2e.py``. Ici : la
comparaison qui décide « rien n'a changé », dont dépend toute l'idempotence.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from scripts.seed_dev import (
    SeedError,
    SeedReport,
    config_is_current,
    desired_config_subset,
    stale_owned_keys,
)

from agentive_backend.features.agent_registry.dev_catalog import load_dev_catalog


def _dev_lead() -> object:
    return load_dev_catalog()["dev_lead"]


def test_the_owned_subset_covers_what_the_catalog_declares() -> None:
    subset = desired_config_subset(_dev_lead())  # type: ignore[arg-type]
    assert set(subset) == {
        "system_prompt",
        "input_contract",
        "output_contract",
        "llm_params",
        "error_policy",
        "push_memory",
    }


def test_an_unset_llm_model_is_not_claimed_as_owned() -> None:
    """Un `None` de premier niveau signifie « le catalogue ne possède pas ce
    champ » — le comparer écraserait un réglage fait à la main."""
    assert "llm_model" not in desired_config_subset(_dev_lead())  # type: ignore[arg-type]


def test_a_config_carrying_exactly_the_desired_values_is_current() -> None:
    desired = desired_config_subset(_dev_lead())  # type: ignore[arg-type]
    assert config_is_current(desired, desired)


def test_extra_keys_in_the_stored_config_do_not_make_it_stale() -> None:
    """`update_template` a une sémantique PATCH : `prompt_base` et `role`,
    posés par l'archétype à la création, restent en base et ne regardent pas
    ce provisioning."""
    desired = desired_config_subset(_dev_lead())  # type: ignore[arg-type]
    stored = {**desired, "prompt_base": "posé par l'archétype", "role": "orchestrator"}
    assert config_is_current(stored, desired)


def test_a_changed_prompt_makes_the_config_stale() -> None:
    desired = desired_config_subset(_dev_lead())  # type: ignore[arg-type]
    stored = {**desired, "system_prompt": "autre chose"}
    assert not config_is_current(stored, desired)


def test_a_missing_key_makes_the_config_stale() -> None:
    """Le cas du premier provisioning : la row existe (créée depuis
    l'archétype) mais n'a encore aucun `system_prompt`."""
    desired = desired_config_subset(_dev_lead())  # type: ignore[arg-type]
    stored = {key: value for key, value in desired.items() if key != "push_memory"}
    assert not config_is_current(stored, desired)


def test_a_non_dict_config_is_stale_rather_than_crashing() -> None:
    """`agent_templates.config` est du JSONB — une row corrompue doit faire
    ré-écrire la config, pas lever au milieu d'un provisioning."""
    assert not config_is_current(None, {"system_prompt": "x"})
    assert not config_is_current("pas un dict", {"system_prompt": "x"})


def test_push_memory_off_compares_against_the_shape_the_db_actually_holds() -> None:
    """Le désiré doit être comparé à ce que la BASE contient, pas à ce que le
    DTO sérialise.

    `PushMemorySettings.to_mapping()` réduit un Push Memory éteint à `{}` —
    PAS à `{"namespace": None, "optin": False}`. Comparer la forme du DTO
    rendait une inégalité perpétuelle : `update_template` à chaque exécution,
    un event d'audit à chaque exécution, sur une config déjà correcte.

    Ce test compare au round-trip RÉEL (le VO qui écrit en base), là où son
    prédécesseur assertait `config_is_current(desired, desired)` — le désiré
    contre lui-même, ce qui ne pouvait rien attraper.
    """
    from agentive_backend.features.agent_registry.dev_catalog import DevAgentDefinition
    from agentive_backend.features.agent_registry.domain.value_objects import (
        PushMemorySettings,
    )

    definition = DevAgentDefinition.model_validate(
        {
            "key": "probe",
            "name": "Probe",
            "archetype": "orchestrateur",
            "system_prompt": "x",
            "input_contract": {"core": {}, "extras": {}},
            "output_contract": {"core": {}, "extras": {}},
            "push_memory": {"namespace": None, "optin": False},
        }
    )
    desired = desired_config_subset(definition)

    persisted = PushMemorySettings(namespace=None, optin=False).to_mapping()
    assert desired["push_memory"] == persisted

    # Le provisioning voit une config déjà conforme, donc ne ré-écrit rien.
    stored = {**desired, "push_memory": persisted}
    assert config_is_current(stored, desired)


def test_a_nested_null_that_is_significant_is_preserved() -> None:
    """La contrepartie : `exclude_none` est RÉCURSIF, et tout `null` imbriqué
    n'est pas du bruit. Un `push_memory` AVEC namespace garde ses deux champs
    — seule la forme « éteinte » est réduite, parce que c'est la seule que le
    VO réduit."""
    from agentive_backend.features.agent_registry.dev_catalog import DevAgentDefinition

    definition = DevAgentDefinition.model_validate(
        {
            "key": "probe",
            "name": "Probe",
            "archetype": "orchestrateur",
            "system_prompt": "x",
            "input_contract": {"core": {}, "extras": {}},
            "output_contract": {"core": {}, "extras": {}},
            "push_memory": {"namespace": "dev-metier", "optin": True},
            # Déclaré, sinon le validateur du catalogue refuse : un
            # `push_memory` pointant un namespace que le provisioning ne crée
            # pas donne un 422 à chaque lancement de run.
            "namespaces": [{"name": "dev-metier", "type": "metier"}],
        }
    )
    desired = desired_config_subset(definition)
    assert desired["push_memory"] == {"namespace": "dev-metier", "optin": True}


def test_a_key_dropped_from_the_catalog_is_refused_rather_than_reported_current() -> None:
    """`update_template` est un PATCH : retirer `llm_model` du YAML ne
    dépingle pas le modèle en base.

    Sans ce contrôle, le provisioning rapportait « inchangé » sur une config
    périmée — un rapport vert sur un état faux, et le runbook promettait
    l'inverse (« il ré-applique le catalogue »).
    """
    desired = desired_config_subset(_dev_lead())  # type: ignore[arg-type]
    assert "llm_model" not in desired

    stored = {**desired, "llm_model": "claude-3-5-sonnet-20241022"}
    assert stale_owned_keys(stored, desired) == ["llm_model"]

    # `config_is_current` seul ne voit RIEN : c'est exactement le trou.
    assert config_is_current(stored, desired)


def test_a_key_absent_from_both_sides_is_not_stale() -> None:
    """Une clé que ni le catalogue ni la base ne portent n'est pas une
    dérive — sinon tout provisioning nominal lèverait."""
    desired = desired_config_subset(_dev_lead())  # type: ignore[arg-type]
    assert stale_owned_keys(desired, desired) == []
    assert stale_owned_keys({**desired, "llm_model": None}, desired) == []


# ─── T2.4 — le refus bruyant sur outil introuvable ou ambigu ──────────
#
# Ce bloc existe parce que la revue de la Story 5.1 a trouvé que le mécanisme
# d'assignation déclarative n'était atteint par AUCUN chemin : `_assign_tools`
# sort immédiatement sur `if not definition.tools:` et le catalogue livré
# déclare `tools: []`. La branche de résolution, les deux `SeedError` et
# `ToolRepo.list_by_names` n'étaient donc jamais exécutés — alors que l'AC1
# exige que le mécanisme soit « livré ET exercé ».
#
# C'est exactement la leçon que la Story 5.0 avait déjà payée (`node_tools`
# câblé par aucun appelant de production, tests unitaires verts) et que les
# Dev Notes de cette story citaient pour ne pas la reproduire.


class _FakeTool:
    def __init__(self, tool_id: uuid.UUID) -> None:
        self.id = tool_id


class _FakeToolRepo:
    def __init__(self, found: dict[str, list[_FakeTool]]) -> None:
        self._found = found
        self.calls: list[list[str]] = []

    async def list_by_names(
        self, names: Sequence[str], *, tenant_id: uuid.UUID | None = None
    ) -> dict[str, list[_FakeTool]]:
        self.calls.append(list(names))
        return {name: self._found[name] for name in names if name in self._found}


class _FakeToolService:
    def __init__(self) -> None:
        self.replaced: list[tuple[uuid.UUID, list[uuid.UUID]]] = []

    async def replace_template_tools(
        self, template_id: uuid.UUID, tool_ids: list[uuid.UUID], *, tenant_id: uuid.UUID | None
    ) -> None:
        self.replaced.append((template_id, list(tool_ids)))


def _seeder_with(
    found: dict[str, list[_FakeTool]], current: set[uuid.UUID] | None = None
) -> tuple[Any, _FakeToolRepo, _FakeToolService]:
    """Un seeder dont SEULES les dépendances d'assignation sont réelles.

    `__new__` plutôt que `__init__` : ce dernier construit six repos et trois
    services contre une vraie `session_factory`, dont aucun n'est utilisé par
    `_assign_tools`. Monter une base pour tester une résolution de noms
    testerait surtout testcontainers.
    """
    from scripts.seed_dev import DevDepartmentSeeder

    seeder = DevDepartmentSeeder.__new__(DevDepartmentSeeder)
    repo = _FakeToolRepo(found)
    service = _FakeToolService()
    seeder._tool_repo = repo  # type: ignore[attr-defined]
    seeder._tool_service = service  # type: ignore[attr-defined]
    seeder._tenant_id = None  # type: ignore[attr-defined]
    held = set() if current is None else current

    async def _current_tool_ids(template_id: uuid.UUID) -> set[uuid.UUID]:
        return held

    seeder._current_tool_ids = _current_tool_ids  # type: ignore[attr-defined]
    return seeder, repo, service


def _definition_with_tools(tools: list[str]) -> Any:
    from agentive_backend.features.agent_registry.dev_catalog import DevAgentDefinition

    return DevAgentDefinition.model_validate(
        {
            "key": "probe",
            "name": "Probe",
            "archetype": "orchestrateur",
            "system_prompt": "x",
            "input_contract": {"core": {}, "extras": {}},
            "output_contract": {"core": {}, "extras": {}},
            "tools": tools,
        }
    )


@pytest.mark.asyncio
async def test_an_unresolvable_tool_fails_the_provisioning_loudly() -> None:
    """Un nom introuvable NOMME l'outil et l'action à faire.

    Jamais un template silencieusement dépourvu d'outils : c'est indiscernable
    d'un template sans outils, et c'est le mensonge par omission que
    `tool_invocations=[]` a entretenu deux epics durant (D80).
    """
    seeder, _repo, service = _seeder_with({"read_file": [_FakeTool(uuid.uuid4())]})
    report = SeedReport()

    with pytest.raises(SeedError) as excinfo:
        await seeder._assign_tools(
            _definition_with_tools(["read_file", "grep_search"]), uuid.uuid4(), report
        )

    message = str(excinfo.value)
    assert "grep_search" in message
    assert "read_file" not in message.split("introuvables")[1]
    assert "POST /api/v1/tools/servers" in message
    # Rien n'a été assigné : l'échec est ATOMIQUE, pas partiel.
    assert service.replaced == []


@pytest.mark.asyncio
async def test_an_ambiguous_tool_fails_rather_than_picking_the_first() -> None:
    """Deux serveurs MCP exposant le même nom : en choisir un ferait dépendre
    l'assignation de l'ordre de retour de Postgres."""
    seeder, _repo, service = _seeder_with(
        {"read_file": [_FakeTool(uuid.uuid4()), _FakeTool(uuid.uuid4())]}
    )

    with pytest.raises(SeedError) as excinfo:
        await seeder._assign_tools(
            _definition_with_tools(["read_file"]), uuid.uuid4(), SeedReport()
        )

    assert "ambigus" in str(excinfo.value)
    assert "read_file" in str(excinfo.value)
    assert service.replaced == []


@pytest.mark.asyncio
async def test_a_tool_declared_twice_is_refused() -> None:
    """`replace_template_tools` recevrait deux fois le même id — et la
    comparaison par ensemble juste après masquerait le doublon en
    « inchangé »."""
    seeder, _repo, service = _seeder_with({"read_file": [_FakeTool(uuid.uuid4())]})

    with pytest.raises(SeedError) as excinfo:
        await seeder._assign_tools(
            _definition_with_tools(["read_file", "read_file"]), uuid.uuid4(), SeedReport()
        )

    assert "deux fois" in str(excinfo.value)
    assert service.replaced == []


@pytest.mark.asyncio
async def test_resolved_tools_are_assigned_in_the_declared_order() -> None:
    """Le chemin NOMINAL — celui que le catalogue livré (`tools: []`) ne
    parcourait jamais."""
    first, second = _FakeTool(uuid.uuid4()), _FakeTool(uuid.uuid4())
    seeder, repo, service = _seeder_with({"read_file": [first], "grep_search": [second]})
    report = SeedReport()

    await seeder._assign_tools(
        _definition_with_tools(["read_file", "grep_search"]), uuid.uuid4(), report
    )

    assert repo.calls == [["read_file", "grep_search"]]
    assert [tool_id for _template, ids in service.replaced for tool_id in ids] == [
        first.id,
        second.id,
    ]
    assert report.updated == ["outils de Probe"]


@pytest.mark.asyncio
async def test_an_unchanged_tool_set_is_not_rewritten() -> None:
    """Idempotence : même ensemble d'outils ⇒ aucun appel, aucun event."""
    tool = _FakeTool(uuid.uuid4())
    seeder, _repo, service = _seeder_with({"read_file": [tool]}, current={tool.id})
    report = SeedReport()

    await seeder._assign_tools(_definition_with_tools(["read_file"]), uuid.uuid4(), report)

    assert service.replaced == []
    assert report.unchanged == ["outils de Probe"]


@pytest.mark.asyncio
async def test_an_agent_declaring_no_tool_says_so_in_the_report() -> None:
    """« provisionné sans outils » et « outils non considérés » rendaient le
    MÊME rapport — l'ambiguïté que D80 a coûté deux epics à lever."""
    seeder, _repo, service = _seeder_with({})
    report = SeedReport()

    await seeder._assign_tools(_definition_with_tools([]), uuid.uuid4(), report)

    assert service.replaced == []
    assert report.unchanged == ["outils de Probe (aucun déclaré)"]
