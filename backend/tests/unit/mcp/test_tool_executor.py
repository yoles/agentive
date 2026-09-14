"""Story 5.0 AC2/AC3 — l'exécuteur MCP injecté dans la boucle d'outils."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agentive_backend.infra.mcp.client import MCPExecutionTimeoutError
from agentive_backend.infra.mcp.tool_executor import (
    MAX_TOOL_RESULT_CHARS,
    McpToolExecutor,
    ResolvedTool,
    to_tool_definitions,
)
from agentive_backend.shared.llm.types import ToolCall

_MODULE = "agentive_backend.infra.mcp.tool_executor.call_tool"


def _tool(name: str = "grep") -> ResolvedTool:
    return ResolvedTool(
        tool_id=uuid4(),
        server_id=uuid4(),
        name=name,
        description="cherche",
        input_schema={"type": "object"},
        transport="stdio",
        connection_config={"command": "x"},
    )


def _result(text: str = "trouvé", *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


@pytest.mark.asyncio
async def test_a_successful_call_is_rendered_wrapped_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _tool()
    monkeypatch.setattr(_MODULE, AsyncMock(return_value=_result("ligne 42")))
    executor = McpToolExecutor(resolved={tool.name: tool})

    out = await executor(ToolCall(id="t1", name="grep", arguments={"q": "TODO"}))

    assert "ligne 42" in out
    assert out.startswith("<tool_output>")
    assert len(executor.invocations) == 1
    assert executor.invocations[0].status == "success"
    assert executor.invocations[0].tool_id == tool.tool_id


@pytest.mark.asyncio
async def test_every_exit_of_this_class_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 — l'enveloppe anti-injection est posée à l'UNIQUE sortie, donc
    aucun appelant ne peut l'oublier. C'est le 4ᵉ constructeur de prompt du
    dépôt ; l'oubli sur le 3ᵉ (`PlaygroundService`) a survécu deux epics.

    Les quatre chemins de sortie sont couverts ici, pas seulement le nominal :
    succès, `isError`, timeout, et outil inconnu."""
    tool = _tool()
    executor = McpToolExecutor(resolved={tool.name: tool})
    call = ToolCall(id="t1", name="grep")

    monkeypatch.setattr(_MODULE, AsyncMock(return_value=_result("ok")))
    nominal = await executor(call)

    monkeypatch.setattr(_MODULE, AsyncMock(return_value=_result("boum", is_error=True)))
    errored = await executor(call)

    monkeypatch.setattr(_MODULE, AsyncMock(side_effect=MCPExecutionTimeoutError(timeout=15.0)))
    timed_out = await executor(call)

    monkeypatch.setattr(_MODULE, AsyncMock(side_effect=RuntimeError("inattendu")))
    crashed = await executor(call)

    unknown = await executor(ToolCall(id="t2", name="jamais-assigne"))

    for out in (nominal, errored, timed_out, crashed, unknown):
        assert out.startswith("<tool_output>"), out
        assert out.endswith("</tool_output>"), out


@pytest.mark.asyncio
async def test_an_mcp_is_error_result_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP signale un échec d'outil DANS la charge utile, pas en levant.
    Le traiter comme un succès rendrait au modèle un message d'erreur mis en
    forme comme un résultat."""
    tool = _tool()
    monkeypatch.setattr(
        _MODULE, AsyncMock(return_value=_result("permission denied", is_error=True))
    )
    executor = McpToolExecutor(resolved={tool.name: tool})

    await executor(ToolCall(id="t1", name="grep"))

    assert executor.invocations[0].status == "error"


@pytest.mark.asyncio
async def test_a_timeout_is_reported_to_the_model_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tuer le run jetterait tout ce qui a déjà été facturé ; le modèle, lui,
    peut essayer autre chose ou dire qu'il n'a pas pu savoir."""
    tool = _tool()
    monkeypatch.setattr(_MODULE, AsyncMock(side_effect=MCPExecutionTimeoutError(timeout=15.0)))
    executor = McpToolExecutor(resolved={tool.name: tool})

    out = await executor(ToolCall(id="t1", name="grep"))

    assert "timed out" in out
    assert executor.invocations[0].status == "timeout"


