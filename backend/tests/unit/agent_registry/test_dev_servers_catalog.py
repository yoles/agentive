"""Déclaration des serveurs MCP du Pôle Dev — Story 5.2 T3.1 / T7.3.

Deux familles, comme pour les agents : le LOADER, et le CATALOGUE LIVRÉ dont
les propriétés sont des AC.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentive_backend.features.agent_registry.dev_catalog import (
    DEV_SERVERS_FILENAME,
    load_dev_catalog,
    load_dev_servers,
)

_AGENT = """
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

_SERVERS = """
servers:
  - key: code_search
    name: dev-code-search
    launcher: code_search
    tools: [read_file, find_files]
"""


def _write(directory: Path, name: str, body: str) -> Path:
    (directory / name).write_text(body, encoding="utf-8")
    return directory


# ─── Loader ──────────────────────────────────────────────────────────


def test_a_directory_without_a_servers_file_declares_no_server(tmp_path: Path) -> None:
    """Un pôle sans serveur MCP est un état LÉGITIME — c'était celui de la
    Story 5.1. L'absence rend un tuple vide, pas une erreur."""
    assert load_dev_servers(_write(tmp_path, "probe.yaml", _AGENT)) == ()


def test_the_servers_file_is_not_loaded_as_an_agent(tmp_path: Path) -> None:
    """Les deux documents vivent dans le même répertoire — c'est ce que T3.1
    demande — et ils n'ont pas la même forme. Sans exclusion explicite,
    `load_dev_catalog` validerait `mcp-servers.yaml` comme un agent et
    échouerait sur `extra="forbid"`."""
    _write(tmp_path, "probe.yaml", _AGENT)
    _write(tmp_path, DEV_SERVERS_FILENAME, _SERVERS)

    assert set(load_dev_catalog(tmp_path)) == {"probe"}
    assert [server.key for server in load_dev_servers(tmp_path)] == ["code_search"]


def test_an_unknown_field_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, DEV_SERVERS_FILENAME, _SERVERS + "    timeout_s: 30\n")
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_dev_servers(tmp_path)


def test_an_unknown_launcher_is_refused(tmp_path: Path) -> None:
    """`launcher` est un jeu FERMÉ, et c'est une décision de sécurité : un
    champ libre ferait de ce YAML une surface d'exécution arbitraire — éditer
    une ligne suffirait à lancer n'importe quel module Python dans le
    sous-processus sandboxé."""
    _write(
        tmp_path,
        DEV_SERVERS_FILENAME,
        "servers:\n  - key: evil\n    name: evil\n    launcher: os.system\n    tools: [x]\n",
    )
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_dev_servers(tmp_path)


def test_a_server_declaring_no_tool_is_refused(tmp_path: Path) -> None:
    """Un serveur sans outil déclaré rendrait le contrôle de la seconde
    exécution vide de sens : il passerait toujours."""
    _write(
        tmp_path,
        DEV_SERVERS_FILENAME,
        "servers:\n  - key: empty\n    name: empty\n    launcher: code_search\n    tools: []\n",
    )
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_dev_servers(tmp_path)


def test_two_servers_sharing_a_name_are_refused(tmp_path: Path) -> None:
    """`tool_servers` porte UNIQUE(name, tenant_id) : le second ferait lever
    `ConflictError` au provisioning sans que rien ne dise pourquoi."""
    _write(
        tmp_path,
        DEV_SERVERS_FILENAME,
        "servers:\n"
        "  - key: a\n    name: same\n    launcher: code_search\n    tools: [x]\n"
        "  - key: b\n    name: same\n    launcher: code_search\n    tools: [y]\n",
    )
    with pytest.raises(RuntimeError, match="duplicate server name"):
        load_dev_servers(tmp_path)


