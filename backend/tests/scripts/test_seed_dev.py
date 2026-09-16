"""Provisioning du Pôle Dev — logique pure (Story 5.1 T2).

La partie qui parle à Postgres est couverte par
``tests/integration/workflow_engine/test_dev_lead_e2e.py``. Ici : la
comparaison qui décide « rien n'a changé », dont dépend toute l'idempotence.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from scripts.seed_dev import (
    SeedError,
    SeedReport,
    _dag_edges_of,
    _dag_nodes_of,
    _dag_of,
    _is_neutral_stored_value,
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
    assert "catalogue du pôle" in message
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


# ─── Provisioning des serveurs MCP (Story 5.2 T3 / T7.3) ─────────────
#
# Même parti pris que le bloc d'assignation ci-dessus : `__new__` et des faux,
# parce que ce qui est testé ici est une DÉCISION (refuser, vérifier,
# rapporter), pas un aller-retour SQL. La partie qui parle à Postgres — le
# serveur réellement enregistré, la table `tools` peuplée par la découverte —
# est couverte par `tests/integration/mcp/test_code_search_registration_e2e.py`.


class _FakeServerRow:
    def __init__(
        self,
        server_id: uuid.UUID,
        name: str,
        *,
        transport: str = "stdio",
        connection_config: dict[str, Any] | None = None,
    ) -> None:
        from agentive_backend.infra.mcp.policy import code_search_connection_config

        self.id = server_id
        self.name = name
        self.transport = transport
        self.connection_config = (
            connection_config if connection_config is not None else code_search_connection_config()
        )


class _FakeToolHubService:
    """Un `ToolHubService` qui enregistre ce qu'on lui demande.

    `raises` permet de rejouer le chemin où la découverte échoue — le point
    exact où un serveur « enregistrable sur le papier » se révèle impossible à
    lancer.
    """

    def __init__(self, *, tools_count: int = 4, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._tools_count = tools_count
        self._raises = raises
        #: Fixe, pour que le test puisse vérifier que l'id RAPPORTÉ est bien
        #: celui que le service a rendu — l'assertion d'origine
        #: (`... is not None`) était vraie par construction.
        self.server_id = uuid.uuid4()

    async def connect_server(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        detail = type("_Detail", (), {})()
        detail.server_id = self.server_id
        detail.tools_count = self._tools_count
        return detail


class _FakeExposureRepo:
    """Rend les outils qu'un serveur expose, PAR ID.

    Revue 5.2 — la version d'origine ignorait le `server_id` (argument préfixé
    `_`, jamais lu) et rendait la même réponse pour n'importe quel id. Or
    `_provision_server` en passe trois différents selon la branche : une
    inversion passait toute la suite.
    """

    def __init__(self, exposed: dict[uuid.UUID, list[str]]) -> None:
        self._exposed = exposed
        self.queried: list[uuid.UUID] = []

    def with_tenant(self, _tenant_id: uuid.UUID | None) -> Any:
        class _Ctx:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *_exc: Any) -> bool:
                return False

        return _Ctx()

    async def list_by_server_in_session(self, _session: Any, server_id: uuid.UUID) -> list[Any]:
        self.queried.append(server_id)
        names = self._exposed.get(server_id, [])
        return [type("_Row", (), {"name": name})() for name in names]


def _server_definition(tools: list[str] | None = None) -> Any:
    from agentive_backend.features.agent_registry.dev_catalog import DevMcpServerDefinition

    return DevMcpServerDefinition.model_validate(
        {
            "key": "code_search",
            "name": "dev-code-search",
            "launcher": "code_search",
            "tools": tools or ["list_directory", "read_file", "find_files", "search_content"],
        }
    )


def _server_seeder(
    *,
    existing: _FakeServerRow | None = None,
    exposed: list[str] | None = None,
    live: list[str] | None = None,
    hub: _FakeToolHubService | None = None,
) -> tuple[Any, _FakeToolHubService, _FakeExposureRepo]:
    from scripts.seed_dev import DevDepartmentSeeder

    seeder = DevDepartmentSeeder.__new__(DevDepartmentSeeder)
    service = hub or _FakeToolHubService()
    seeder._tool_hub_service = service  # type: ignore[attr-defined]
    seeder._tenant_id = None  # type: ignore[attr-defined]

    default = list(_server_definition().tools)
    stored = exposed if exposed is not None else default
    # Le repo répond par id : celui de la row existante s'il y en a une, et
    # celui que le hub vient de rendre sinon.
    table: dict[uuid.UUID, list[str]] = {service.server_id: stored}
    if existing is not None:
        table[existing.id] = stored
    repo = _FakeExposureRepo(table)
    seeder._tool_repo = repo  # type: ignore[attr-defined]

    async def _get_server_by_name(_name: str) -> _FakeServerRow | None:
        return existing

    async def _probe_server(_server: Any) -> set[str]:
        return set(live if live is not None else stored)

    seeder._get_server_by_name = _get_server_by_name  # type: ignore[attr-defined]
    seeder._probe_server = _probe_server  # type: ignore[attr-defined]
    return seeder, service, repo


@pytest.mark.asyncio
async def test_the_registration_refusal_names_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3.4 — le refus du service devient un message d'opérateur.

    ⚠️ Revue 5.2 : la garde a DÉMÉNAGÉ. Elle vivait ici, dans l'appelant, ce
    qui laissait `ToolHubService.connect_server` ouvert pour le prochain script
    in-process — le trou même que la task décrivait. Elle est maintenant dans
    le service, au point où le sous-processus est spawné ; ce test vérifie que
    le seed la traduit sans l'avaler.
    """
    from scripts.seed_dev import SeedError

    from agentive_backend.shared.exceptions import ForbiddenError

    hub = _FakeToolHubService(raises=ForbiddenError(detail="disabled", context={}))
    seeder, _service, _repo = _server_seeder(hub=hub)

    with pytest.raises(SeedError) as excinfo:
        await seeder._provision_servers([_server_definition()], SeedReport())

    assert "AGENTIVE_ALLOW_MCP_REGISTRATION" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_run_that_registers_nothing_does_not_need_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La conséquence de ce déménagement, et c'est elle qui compte.

    La garde globale posée avant la boucle se déclenchait dès que le catalogue
    était non vide — donc aussi sur une base déjà provisionnée, où rien ne
    serait enregistré. `make seed-dev` échouait par conception, et la procédure
    nominale demandait d'activer un drapeau de sécurité à CHAQUE
    re-provisioning.
    """
    from scripts.seed_dev import SeedReport as _Report

    import agentive_backend.shared.config as _config

    monkeypatch.setattr(_config.settings, "mcp_allow_registration", False)
    row = _FakeServerRow(uuid.uuid4(), "dev-code-search")
    seeder, hub, _repo = _server_seeder(existing=row)
    report = _Report()

    await seeder._provision_servers([_server_definition()], report)

    assert hub.calls == [], "rien ne devait être enregistré"
    assert report.unchanged == ["serveur MCP dev-code-search"]


@pytest.mark.asyncio
async def test_a_declared_server_is_registered_through_the_service_not_in_sql() -> None:
    """T3.2 — c'est la découverte qui peuple `tools`, donc jamais de SQL direct."""
    seeder, hub, _repo = _server_seeder()
    report = SeedReport()

    await seeder._provision_servers([_server_definition()], report)

    assert len(hub.calls) == 1
    call = hub.calls[0]
    assert call["name"] == "dev-code-search"
    assert call["transport"] == "stdio"
    # Revue 5.2 — l'assertion d'origine (`server_ids[...] is not None`) était
    # vraie par construction, le fake rendant un `uuid4()`. Ce qui se vérifie
    # est que l'id RAPPORTÉ est celui que le service a rendu.
    assert report.server_ids["code_search"] == hub.server_id
    assert report.created == ["serveur MCP dev-code-search (4 outils découverts)"]