@pytest.mark.asyncio
async def test_an_unexpected_exception_message_never_reaches_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NFR9 — le message d'une exception peut porter une DSN, un chemin ou un
    token, et cette chaîne part DIRECTEMENT dans un prompt. Seul le type est
    rendu ; la trace complète va au log."""
    tool = _tool()
    secret = "postgresql://user:SUPERSECRET@db:5432/x"
    monkeypatch.setattr(_MODULE, AsyncMock(side_effect=RuntimeError(secret)))
    executor = McpToolExecutor(resolved={tool.name: tool})

    out = await executor(ToolCall(id="t1", name="grep"))

    assert "SUPERSECRET" not in out
    assert "RuntimeError" in out


@pytest.mark.asyncio
async def test_an_unknown_tool_is_named_rather_than_silently_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un agent qui hallucine un nom d'outil peut se corriger si on le lui
    dit, et ne peut rien faire si le résultat est une chaîne vide."""
    tool = _tool()
    monkeypatch.setattr(_MODULE, AsyncMock())
    executor = McpToolExecutor(resolved={tool.name: tool})

    out = await executor(ToolCall(id="t1", name="rm-rf"))

    assert "rm-rf" in out
    # Revue P11 — la tentative EST enregistrée, sans ids puisqu'il n'existe ni
    # ligne `tools` ni serveur à nommer. Elle ne s'enregistrait pas, alors que
    # `run_tool_loop` compte le tour : un modèle qui hallucine huit fois le
    # même nom produisait `tool_calls: 8, tool_names: [], tool_failures: 0` et
    # un node « réussi » dont personne ne pouvait expliquer le coût.
    assert len(executor.invocations) == 1
    recorded = executor.invocations[0]
    assert recorded.tool_name == "rm-rf"
    assert recorded.status == "error"
    assert recorded.tool_id is None
    assert recorded.server_id is None


@pytest.mark.asyncio
async def test_an_oversized_result_is_truncated_before_being_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un outil est une source de texte non bornée (un `grep` sur un
    monorepo, un log de CI). Le mécanisme censé faire LIRE un agent ne doit
    pas devenir le moyen le plus direct de faire exploser un prompt.

    Et la troncature vient AVANT l'enveloppe : l'inverse laisserait
    l'échappement repousser le résultat au-delà du plafond qu'il sert —
    l'erreur d'ordre exacte du finding P-01 de la revue 4.15."""
    tool = _tool()
    monkeypatch.setattr(_MODULE, AsyncMock(return_value=_result("A" * (MAX_TOOL_RESULT_CHARS * 2))))
    executor = McpToolExecutor(resolved={tool.name: tool})

    out = await executor(ToolCall(id="t1", name="grep"))

    assert "tronqué" in out
    assert out.count("A") <= MAX_TOOL_RESULT_CHARS


def test_to_tool_definitions_projects_only_the_public_contract() -> None:
    """Ce qui part vers le LLM ne porte ni `tool_id`, ni `server_id`, ni la
    configuration de connexion — qui est chiffrée au repos et n'a aucune
    raison d'entrer dans un prompt."""
    tool = _tool()

    definitions = to_tool_definitions({tool.name: tool})

    assert len(definitions) == 1
    dumped = definitions[0].model_dump()
    assert set(dumped) == {"name", "description", "input_schema"}
    assert str(tool.server_id) not in str(dumped)


# ─── Revue de code — lot 2 ────────────────────────────────────────────────


