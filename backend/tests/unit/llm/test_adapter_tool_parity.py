"""Parité des deux adaptateurs sur la boucle d'outils (Story 5.0 T2.4).

Cette tâche était cochée dans la story et le fichier n'existait pas. C'est le
test qui aurait attrapé la revue P1 : toute la suite de `test_tool_loop.py`
substitue un `complete` scripté qui IGNORE les messages qu'on lui passe, donc
aucun test n'a jamais fait traverser un tour d'outil à un adaptateur. Le
contrat provider — la seule partie de cette story qu'on ne pouvait pas
vérifier par lecture — était exactement la partie non testée.

Deux propriétés, sur les DEUX adaptateurs, depuis la MÊME `ToolDefinition` :

1. **Corrélation** — un `ToolMessage` réinjecté doit toujours répondre à un
   `tool_use` présent sur un `AIMessage` qui le PRÉCÈDE. C'est l'invariant que
   les deux providers vérifient côté serveur, et dont la violation est un 400
   fatal (sans repli possible dans `LLMRouter`).
2. **Normalisation** — la réponse d'un provider redevient un `ToolCall`
   exploitable, identique des deux côtés.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from agentive_backend.infra.llm import anthropic_adapter, openai_adapter
from agentive_backend.shared.llm.types import ChatMessage, ToolCall, ToolDefinition

#: Les deux adaptateurs, pilotés par les mêmes cas. Paramétrer plutôt que
#: dupliquer : une divergence entre providers doit faire échouer UN cas nommé,
#: pas se cacher dans un fichier qu'on a oublié de mettre à jour.
ADAPTERS = [
    pytest.param(anthropic_adapter, id="anthropic"),
    pytest.param(openai_adapter, id="openai"),
]

#: La MÊME définition pour les deux — c'est la moitié « parité » de T2.4.
SEARCH_TOOL = ToolDefinition(
    name="search_files",
    description="Search the repository for a pattern.",
    input_schema={
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"],
    },
)


def _tool_exchange() -> list[ChatMessage]:
    """Un tour d'outil complet, tel que `run_tool_loop` le construit."""
    return [
        ChatMessage(role="user", content="where is the retry policy?"),
        ChatMessage(
            role="assistant",
            # Vide À DESSEIN : sur un tour purement `tool_use`, ce sont les
            # blocs d'outils qui portent le tour. C'est la forme que les
            # fixtures du dépôt produisent déjà (`_completion("", ...)`).
            content="",
            tool_calls=(
                ToolCall(id="toolu_01", name="search_files", arguments={"pattern": "retry"}),
            ),
        ),
        ChatMessage(
            role="tool",
            content="<tool_output>backend/.../retry.py</tool_output>",
            tool_call_id="toolu_01",
        ),
    ]