@pytest.mark.asyncio
async def test_a_second_run_verifies_the_exposed_tools_instead_of_re_registering() -> None:
    """T3.3 — `connect_server` n'a AUCUN chemin de re-découverte.

    Un nom déjà pris lève `ConflictError` avant même de spawner le serveur. La
    seconde exécution doit donc relire et VÉRIFIER, puis rapporter `unchanged`.
    """
    row = _FakeServerRow(uuid.uuid4(), "dev-code-search")
    seeder, hub, repo = _server_seeder(existing=row)
    report = SeedReport()

    await seeder._provision_servers([_server_definition()], report)

    assert hub.calls == []
    assert report.created == []
    assert report.unchanged == ["serveur MCP dev-code-search"]
    assert report.server_ids["code_search"] == row.id
    # Le repo a bien été interrogé sur CE serveur, pas sur un autre.
    assert repo.queried == [row.id]


@pytest.mark.asyncio
async def test_a_tool_that_vanished_from_the_stored_rows_is_refused_by_name() -> None:
    from scripts.seed_dev import SeedError

    row = _FakeServerRow(uuid.uuid4(), "dev-code-search")
    seeder, _hub, _repo = _server_seeder(
        existing=row, exposed=["list_directory", "read_file", "find_files"]
    )

    with pytest.raises(SeedError) as excinfo:
        await seeder._provision_servers([_server_definition()], SeedReport())

    message = str(excinfo.value)
    assert "search_content" in message
    assert "table `tools`" in message