def test_an_empty_input_schema_is_normalized_before_being_offered() -> None:
    """Revue P6 — `tools.input_schema` porte `server_default='{}'::jsonb`,
    donc `{}` est la valeur PERSISTÉE PAR DÉFAUT de tout outil dont le serveur
    MCP n'annonce pas de schéma. Anthropic refuse un schéma vide
    (`input_schema.type: Field required`, 400 — fatal, sans repli), donc un
    outil parfaitement légitime sans argument faisait échouer le run entier.
    """
    from agentive_backend.infra.mcp.tool_executor import to_tool_definitions

    tool = _tool()
    object.__setattr__(tool, "input_schema", {})

    [definition] = to_tool_definitions({tool.name: tool})

    assert definition.input_schema == {"type": "object", "properties": {}}


def test_a_declared_schema_is_passed_through_untouched() -> None:
    """Le pendant : la normalisation ne doit toucher QUE le cas vide."""
    from agentive_backend.infra.mcp.tool_executor import to_tool_definitions

    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    tool = _tool()
    object.__setattr__(tool, "input_schema", schema)

    [definition] = to_tool_definitions({tool.name: tool})

    assert definition.input_schema == schema


@pytest.mark.asyncio
async def test_the_cap_bounds_what_the_model_reads_not_what_the_tool_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revue P9 — la troncature s'appliquait au texte BRUT, l'échappement
    venait après et dilate `&` d'un facteur 5.

    8 000 caractères de `&` — du HTML, du XML ou un diff, c'est-à-dire un
    `grep` sur un monorepo, le cas d'usage que ce module revendique —
    atteignaient donc le prompt en 40 000. Le test existant ne sondait que
    `"A"`, un caractère que l'échappement laisse tranquille, donc il passait.
    """
    hostile = "&" * 20_000

    async def _flood(**_kwargs: object) -> dict[str, object]:
        return {"content": [{"type": "text", "text": hostile}]}

    monkeypatch.setattr(_MODULE, _flood)
    tool = _tool()
    executor = McpToolExecutor(resolved={tool.name: tool})

    out = await executor(ToolCall(id="t1", name=tool.name))

    envelope = len("<tool_output>") + len("</tool_output>")
    marker = len("\n[… tronqué]")
    assert len(out) <= MAX_TOOL_RESULT_CHARS + envelope + marker


def test_a_verbose_server_description_is_truncated_not_fatal() -> None:
    """Revue P5 — `ToolDefinition.description` est borné à 2048 et
    `Tool.description` est une colonne `Text` non bornée : aucune validation
    de longueur n'existe à la découverte.

    Une description de 3 ko — courant, beaucoup de serveurs MCP y inlinent
    des exemples d'usage — levait donc une `ValidationError` sur le chemin
    chaud d'un run, pour un outil parfaitement fonctionnel. Tronquée : la
    description est indicative, sa perte dégrade la qualité de l'appel, elle
    ne le rend pas impossible.
    """
    from agentive_backend.infra.mcp.tool_executor import (
        _MAX_TOOL_DESCRIPTION_CHARS,
        to_tool_definitions,
    )

    tool = _tool()
    object.__setattr__(tool, "description", "x" * 5_000)

    [definition] = to_tool_definitions({tool.name: tool})

    assert len(definition.description) == _MAX_TOOL_DESCRIPTION_CHARS
    assert definition.description.endswith("…")


def test_a_tool_whose_name_exceeds_the_contract_is_dropped_not_truncated() -> None:
    """Le pendant asymétrique : un NOM ne se tronque pas.

    C'est l'identifiant que le modèle renverra ; le raccourcir ferait échouer
    la résolution sur un nom qui n'existe pas, et offrir un outil inappelable
    est pire que ne pas l'offrir du tout.
    """
    from agentive_backend.infra.mcp.tool_executor import to_tool_definitions

    ok, oversized = _tool(), _tool()
    object.__setattr__(oversized, "name", "n" * 200)

    definitions = to_tool_definitions({ok.name: ok, oversized.name: oversized})

    assert [d.name for d in definitions] == [ok.name]