def _assert_every_result_answers_a_request(converted: list[BaseMessage]) -> None:
    """L'invariant que les deux providers imposent côté serveur.

    Anthropic : `tool_result` sans `tool_use` correspondant → 400.
    OpenAI : `role="tool"` non précédé d'un assistant portant `tool_calls` → 400.
    """
    offered: set[str] = set()
    for message in converted:
        if isinstance(message, AIMessage):
            offered.update(call["id"] for call in message.tool_calls if call.get("id"))
        elif isinstance(message, ToolMessage):
            assert message.tool_call_id in offered, (
                f"ToolMessage(tool_call_id={message.tool_call_id!r}) ne répond à aucun "
                f"tool_use présent avant lui — connus à ce point : {sorted(offered)}. "
                "Les deux providers refusent cet échange par un 400 fatal."
            )


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_reinjected_tool_result_still_answers_a_request(adapter: Any) -> None:
    """LE test de non-régression de la revue P1.

    Avant le correctif, `_to_lc_messages` reconstruisait le tour assistant
    depuis `content` seul : les `tool_use` étaient perdus, et le `ToolMessage`
    suivant référençait un id absent du transcript. Retirer `tool_calls=` de
    la branche `assistant` d'un adaptateur fait tomber ce test.
    """
    converted, _ = adapter._to_lc_messages(_tool_exchange())

    _assert_every_result_answers_a_request(converted)

    ai_turns = [m for m in converted if isinstance(m, AIMessage)]
    assert len(ai_turns) == 1
    assert [(c["name"], c["args"]) for c in ai_turns[0].tool_calls] == [
        ("search_files", {"pattern": "retry"})
    ]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_an_assistant_turn_that_asked_for_nothing_carries_no_tool_calls(adapter: Any) -> None:
    """Rétro-compatibilité : tout appelant antérieur à la boucle est inchangé."""
    converted, _ = adapter._to_lc_messages(
        [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
        ]
    )
    ai_turns = [m for m in converted if isinstance(m, AIMessage)]
    assert len(ai_turns) == 1
    assert ai_turns[0].content == "hello"
    assert ai_turns[0].tool_calls == []


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_the_same_provider_response_normalizes_identically(adapter: Any) -> None:
    """La moitié « parité » de T2.4 : LangChain rend `{id, name, args}` des
    deux côtés, donc `_to_tool_calls` doit rendre le MÊME `ToolCall`."""
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "name": SEARCH_TOOL.name,
                "args": {"pattern": "retry"},
                "type": "tool_call",
            }
        ],
    )
    assert adapter._to_tool_calls(response) == (
        ToolCall(id="call_1", name="search_files", arguments={"pattern": "retry"}),
    )


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_malformed_entry_is_dropped_and_the_rest_kept(adapter: Any) -> None:
    """Posture défensive documentée : par ENTRÉE, pas par réponse."""
    response = AIMessage(
        content="",
        tool_calls=[
            {"id": "", "name": "search_files", "args": {}, "type": "tool_call"},
            {
                "id": "call_2",
                "name": "search_files",
                "args": {"pattern": "ok"},
                "type": "tool_call",
            },
        ],
    )
    calls = adapter._to_tool_calls(response)
    assert [c.id for c in calls] == ["call_2"]


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("bad_args", [None, "pattern=retry", ["retry"], 42])
def test_non_dict_arguments_are_dropped_not_coerced(adapter: Any, bad_args: Any) -> None:
    """Revue P13 — `args` non-dict était coercé en `{}` et l'outil s'exécutait
    QUAND MÊME, sans argument : il échouait alors sur la validation de schéma
    du serveur MCP, et le modèle recevait une erreur qui ne ressemblait en
    rien à sa vraie cause.

    Abandonné comme n'importe quelle autre entrée malformée — et si toutes le
    sont, `run_tool_loop` lève `ToolLoopProtocolError` plutôt que de rendre
    une réponse vide (P12).
    """
    # `AIMessage` valide lui-même `args` comme un dict, donc cette forme n'est
    # PAS constructible par ce chemin aujourd'hui : la branche défend contre
    # un changement de forme de la charge utile LangChain/provider, pas contre
    # un cas courant. `_to_tool_calls` lit par `getattr`, donc c'est cette
    # surface-là qu'on éprouve — celle que la fonction accepte réellement.
    response = SimpleNamespace(
        tool_calls=[
            {"id": "c1", "name": "search_files", "args": bad_args},
            {"id": "c2", "name": "search_files", "args": {"pattern": "ok"}},
        ]
    )
    assert [c.id for c in adapter._to_tool_calls(response)] == ["c2"]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_a_round_trip_through_both_halves_keeps_the_correlation(adapter: Any) -> None:
    """Bout en bout : ce que l'adaptateur LIT d'une réponse doit pouvoir être
    réécrit par lui dans le transcript sans casser la corrélation.

    C'est la composition des deux moitiés — `_to_tool_calls` puis
    `_to_lc_messages` — donc la boucle réelle en miniature, sans provider.
    """
    response = AIMessage(
        content="",
        tool_calls=[
            {"id": "rt_1", "name": "search_files", "args": {"pattern": "x"}, "type": "tool_call"}
        ],
    )
    calls = adapter._to_tool_calls(response)

    transcript = [
        ChatMessage(role="user", content="go"),
        ChatMessage(role="assistant", content="", tool_calls=calls),
        *[ChatMessage(role="tool", content="result", tool_call_id=call.id) for call in calls],
    ]
    converted, _ = adapter._to_lc_messages(transcript)
    _assert_every_result_answers_a_request(converted)