@pytest.mark.asyncio
async def test_a_tool_that_vanished_from_the_live_server_is_refused_too() -> None:
    """Revue 5.2 — LE cas que T3.3 revendiquait et que le code ne voyait pas.

    Le contrôle n'interrogeait que la table `tools`, c'est-à-dire les rows
    écrites par la PREMIÈRE découverte. Un binaire qui cesse d'exposer
    `search_content` garde ses rows : le seed rapportait `unchanged`, et
    l'écart n'apparaissait qu'au premier run.
    """
    from scripts.seed_dev import SeedError

    row = _FakeServerRow(uuid.uuid4(), "dev-code-search")
    seeder, _hub, _repo = _server_seeder(
        existing=row, live=["list_directory", "read_file", "find_files"]
    )

    with pytest.raises(SeedError) as excinfo:
        await seeder._provision_servers([_server_definition()], SeedReport())

    message = str(excinfo.value)
    assert "search_content" in message
    assert "re-sondé" in message


@pytest.mark.asyncio
async def test_a_stored_config_that_escapes_the_sandbox_policy_is_refused() -> None:
    """Revue 5.2 — seuls les NOMS d'outils étaient comparés.

    Une row enregistrée autrement, exposant quatre outils homonymes, passait
    `unchanged` — et échappait à `apply_sandbox_policy`, dont la reconnaissance
    porte sur la tête de l'`argv`.
    """
    from scripts.seed_dev import SeedError

    row = _FakeServerRow(
        uuid.uuid4(),
        "dev-code-search",
        connection_config={"command": "/bin/sh", "args": ["-c", "echo hi"]},
    )
    seeder, _hub, _repo = _server_seeder(existing=row)

    with pytest.raises(SeedError, match="politique de sandbox"):
        await seeder._provision_servers([_server_definition()], SeedReport())


@pytest.mark.asyncio
async def test_a_stored_transport_that_diverges_is_refused() -> None:
    from scripts.seed_dev import SeedError

    row = _FakeServerRow(uuid.uuid4(), "dev-code-search", transport="sse")
    seeder, _hub, _repo = _server_seeder(existing=row)

    with pytest.raises(SeedError, match="transport"):
        await seeder._provision_servers([_server_definition()], SeedReport())


@pytest.mark.asyncio
async def test_a_server_that_cannot_be_launched_fails_the_provisioning_not_a_run() -> None:
    """Mieux vaut l'apprendre au provisioning qu'au premier `POST /runs`."""
    from scripts.seed_dev import SeedError

    hub = _FakeToolHubService(raises=RuntimeError("spawn failed"))
    seeder, _service, _repo = _server_seeder(hub=hub)

    with pytest.raises(SeedError) as excinfo:
        await seeder._provision_servers([_server_definition()], SeedReport())

    message = str(excinfo.value)
    # Revue 5.2 — le conseil ne code plus en dur un launcher : il rejoue la
    # commande RÉELLEMENT enregistrée, seule invocation dont l'échec est celui
    # qu'on diagnostique.
    assert "code_search" in message
    assert "--root" in message


