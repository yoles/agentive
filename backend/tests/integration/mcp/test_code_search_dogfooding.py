"""Le Code Researcher sur Agentive lui-même — Story 5.2 AC3 / T6.

Le corpus est le dépôt monté dans le container (``/app``, le volume
``./backend`` du ``docker-compose.yml``), et les « modules M2-M12 » du PRD sont
les packages de ``features/`` (table de correspondance :
``docs/decisions/module-naming.md``).

**Ce que ce module prouve.** Que le serveur de lecture de code, interrogé par
le VRAI transport MCP, rend des chemins qui EXISTENT sur le disque, couvrent
plusieurs modules, et font apparaître la structure récurrente qu'un nouveau
module M13 devrait reprendre.

**Ce qu'il ne prouve pas, et ce n'est pas un détail.** Que le JUGEMENT du
modèle est pertinent. Aucun LLM n'intervient ici : les requêtes sont écrites à
la main. Un test qui ferait produire la réponse par un `MockProvider` puis
vérifierait cette réponse ne prouverait que la fixture — c'est exactement le
défaut que la revue de la Story 5.1 a trouvé dans SON test d'AC3, circulaire
parce qu'il validait une sortie construite depuis les mêmes constantes. Le
jugement du modèle relève du protocole manuel du runbook
(``docs/runbooks/pole-dev.md`` § « Juger la qualité d'exploration »), dont la
table de résultat est versionnée et vide tant qu'il n'a pas tourné.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentive_backend.infra.mcp.client import call_tool, discover_tools
from agentive_backend.infra.mcp.policy import (
    apply_sandbox_policy,
    code_search_connection_config,
)
from agentive_backend.shared.config import settings

#: Le corpus, et il n'y en a qu'un possible : la PREMIÈRE racine autorisée.
#:
#: ⚠️ La version d'origine calculait un repli hors container
#: (`Path(__file__).parents[3]`) et le passait à
#: `code_search_connection_config((str(_CORPUS),))`. **Ce repli était inerte** :
#: `apply_sandbox_policy` ignore le `connection_config` reçu et réécrit
#: `args` depuis `Settings` — vérifié dans le container par la revue 5.2, une
#: config construite avec `--root /srv/faux-corpus` en ressort avec
#: `--root /app`. Le serveur était donc TOUJOURS lancé sur les racines de
#: `Settings`, et hors container il mourait au spawn sur « racine autorisée
#: inexistante », pas sur le skip que la docstring promettait.
#:
#: Lire la racine depuis `Settings` est donc la seule façon de ne pas mentir
#: sur ce qui est testé.
_CORPUS = Path(settings.dev_code_roots[0])
_FEATURES = _CORPUS / "src" / "agentive_backend" / "features"

#: Sauté plutôt que faux : hors du container, la racine autorisée n'existe pas
#: et chacun des six tests échouerait sur une erreur de spawn opaque.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _FEATURES.is_dir(),
        reason=(
            f"corpus absent ({_FEATURES}) — ce module lit le dépôt monté dans le container, "
            "et `apply_sandbox_policy` impose les racines de Settings"
        ),
    ),
]

#: Aligné sur le budget de la PRODUCTION (`DEFAULT_DISCOVERY_TIMEOUT_S = 10`).
#: À 30 s, le test restait vert pendant qu'un `make seed-dev` échouait en
#: `DependencyError` 503 : il triplait silencieusement le budget qu'il atteste.
_CALL_TIMEOUT_S = 10.0


async def _call(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Un appel d'outil par le VRAI transport stdio, sandbox comprise."""
    profile, effective = apply_sandbox_policy(
        transport="stdio", connection_config=code_search_connection_config((str(_CORPUS),))
    )
    raw = await call_tool(
        transport="stdio",
        connection_config=effective,
        tool_name=tool_name,
        arguments=arguments,
        profile=profile,
        timeout=_CALL_TIMEOUT_S,
    )
    return json.loads(raw["content"][0]["text"])