def test_a_duplicated_tool_name_is_refused(tmp_path: Path) -> None:
    _write(
        tmp_path,
        DEV_SERVERS_FILENAME,
        "servers:\n  - key: a\n    name: a\n    launcher: code_search\n    tools: [x, x]\n",
    )
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_dev_servers(tmp_path)


def test_a_duplicate_key_inside_the_file_is_refused(tmp_path: Path) -> None:
    """`yaml.safe_load` résout un doublon en « dernier gagnant », sans un mot —
    ce que produit un conflit de merge mal résolu. Le `_StrictLoader` du
    catalogue d'agents s'applique ici aussi."""
    _write(tmp_path, DEV_SERVERS_FILENAME, _SERVERS + "servers: []\n")
    with pytest.raises(RuntimeError, match="YAML parse error"):
        load_dev_servers(tmp_path)


def test_a_non_mapping_document_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, DEV_SERVERS_FILENAME, "- just\n- a list\n")
    with pytest.raises(RuntimeError, match="expected mapping"):
        load_dev_servers(tmp_path)


# ─── Le catalogue réellement livré ───────────────────────────────────


def test_the_shipped_catalog_declares_the_code_search_server() -> None:
    servers = load_dev_servers()
    assert [server.key for server in servers] == ["code_search"]
    assert servers[0].name == "dev-code-search"
    assert servers[0].transport == "stdio"


def test_the_declared_tools_are_exactly_what_the_code_researcher_assigns() -> None:
    """Le lien que le provisioning ne peut PAS rattraper tout seul.

    `_assign_tools` résout les noms du template contre la table `tools`, qui
    est peuplée par la découverte du serveur. Un outil déclaré par le template
    et absent du serveur fait échouer le provisioning — après avoir créé le
    template. Ici l'écart est attrapé sans base de données.
    """
    declared = set(load_dev_servers()[0].tools)
    assigned = set(load_dev_catalog()["code_researcher"].tools)
    assert assigned == declared, (
        f"le template assigne {sorted(assigned)} et le serveur déclare {sorted(declared)}"
    )


def test_every_declared_tool_is_actually_exposed_by_the_server_module() -> None:
    """La troisième surface, et celle qui rendait le test précédent circulaire.

    Comparer le YAML au YAML ne prouve rien sur le PROGRAMME : les deux
    pourraient nommer un outil que le serveur n'expose pas, et l'écart
    n'apparaîtrait qu'à la découverte, en base, au provisioning.
    """
    from agentive_backend.infra.mcp.servers.code_search import tool_definitions

    exposed = {tool.name for tool in tool_definitions()}
    assert set(load_dev_servers()[0].tools) <= exposed


def test_every_dev_template_assigns_only_tools_the_server_really_declares() -> None:
    """T6.3 — la parité catalogue ↔ serveur, pour TOUS les agents outillés.

    ⚠️ Revue de la Story 5.3 : la parité n'était gardée que pour le
    Chercheur. Les deux nouveaux agents n'étaient couverts que par un
    `set(definition.tools) <= read_only` dont l'ensemble de droite était
    RECOPIÉ EN DUR dans le test. Si `mcp-servers.yaml` perdait `find_files`,
    le test du Chercheur tombait (il compare par égalité) et celui du
    Producteur passait — contre un littéral périmé — pendant que le
    provisioning échouait en base. C'est exactement l'écart que T6.3 demande
    d'attraper SANS base de données.

    La propriété gardée est l'inclusion et non l'égalité : un agent a le droit
    de n'assigner qu'un sous-ensemble des outils du serveur (l'Analyste en
    prend moins que le Producteur, et chaque outil de plus est proposé au
    modèle à chaque itération, donc facturé).
    """
    declared = set(load_dev_servers()[0].tools)
    for key, definition in load_dev_catalog().items():
        assigned = set(definition.tools)
        assert assigned <= declared, (
            f"{key} assigne {sorted(assigned - declared)}, que le serveur ne déclare pas : "
            "le provisioning échouera APRÈS avoir créé le template"
        )