@pytest.mark.asyncio
async def test_an_empty_server_catalog_registers_nothing() -> None:
    seeder, hub, _repo = _server_seeder()
    report = SeedReport()

    await seeder._provision_servers([], report)

    assert hub.calls == []
    assert report.created == []
    assert report.unchanged == []


def test_every_launcher_of_the_literal_has_a_factory() -> None:
    """Revue 5.2 — `_LAUNCHERS` et le `Literal` n'étaient couplés par RIEN.

    Le commentaire prétendait qu'ajouter un serveur demande les deux éditions
    « donc jamais par mégarde ». Une valeur ajoutée au `Literal` sans entrée
    dans `_LAUNCHERS` passait la validation YAML, passait le contrôle du
    drapeau, et échouait au moment de l'écriture.
    """
    from typing import get_args, get_type_hints

    from scripts.seed_dev import _LAUNCHERS

    from agentive_backend.features.agent_registry.dev_catalog import DevMcpServerDefinition

    declared = set(get_args(get_type_hints(DevMcpServerDefinition)["launcher"]))
    assert declared == set(_LAUNCHERS), (
        "toute valeur de `launcher` doit avoir une fabrique de `connection_config`"
    )


# ─── T5.2 — le garde-fou d'unicité du workflow d'entrée ──────────────────
#
# ⚠️ Ce bloc n'existait PAS. La revue de code l'a relevé : T5.2 est le defer
# que la revue de la Story 5.1 avait nommément confié à cette story, il était
# coché `[x]`, et `grep` sur tout `backend/tests` ne rendait aucune occurrence
# de `_assert_entry_workflow_is_unique`, de `list_by_name` ni de
# `DEV_LEAD_WORKFLOW_NAME_V1`. Les fakes ne câblaient même pas `_workflow_repo`.