def _modules_of(paths: list[str]) -> set[str]:
    """Le nom de package `features/<module>/…` pour chaque chemin rendu."""
    modules: set[str] = set()
    for path in paths:
        try:
            relative = Path(path).relative_to(_FEATURES)
        except ValueError:
            continue
        if relative.parts:
            modules.add(relative.parts[0])
    return modules


@pytest.mark.asyncio
async def test_the_server_exposes_its_four_read_tools_over_the_real_transport() -> None:
    profile, effective = apply_sandbox_policy(
        transport="stdio", connection_config=code_search_connection_config((str(_CORPUS),))
    )
    tools = await discover_tools(
        transport="stdio", connection_config=effective, profile=profile, timeout=_CALL_TIMEOUT_S
    )
    assert {tool.name for tool in tools} == {
        "list_directory",
        "read_file",
        "find_files",
        "search_content",
    }


@pytest.mark.asyncio
async def test_looking_for_the_shape_of_a_new_module_finds_several_existing_ones() -> None:
    """AC3 — « le Code Researcher retrouve les patterns des modules existants ».

    La question posée est celle qu'un M13 pose vraiment : « à quoi ressemble
    un module ici ? ». L'assertion porte sur le DISQUE et sur le nombre de
    modules distincts couverts, pas sur une liste attendue — une liste
    attendue serait un inventaire de plus à maintenir, et elle ne prouverait
    rien de plus.
    """
    result = await _call(
        "find_files", {"pattern": "**/router.py", "path": str(_FEATURES), "max_results": 50}
    )

    matches = result["matches"]
    assert matches, "aucun `router.py` trouvé dans `features/` — le corpus n'est pas celui attendu"
    for path in matches:
        assert Path(path).is_file(), f"chemin rendu inexistant sur le disque : {path}"

    modules = _modules_of(matches)
    assert len(modules) >= 2, f"un seul module couvert ({modules}) — ce n'est pas un pattern"


@pytest.mark.asyncio
async def test_the_recurring_structure_of_a_module_is_visible_from_the_tools() -> None:
    """T6.2 — `router.py` / `service.py` / `schemas.py`, la baseline d'un M13.

    Chaque fichier est cherché séparément : un module peut légitimement ne pas
    porter les trois, et c'est la RÉCURRENCE (le même trio dans plusieurs
    modules) qui fait le pattern, pas sa présence partout.
    """
    structure: dict[str, set[str]] = {}
    for filename in ("router.py", "service.py", "schemas.py"):
        result = await _call(
            "find_files", {"pattern": filename, "path": str(_FEATURES), "max_results": 50}
        )
        for path in result["matches"]:
            assert Path(path).is_file(), path
        structure[filename] = _modules_of(result["matches"])

    complete = structure["router.py"] & structure["service.py"] & structure["schemas.py"]
    assert len(complete) >= 2, (
        f"le trio router/service/schemas n'est complet que dans {complete} — "
        "il ne constitue pas une baseline observable pour un nouveau module"
    )


@pytest.mark.asyncio
async def test_the_shared_conventions_a_new_module_must_follow_are_findable() -> None:
    """Les deux autres points que T6.2 nomme : le repo dans
    `shared/repositories/`, les events dans `shared/contracts/events/`."""
    repositories = await _call(
        "list_directory",
        {"path": str(_CORPUS / "src" / "agentive_backend" / "shared" / "repositories")},
    )
    assert any(entry["name"].endswith("_repo.py") for entry in repositories["entries"])

    events = await _call(
        "list_directory",
        {"path": str(_CORPUS / "src" / "agentive_backend" / "shared" / "contracts" / "events")},
    )
    # Revue 5.2 — `len(entries) >= 2` comptait des ENTRÉES DE RÉPERTOIRE :
    # un dossier contenant `__init__.py` et rien d'autre satisfaisait
    # l'assertion, alors que la docstring promet « les contrats d'événement
    # sont trouvables ».
    contracts = [
        entry["name"]
        for entry in events["entries"]
        if entry["type"] == "file"
        and entry["name"].endswith(".py")
        and entry["name"] != "__init__.py"
    ]
    assert len(contracts) >= 2, f"aucun contrat d'événement trouvable : {events['entries']}"


