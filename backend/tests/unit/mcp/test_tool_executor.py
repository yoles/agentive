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
    assert executor.invocations == [], "un outil jamais appelé ne s'enregistre pas"


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