class _FakeWorkflowRow:
    def __init__(
        self,
        name: str,
        nodes: dict[str, uuid.UUID],
        edges: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.dag = {
            "nodes": [
                {"node_id": node_id, "agent_template_id": str(template_id)}
                for node_id, template_id in nodes.items()
            ],
            # ⚠️ Revue 5.3 : les arêtes font partie de l'empreinte
            # d'idempotence, donc du contrôle. Une fake row qui n'en portait
            # aucune rendait la garde intestable sur ce point.
            "edges": [{"from_node_id": src, "to_node_id": dst} for src, dst in edges],
        }


class _FakeWorkflowRepo:
    def __init__(self, rows: dict[str, list[_FakeWorkflowRow]]) -> None:
        self._rows = rows

    async def list_by_name(
        self, name: str, *, tenant_id: uuid.UUID | None = None, limit: int = 100
    ) -> list[_FakeWorkflowRow]:
        return self._rows.get(name, [])[:limit]


def _workflow_seeder(rows: dict[str, list[_FakeWorkflowRow]]) -> Any:
    from scripts.seed_dev import DevDepartmentSeeder

    seeder = DevDepartmentSeeder.__new__(DevDepartmentSeeder)
    seeder._workflow_repo = _FakeWorkflowRepo(rows)  # type: ignore[attr-defined]
    seeder._tenant_id = None  # type: ignore[attr-defined]
    return seeder


@pytest.mark.asyncio
async def test_a_homonym_with_a_divergent_dag_is_refused_BEFORE_it_is_duplicated() -> None:
    """Le défaut central de T5.2 : le contrôle arrivait un run trop tard.

    Il ne levait que sur `len(homonyms) > 1`. Or dans le scénario que T5.2
    nomme — un template recréé, donc un UUID différent, donc une empreinte de
    DAG différente — il y a à cet instant EXACTEMENT UN homonyme : la garde
    passait, `create_workflow` insérait la seconde ligne, et le rapport disait
    « créé ». Le refus n'arrivait qu'à l'exécution suivante, et il était alors
    définitif (aucun chemin de suppression de workflow n'existe).
    """
    from scripts.seed_dev import DEV_ENTRY_WORKFLOW_NAME, SeedError

    stale = _FakeWorkflowRow(DEV_ENTRY_WORKFLOW_NAME, {"dev_lead": uuid.uuid4()})
    seeder = _workflow_seeder({DEV_ENTRY_WORKFLOW_NAME: [stale]})

    with pytest.raises(SeedError) as excinfo:
        await seeder._assert_entry_workflow_can_be_created(
            {"dev_lead": uuid.uuid4(), "code_researcher": uuid.uuid4()},
            {("dev_lead", "code_researcher")},
        )

    assert str(stale.id) in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_matching_homonym_is_left_to_the_idempotent_replay() -> None:
    """Un homonyme dont le DAG monte les mêmes templates est la ligne attendue."""
    from scripts.seed_dev import DEV_ENTRY_WORKFLOW_NAME

    nodes = {"dev_lead": uuid.uuid4(), "code_researcher": uuid.uuid4()}
    edges = [("dev_lead", "code_researcher")]
    seeder = _workflow_seeder(
        {DEV_ENTRY_WORKFLOW_NAME: [_FakeWorkflowRow(DEV_ENTRY_WORKFLOW_NAME, nodes, edges)]}
    )

    await seeder._assert_entry_workflow_can_be_created(dict(nodes), set(edges))


@pytest.mark.asyncio
async def test_two_homonyms_stop_the_provisioning_and_name_them_both() -> None:
    from scripts.seed_dev import DEV_ENTRY_WORKFLOW_NAME, SeedError

    nodes = {"dev_lead": uuid.uuid4(), "code_researcher": uuid.uuid4()}
    edges = [("dev_lead", "code_researcher")]
    rows = [
        _FakeWorkflowRow(DEV_ENTRY_WORKFLOW_NAME, nodes, edges),
        _FakeWorkflowRow(DEV_ENTRY_WORKFLOW_NAME, nodes, edges),
    ]
    seeder = _workflow_seeder({DEV_ENTRY_WORKFLOW_NAME: rows})

    with pytest.raises(SeedError) as excinfo:
        await seeder._assert_entry_workflow_can_be_created(dict(nodes), set(edges))

    message = str(excinfo.value)
    assert all(str(row.id) in message for row in rows)


@pytest.mark.asyncio
async def test_every_legacy_homonym_is_named_not_just_the_first() -> None:
    """`legacy[0].id` n'en citait qu'un — alors que la non-unicité est le sujet.

    ⚠️ Story 5.3 : le helper est devenu `_report_legacy_entry_workflows` (il y a
    désormais DEUX générations héritées). La propriété gardée, elle, est
    inchangée — et c'est la raison pour laquelle ce test a été réécrit plutôt
    que supprimé.
    """
    from scripts.seed_dev import DEV_LEAD_WORKFLOW_NAME_V1

    rows = [
        _FakeWorkflowRow(DEV_LEAD_WORKFLOW_NAME_V1, {"dev_lead": uuid.uuid4()}),
        _FakeWorkflowRow(DEV_LEAD_WORKFLOW_NAME_V1, {"dev_lead": uuid.uuid4()}),
    ]
    seeder = _workflow_seeder({DEV_LEAD_WORKFLOW_NAME_V1: rows})
    report = SeedReport()

    await seeder._report_legacy_entry_workflows(report)

    assert len(report.unchanged) == 1
    assert all(str(row.id) in report.unchanged[0] for row in rows)


def test_a_blank_tool_name_is_refused_by_the_catalog_not_by_the_provisioning() -> None:
    """Revue 5.2 — `min_length=1` portait sur la LISTE, pas sur ses éléments.

    `tools: ["  ", ""]` était accepté, et le refus n'arrivait qu'au
    provisioning, avec un message où le nom manquant est invisible :
    « n'expose plus :   , ».
    """
    import pydantic

    from agentive_backend.features.agent_registry.dev_catalog import DevMcpServerDefinition

    with pytest.raises(pydantic.ValidationError):
        DevMcpServerDefinition.model_validate(
            {
                "key": "code_search",
                "name": "dev-code-search",
                "launcher": "code_search",
                "tools": ["read_file", "  "],
            }
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# include_raw_previous_output — Story 5.3 T1.5
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _probe(**overrides: object) -> Any:
    from agentive_backend.features.agent_registry.dev_catalog import DevAgentDefinition

    payload: dict[str, object] = {
        "key": "probe",
        "name": "Probe",
        "archetype": "analyste",
        "system_prompt": "x",
        "input_contract": {"core": {}, "extras": {}},
        "output_contract": {"core": {}, "extras": {}},
    }
    payload.update(overrides)
    return DevAgentDefinition.model_validate(payload)


def test_the_handoff_opt_out_is_a_config_key_the_provisioning_owns() -> None:
    """T1.5 — `_OWNED_CONFIG_KEYS` décide de ce que le seed COMPARE.

    Une clé absente de ce tuple est écrite une fois par `update_template` puis
    plus jamais vérifiée : une divergence entre le YAML et la base passerait
    pour « inchangé ». C'est le défaut que la normalisation de `push_memory` a
    déjà dû fermer une fois.
    """
    desired = desired_config_subset(_probe(include_raw_previous_output=True))
    assert desired["include_raw_previous_output"] is True


def test_a_catalog_that_does_not_declare_the_opt_out_claims_no_key() -> None:
    """Le catalogue ne possède que ce qu'il déclare.

    Émettre `False` par défaut ferait écrire la clé sur TOUS les templates du
    pôle, y compris ceux de la 5.1 et de la 5.2 — donc un `update_template` et
    un event d'audit sur une intention que personne n'a exprimée.
    """
    assert "include_raw_previous_output" not in desired_config_subset(_probe())


def test_a_catalog_that_turns_the_opt_out_off_still_owns_the_key() -> None:
    """`False` n'est pas `None` : le filtre du seed doit les distinguer.

    Un filtre écrit `if value` (au lieu de `if value is not None`) ferait
    disparaître un opt-out explicitement remis à `False`, et le provisioning
    cesserait de garder cette décision.
    """
    desired = desired_config_subset(_probe(include_raw_previous_output=False))
    assert desired["include_raw_previous_output"] is False


# ─── T5 — le DAG d'entrée passe à QUATRE nodes (Story 5.3) ───────────────


def test_the_entry_workflow_mounts_the_four_dev_nodes() -> None:
    """Le DAG d'entrée monte le pôle tel qu'il existe, pas une moitié.

    `_ENTRY_NODE_KEYS` est ce que `_provision_entry_workflow` exige d'avoir
    provisionné avant de créer le workflow : une clé oubliée ici donne un
    workflow amputé qui se lance quand même.
    """
    from scripts.seed_dev import _ENTRY_NODE_KEYS

    assert _ENTRY_NODE_KEYS == (
        "dev_lead",
        "code_researcher",
        "architect_analyst",
        "code_producer",
    )


def test_the_entry_workflow_name_is_versioned_a_third_time() -> None:
    """T5.3 — `workflows.name` n'est pas unique et l'empreinte couvre le DAG.

    Passer de deux à quatre nodes change l'empreinte : sans changement de nom,
    le seed créerait une TROISIÈME ligne homonyme et le `workflow_id` que John
    a noté pointerait toujours sur l'ancienne.
    """
    from scripts.seed_dev import (
        DEV_ENTRY_WORKFLOW_NAME,
        DEV_ENTRY_WORKFLOW_NAME_V2,
        DEV_LEAD_WORKFLOW_NAME_V1,
    )

    names = {DEV_ENTRY_WORKFLOW_NAME, DEV_ENTRY_WORKFLOW_NAME_V2, DEV_LEAD_WORKFLOW_NAME_V1}
    assert len(names) == 3, "chaque génération doit porter un nom distinct"


def test_both_previous_generations_are_looked_up_as_legacy() -> None:
    """Il y a désormais DEUX générations héritées, pas une.

    Ne chercher que la v1 laisserait le workflow à deux nodes de la Story 5.2
    invisible dans le rapport — donc un opérateur qui lance l'ancien `workflow_id`
    sans jamais apprendre qu'il existe une suite.
    """
    from scripts.seed_dev import (
        _LEGACY_ENTRY_WORKFLOW_NAMES,
        DEV_ENTRY_WORKFLOW_NAME_V2,
        DEV_LEAD_WORKFLOW_NAME_V1,
    )

    assert _LEGACY_ENTRY_WORKFLOW_NAMES == (
        DEV_LEAD_WORKFLOW_NAME_V1,
        DEV_ENTRY_WORKFLOW_NAME_V2,
    )


@pytest.mark.asyncio
async def test_the_legacy_report_names_each_generation_separately() -> None:
    """Deux générations, deux lignes de rapport — et chacune nomme SES rows.

    Les fondre en une seule ligne redonnerait le défaut que la revue de la 5.2
    a corrigé sur `legacy[0].id` : un rapport qui masque des lignes en base.
    """
    from scripts.seed_dev import DEV_ENTRY_WORKFLOW_NAME_V2, DEV_LEAD_WORKFLOW_NAME_V1

    v1 = _FakeWorkflowRow(DEV_LEAD_WORKFLOW_NAME_V1, {"dev_lead": uuid.uuid4()})
    v2 = _FakeWorkflowRow(
        DEV_ENTRY_WORKFLOW_NAME_V2, {"dev_lead": uuid.uuid4(), "code_researcher": uuid.uuid4()}
    )
    seeder = _workflow_seeder({DEV_LEAD_WORKFLOW_NAME_V1: [v1], DEV_ENTRY_WORKFLOW_NAME_V2: [v2]})
    report = SeedReport()

    await seeder._report_legacy_entry_workflows(report)

    assert len(report.unchanged) == 2
    joined = "\n".join(report.unchanged)
    assert str(v1.id) in joined
    assert str(v2.id) in joined
    assert DEV_LEAD_WORKFLOW_NAME_V1 in joined
    assert DEV_ENTRY_WORKFLOW_NAME_V2 in joined


@pytest.mark.asyncio
async def test_a_generation_with_no_row_produces_no_report_line() -> None:
    """Une base neuve n'a aucun workflow hérité : ne rien dire est la vérité."""
    seeder = _workflow_seeder({})
    report = SeedReport()

    await seeder._report_legacy_entry_workflows(report)

    assert report.unchanged == []


# ─────────────────────────────────────────────────────────────────────────────
# Revue de la Story 5.3 — le garde-fou d'unicité et la sémantique PATCH.
# ─────────────────────────────────────────────────────────────────────────────


class _FakeDagRow:
    """Une row de `workflows` minimale dont le `dag` est fourni TEL QUEL.

    Distincte de `_FakeWorkflowRow`, qui construit toujours un mapping bien
    formé : ici on veut pouvoir passer du texte JSON, ou un DAG illisible.
    """

    def __init__(self, row_id: UUID, dag: Any) -> None:
        self.id = row_id
        self.dag = dag


def _dag_payload(nodes: dict[str, UUID], edges: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "nodes": [
            {"node_id": node_id, "agent_template_id": str(template_id)}
            for node_id, template_id in nodes.items()
        ],
        "edges": [{"from_node_id": source, "to_node_id": target} for source, target in edges],
    }


def test_two_homonyms_differing_only_by_their_edges_are_seen_as_divergent() -> None:
    """L'empreinte d'idempotence couvre le DAG ENTIER, arêtes comprises.

    Comparer les seuls nodes laissait passer un homonyme de même topologie de
    nodes mais d'arêtes différentes : la garde rendait sans lever, puis
    `create_workflow` — voyant une empreinte différente — insérait une SECONDE
    ligne homonyme en rapportant « créé ». C'est le défaut que la revue de la
    5.2 avait fait corriger sur les nodes, revenu par les arêtes dès que la
    5.3 a rendu celles-ci variables.
    """
    nodes = {key: uuid4() for key in ("dev_lead", "code_researcher", "architect_analyst")}
    chain = [("dev_lead", "code_researcher"), ("code_researcher", "architect_analyst")]
    fan_out = [("dev_lead", "code_researcher"), ("dev_lead", "architect_analyst")]

    row = _FakeDagRow(uuid4(), _dag_payload(nodes, fan_out))
    # Les nodes sont identiques : c'est exactement ce qui rendait l'ancien
    # contrôle aveugle.
    assert _dag_nodes_of(row) == nodes
    assert _dag_edges_of(row) != set(chain)


def test_a_dag_stored_as_json_text_is_read_not_mistaken_for_an_empty_one() -> None:
    """`workflows.dag` revient tantôt en dict, tantôt en texte JSON.

    Le repli `else {}` classait la forme texte comme « DAG vide », donc
    divergente, donc un refus DÉFINITIF au message faux (« ne monte pas les
    templates attendus ») — alors que le seul chemin de sortie est un `DELETE`
    manuel. Le test E2E du même pôle fait déjà ce `json.loads` de repli : la
    forme est un état connu du dépôt.
    """
    nodes = {"dev_lead": uuid4(), "code_researcher": uuid4()}
    edges = [("dev_lead", "code_researcher")]
    row = _FakeDagRow(uuid4(), json.dumps(_dag_payload(nodes, edges)))

    assert _dag_nodes_of(row) == nodes
    assert _dag_edges_of(row) == set(edges)


def test_a_dag_that_cannot_be_read_at_all_is_distinguished_from_an_empty_one() -> None:
    """« Illisible » et « vide » ne sont pas le même constat.

    Les confondre produisait un refus au message faux. `_dag_of` rend `None`
    pour que la garde puisse le dire, plutôt que d'accuser le DAG de monter
    les mauvais templates.
    """
    assert _dag_of(_FakeDagRow(uuid4(), "{ pas du JSON")) is None
    assert _dag_of(_FakeDagRow(uuid4(), 42)) is None
    assert _dag_edges_of(_FakeDagRow(uuid4(), "{ pas du JSON")) is None
    # Un DAG lisible mais sans arêtes reste un DAG lisible : `set()`, pas `None`.
    assert _dag_edges_of(_FakeDagRow(uuid4(), {"nodes": []})) == set()


def test_a_handoff_opt_out_left_at_false_does_not_wedge_the_provisioning() -> None:
    """`include_raw_previous_output: false` équivaut à son absence.

    Le moteur ne reconnaît QUE le littéral `True`. Un `false` posé par l'API
    — usage que le champ encourage — n'a donc aucun effet, mais il bloquait
    DÉFINITIVEMENT tout `make seed-dev` via `stale_owned_keys`, sans chemin de
    retour : `merge_updates` traite `None` comme « ne touche pas » et
    `to_mapping` n'émet la clé que `is not None`, donc l'API ne peut pas la
    remettre à `null`.
    """
    desired = {"system_prompt": "x"}
    stored = {"system_prompt": "x", "include_raw_previous_output": False}
    assert stale_owned_keys(stored, desired) == []


def test_a_handoff_opt_out_left_at_true_is_still_reported_as_stale() -> None:
    """La contrepartie, sans quoi le correctif précédent serait une cécité.

    `True` change réellement le régime de passage du node : le laisser en base
    alors que le catalogue ne le déclare plus est une config périmée, et le
    provisioning doit toujours s'arrêter dessus.
    """
    desired = {"system_prompt": "x"}
    stored = {"system_prompt": "x", "include_raw_previous_output": True}
    assert stale_owned_keys(stored, desired) == ["include_raw_previous_output"]


def test_the_neutral_value_exception_does_not_leak_to_other_keys() -> None:
    """`0 == False` en Python : l'exception doit porter sur l'identité.

    Sans le `is`, un plafond réglé à zéro sur une autre clé possédée serait
    confondu avec un booléen éteint, et l'exception posée pour UNE clé
    deviendrait contagieuse.
    """
    assert not _is_neutral_stored_value("llm_params", 0)
    assert not _is_neutral_stored_value("include_raw_previous_output", 0)
    assert _is_neutral_stored_value("include_raw_previous_output", False)