@pytest.mark.asyncio
async def test_a_content_search_returns_real_lines_of_real_files() -> None:
    """`search_content` est le « grep/ripgrep » de l'AC1, et il doit rendre de
    quoi VÉRIFIER : un chemin, un numéro de ligne, et la ligne elle-même."""
    result = await _call(
        "search_content",
        {"pattern": r"^class \w+Service", "path": str(_FEATURES), "max_results": 25},
    )

    # Si le budget avait été consommé avant le second module, l'assertion de
    # couverture ci-dessous casserait sans rapport avec ce qu'elle teste.
    assert not result["truncated"], (
        f"résultat tronqué, la couverture n'est pas mesurable : {result.get('note')}"
    )
    assert result["matches"], "aucune classe `*Service` trouvée dans `features/`"
    for match in result["matches"]:
        path = Path(match["path"])
        assert path.is_file(), match
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        assert 1 <= match["line"] <= len(lines)
        # `match["text"][:40]` vide rendrait `startswith("")` toujours vrai :
        # la vérification chemin/ligne/texte disparaissait en silence.
        assert match["text"], f"correspondance sans texte : {match}"
        assert lines[match["line"] - 1].startswith(match["text"][:40])

    assert len(_modules_of([match["path"] for match in result["matches"]])) >= 2


@pytest.mark.asyncio
async def test_reading_a_file_of_the_repo_returns_its_real_content() -> None:
    target = _FEATURES / "agent_registry" / "dev_catalog.py"
    result = await _call("read_file", {"path": str(target), "max_bytes": 400})

    assert result["path"] == str(target)
    assert result["total_bytes"] == target.stat().st_size
    # Revue 5.2 — la version d'origine lisait 400 octets et n'en comparait que
    # 100 : 300 octets rendus n'étaient confrontés à rien.
    assert target.read_bytes().startswith(result["content"].encode("utf-8"))
    # Un fichier de cette taille NE TIENT PAS dans 400 octets : la troncature
    # doit être annoncée, et reprenable.
    assert result["truncated"] is True
    assert result["next_offset"] == result["bytes_returned"]

    # ⚠️ Et « reprenable » se REPREND. L'assertion d'origine s'arrêtait à
    # `next_offset == bytes_returned` — vraie par construction pour une lecture
    # démarrée à l'offset 0, donc ne testant aucun code de reprise. Or
    # `read_file(offset=…)` est le SEUL mécanisme permettant au modèle de lire
    # au-delà du plafond, et il n'avait aucune couverture.
    following = await _call(
        "read_file", {"path": str(target), "offset": result["next_offset"], "max_bytes": 400}
    )
    assert following["offset"] == result["next_offset"]
    rebuilt = (result["content"] + following["content"]).encode("utf-8")
    assert target.read_bytes().startswith(rebuilt)


@pytest.mark.asyncio
async def test_the_repository_secrets_stay_out_of_reach_over_the_real_transport() -> None:
    """La garde de chemin, vue depuis le protocole et non depuis `dispatch`.

    Deux choses à prouver ici que les unitaires ne peuvent pas : que le refus
    traverse MCP en `isError` (donc en `MCPToolError`) plutôt qu'en plantage
    du sous-processus, et que le message reste exploitable par un agent.
    """
    from agentive_backend.infra.mcp.client import MCPToolError

    with pytest.raises(MCPToolError) as excinfo:
        await _call("read_file", {"path": "/etc/passwd"})
    assert "hors des racines autorisées" in excinfo.value.detail

    with pytest.raises(MCPToolError) as excinfo:
        await _call("read_file", {"path": str(_CORPUS / ".env")})
    assert "deny-list" in excinfo.value.detail
