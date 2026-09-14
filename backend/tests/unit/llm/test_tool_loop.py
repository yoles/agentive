"""Story 5.0 AC2/AC4 — la boucle d'outils, testée SEULE.

Aucun MCP, aucune base, aucun provider : c'est précisément ce que
l'exécuteur injecté achète, et la raison pour laquelle la boucle vit dans
`shared/` plutôt que dans une feature.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal

import pytest

from agentive_backend.shared.llm.tool_loop import (
    ToolLoopLimitError,
    ToolLoopProtocolError,
    run_tool_loop,
)
from agentive_backend.shared.llm.types import ChatMessage, Completion, ToolCall, ToolDefinition

_TOOL = ToolDefinition(name="grep", description="cherche", input_schema={"type": "object"})


def _completion(
    *, text: str = "ok", tool_calls: tuple[ToolCall, ...] = (), cost: str | None = "0.001"
) -> Completion:
    return Completion(
        text=text,
        model="m",
        provider="p",
        input_tokens=10,
        output_tokens=5,
        finish_reason="tool_use" if tool_calls else "stop",
        latency_ms=1.0,
        cost_estimate_usd=Decimal(cost) if cost is not None else None,
        tool_calls=tool_calls,
    )


def _scripted(*responses: Completion):
    """Un `complete` qui rend les réponses dans l'ordre, et enregistre ce
    qu'on lui a passé — sans quoi « les outils sont proposés » resterait
    invérifiable."""
    seen: list[tuple[int, Sequence[ToolDefinition] | None]] = []
    it = iter(responses)

    async def _complete(messages, tools):  # type: ignore[no-untyped-def]
        seen.append((len(messages), tools))
        return next(it)

    return _complete, seen


async def _echo(call: ToolCall) -> str:
    return f"résultat de {call.name}"


@pytest.mark.asyncio
async def test_a_model_that_asks_for_nothing_costs_exactly_one_call() -> None:
    """Le cas majoritaire : sans outil demandé, la boucle dégénère en UN
    appel. Un agent sans outils assignés ne doit rien payer pour cette
    machinerie."""
    complete, seen = _scripted(_completion(text="réponse"))

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="salut")],
        tools=None,
        execute=_echo,
    )

    assert result.iterations == 1
    assert result.tool_calls_made == 0
    assert result.completion.text == "réponse"
    assert seen == [(1, None)]


@pytest.mark.asyncio
async def test_the_tools_are_actually_offered_to_the_model() -> None:
    """Le câblage lui-même. Sans cette assertion, supprimer le passage de
    `tools` laisserait la suite verte — le défaut exact que la revue 4.14 a
    relevé sur le `lock_timeout`."""
    complete, seen = _scripted(_completion())

    await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=[_TOOL],
        execute=_echo,
    )

    assert seen[0][1] == [_TOOL]


@pytest.mark.asyncio
async def test_a_requested_tool_is_executed_and_its_result_handed_back() -> None:
    """Le tour complet : le modèle demande, on exécute, on rend, il conclut."""
    call = ToolCall(id="t1", name="grep", arguments={"q": "TODO"})
    complete, _seen = _scripted(
        _completion(text="je cherche", tool_calls=(call,)),
        _completion(text="voici"),
    )

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="cherche TODO")],
        tools=[_TOOL],
        execute=_echo,
    )

    assert result.iterations == 2
    assert result.tool_calls_made == 1
    assert result.completion.text == "voici"
    # Le tour assistant QUI A DEMANDÉ doit rester dans le transcript, sinon
    # le provider voit un résultat qui ne répond à rien.
    # Revue P16 — le tour qui REPOND ferme desormais le transcript : sans lui,
    # `messages` (documente « ready to be persisted ») s'arretait sur le
    # resultat d'outil et ne contenait jamais la reponse du modele.
    roles = [m.role for m in result.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert result.messages[-1].content == "voici"
    tool_msg = result.messages[2]
    assert tool_msg.tool_call_id == "t1"
    assert tool_msg.content == "résultat de grep"
    # Revue P1 — la forme seule ne suffisait pas : ce test épinglait
    # `roles == [...]` et passait alors que le tour assistant était réinjecté
    # SANS ses `tool_calls`, donc que le `tool_call_id` ci-dessus ne
    # référençait rien. Les deux providers refusent cet échange par un 400.
    # C'est la CORRÉLATION qu'il faut asserter, pas l'ordre des rôles.
    assistant_msg = result.messages[1]
    assert assistant_msg.tool_calls == (call,)
    requested = {c.id for m in result.messages if m.role == "assistant" for c in m.tool_calls}
    answered = {m.tool_call_id for m in result.messages if m.role == "tool"}
    assert answered <= requested, f"résultats sans demande : {answered - requested}"


@pytest.mark.asyncio
async def test_tokens_and_cost_are_summed_over_every_iteration() -> None:
    """AC4 — chaque itération est un appel FACTURÉ. Deux revues ont déjà dû
    imposer cette règle (IG1 de la 4.3, P-2 de la 4.7) ; ne compter que le
    dernier tour rendrait la dépense invisible une troisième fois."""
    call = ToolCall(id="t1", name="grep")
    complete, _ = _scripted(
        _completion(tool_calls=(call,), cost="0.002"),
        _completion(cost="0.003"),
    )

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=[_TOOL],
        execute=_echo,
    )

    assert result.total_input_tokens == 20
    assert result.total_output_tokens == 10
    assert result.total_cost_usd == Decimal("0.005")


@pytest.mark.asyncio
async def test_an_entirely_unpriced_loop_reports_none_not_zero() -> None:
    """`None` veut dire « inconnu », `0` veut dire « gratuit ». Fabriquer un
    zéro rendrait les deux indiscernables — même convention que `LLMUsage`."""
    complete, _ = _scripted(_completion(cost=None))

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=None,
        execute=_echo,
    )

    assert result.total_cost_usd is None


@pytest.mark.asyncio
async def test_the_iteration_ceiling_refuses_loudly() -> None:
    """AC4 — refus TYPÉ, jamais une dégradation silencieuse : rendre la
    réponse partielle cacherait un modèle bloqué en boucle derrière un
    résultat d'apparence plausible."""
    call = ToolCall(id="t", name="grep")
    complete, _ = _scripted(*[_completion(tool_calls=(call,)) for _ in range(5)])

    with pytest.raises(ToolLoopLimitError, match="iteration ceiling"):
        await run_tool_loop(
            complete=complete,
            messages=[ChatMessage(role="user", content="x")],
            tools=[_TOOL],
            execute=_echo,
            max_iterations=2,
        )


@pytest.mark.asyncio
async def test_a_model_answering_on_its_last_allowed_turn_is_not_refused() -> None:
    """La borne d'itérations ne mord que si le modèle demande ENCORE. Un
    modèle qui conclut sur son dernier tour autorisé n'a rien dépassé, et
    l'échouer serait faux."""
    call = ToolCall(id="t", name="grep")
    complete, _ = _scripted(_completion(tool_calls=(call,)), _completion(text="fini"))

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=[_TOOL],
        execute=_echo,
        max_iterations=2,
    )

    assert result.completion.text == "fini"


@pytest.mark.asyncio
async def test_the_tool_call_ceiling_is_separate_from_the_iteration_ceiling() -> None:
    """Un seul tour peut demander plusieurs outils : borner les tours ne
    borne pas les appels. D'où deux plafonds et non un."""
    calls = tuple(ToolCall(id=f"t{i}", name="grep") for i in range(5))
    complete, _ = _scripted(_completion(tool_calls=calls), _completion())

    with pytest.raises(ToolLoopLimitError, match="tool call ceiling"):
        await run_tool_loop(
            complete=complete,
            messages=[ChatMessage(role="user", content="x")],
            tools=[_TOOL],
            execute=_echo,
            max_iterations=10,
            max_tool_calls=3,
        )


@pytest.mark.asyncio
async def test_an_executor_failure_reaches_the_model_instead_of_killing_the_run() -> None:
    """Un outil qui échoue est une information que le modèle peut exploiter.
    Tuer le run jetterait tout ce qui a déjà été payé."""
    call = ToolCall(id="t1", name="grep")
    complete, _ = _scripted(_completion(tool_calls=(call,)), _completion(text="tant pis"))

    async def _failing(_call: ToolCall) -> str:
        return "ERREUR: timeout de l'outil"

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=[_TOOL],
        execute=_failing,
    )

    assert result.completion.text == "tant pis"
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert "ERREUR" in tool_msgs[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize(("iters", "calls"), [(0, 5), (-1, 5), (1, -1)])
async def test_a_ceiling_that_cannot_mean_anything_is_refused(iters: int, calls: int) -> None:
    """`max_iterations=0` ferait rendre la fonction sans jamais appeler le
    modèle — aucun appelant ne peut vouloir ça."""
    complete, _ = _scripted(_completion())

    with pytest.raises(ValueError, match="must be >="):
        await run_tool_loop(
            complete=complete,
            messages=[ChatMessage(role="user", content="x")],
            tools=None,
            execute=_echo,
            max_iterations=iters,
            max_tool_calls=calls,
        )


# ─── AC4, troisième plafond : l'horloge murale ───


@pytest.mark.asyncio
async def test_the_wall_clock_ceiling_refuses_a_loop_that_takes_too_long() -> None:
    """Le seul des trois plafonds qui borne une DURÉE, et le seul que la
    dérivation du seuil de run bloqué peut lire.

    Les deux autres bornent un compte : huit tours de huit secondes et huit
    tours de huit minutes valent pareil pour eux. `recovery` a besoin d'un
    nombre de secondes, et l'obtenir en multipliant deux plafonds donnerait
    ~4,8 h — un seuil que ce module qualifie lui-même d'inutilisable.
    """

    async def _slow(messages, tools):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.5)
        return _completion(tool_calls=(ToolCall(id="1", name="grep", arguments={}),))

    with pytest.raises(ToolLoopLimitError, match="wall-clock ceiling"):
        await run_tool_loop(
            complete=_slow,
            messages=[ChatMessage(role="user", content="q")],
            tools=[_TOOL],
            execute=_echo,
            max_wall_clock_s=0.05,
        )


@pytest.mark.asyncio
async def test_a_timeout_from_inside_the_loop_is_not_blamed_on_the_ceiling() -> None:
    """Un `TimeoutError` venu d'ailleurs — un provider, un exécutuer — doit
    ressortir tel quel.

    L'avaler dans un `ToolLoopLimitError` ferait accuser le plafond d'une
    panne qui n'a rien à voir avec lui, et enverrait l'opérateur baisser un
    réglage qui n'était pas en cause.
    """

    async def _boom(messages, tools):  # type: ignore[no-untyped-def]
        raise TimeoutError("le provider a expiré")

    with pytest.raises(TimeoutError) as exc_info:
        await run_tool_loop(
            complete=_boom,
            messages=[ChatMessage(role="user", content="q")],
            tools=[_TOOL],
            execute=_echo,
            max_wall_clock_s=30.0,
        )
    assert not isinstance(exc_info.value, ToolLoopLimitError)
    assert "provider" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a_loop_that_finishes_in_time_is_untouched_by_the_ceiling() -> None:
    """La borne ne doit rien coûter au cas nominal — sans quoi le test
    ci-dessus prouverait seulement qu'on sait lever une exception."""
    complete, _ = _scripted(_completion(text='{"ok": true}'))
    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="q")],
        tools=[_TOOL],
        execute=_echo,
        max_wall_clock_s=30.0,
    )
    assert result.completion.text == '{"ok": true}'
    assert result.iterations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
async def test_a_wall_clock_that_cannot_mean_anything_is_refused(bad: float) -> None:
    """Même posture que les deux autres plafonds : 0 ou négatif refuserait
    toute boucle avant le premier appel, et `inf`/`nan` désarmerait la borne
    en silence — c'est-à-dire rendrait fausse la dérivation de `recovery`
    sans que rien ne le dise."""
    complete, _ = _scripted(_completion())
    with pytest.raises(ValueError, match="max_wall_clock_s"):
        await run_tool_loop(
            complete=complete,
            messages=[ChatMessage(role="user", content="q")],
            tools=[_TOOL],
            execute=_echo,
            max_wall_clock_s=bad,
        )


# ─── Revue de code — lot 2 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_refused_loop_still_reports_what_it_spent() -> None:
    """Revue P4 — un plafond n'est pas une dépense nulle.

    `ToolLoopLimitError` ne portait qu'un message : l'appelant auditait
    `input_tokens=0` pour un run qui avait brûlé plusieurs appels facturés.
    C'est la famille de l'IG1 de la 4.3 et du P-2 de la 4.7 — une dépense qui
    n'apparaît nulle part — et un zéro fabriqué là où ce dépôt impose `None`
    pour « inconnu ».
    """
    call = ToolCall(id="t", name="grep")
    complete, _ = _scripted(*[_completion(tool_calls=(call,), cost="0.002") for _ in range(5)])

    with pytest.raises(ToolLoopLimitError) as excinfo:
        await run_tool_loop(
            complete=complete,
            messages=[ChatMessage(role="user", content="x")],
            tools=[_TOOL],
            execute=_echo,
            max_tool_calls=2,
        )

    usage = excinfo.value.usage
    assert usage.iterations >= 1
    assert usage.total_input_tokens == 10 * usage.iterations
    assert usage.total_output_tokens == 5 * usage.iterations
    assert usage.total_cost_usd == Decimal("0.002") * usage.iterations
    # Rendu aussi dans le `context` RFC 7807, pour l'opérateur.
    assert excinfo.value.context["input_tokens"] == usage.total_input_tokens


@pytest.mark.asyncio
async def test_the_last_allowed_turn_is_not_offered_tools() -> None:
    """Revue P15 — `.env.example` documentait « il ne peut plus demander
    d'outil » ; le code lui en proposait puis le sanctionnait de l'avoir fait,
    jetant 8 appels facturés là où ne pas tendre la perche rend une réponse."""
    call = ToolCall(id="t", name="grep")
    complete, seen = _scripted(_completion(tool_calls=(call,)), _completion(text="conclusion"))

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=[_TOOL],
        execute=_echo,
        max_iterations=2,
    )

    assert seen[0][1] == [_TOOL], "le premier tour doit voir ses outils"
    assert seen[1][1] is None, "le dernier tour autorisé ne doit plus en recevoir"
    assert result.completion.text == "conclusion"


@pytest.mark.asyncio
async def test_a_tool_use_stop_naming_no_call_is_an_anomaly_not_an_answer() -> None:
    """Revue P12 — `finish_reason == "tool_use"` avec un tuple VIDE ne peut pas
    arriver sur un échange sain : soit l'adaptateur a tout abandonné comme
    malformé, soit la charge utile du provider a changé de forme.

    La boucle prenait la branche « le modèle a répondu » et rendait une
    complétion dont le texte est typiquement vide ; `execute_agent_node`
    l'analysait en sortie vide et enregistrait le node comme RÉUSSI.
    """
    anomalous = Completion(
        text="",
        model="m",
        provider="p",
        input_tokens=10,
        output_tokens=0,
        finish_reason="tool_use",
        latency_ms=1.0,
        tool_calls=(),
    )
    complete, _ = _scripted(anomalous)

    with pytest.raises(ToolLoopProtocolError, match="no usable tool call"):
        await run_tool_loop(
            complete=complete,
            messages=[ChatMessage(role="user", content="x")],
            tools=[_TOOL],
            execute=_echo,
        )


@pytest.mark.asyncio
async def test_a_normal_stop_with_no_tool_call_stays_an_answer() -> None:
    """Le pendant du test ci-dessus : la garde P12 ne doit mordre QUE sur
    l'anomalie, jamais sur le cas majoritaire."""
    complete, _ = _scripted(_completion(text="réponse normale"))

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=[_TOOL],
        execute=_echo,
    )

    assert result.completion.text == "réponse normale"


@pytest.mark.asyncio
async def test_the_transcript_handed_to_the_provider_does_not_change_under_it() -> None:
    """La boucle mute son transcript APRÈS chaque appel. Passer la liste vive
    donnait à l'appelé une vue qui changeait sous lui — un adaptateur qui la
    stocke, un log différé ou un mock qui l'enregistre voyaient un état
    postérieur à leur propre appel."""
    call = ToolCall(id="t1", name="grep")
    captured: list[Sequence[ChatMessage]] = []

    async def _capturing(messages, tools):  # type: ignore[no-untyped-def]
        captured.append(messages)
        return _completion(tool_calls=(call,)) if len(captured) == 1 else _completion(text="fin")

    await run_tool_loop(
        complete=_capturing,
        messages=[ChatMessage(role="user", content="x")],
        tools=[_TOOL],
        execute=_echo,
    )

    assert len(captured[0]) == 1, "le 1er appel a vu un transcript d'un message, et le garde"
    assert len(captured[1]) == 3


@pytest.mark.asyncio
async def test_a_single_iteration_loop_with_tools_is_refused() -> None:
    """Conséquence de P15, refusée plutôt que subie.

    Le dernier tour autorisé ne reçoit plus d'outils ; quand il n'y en a
    qu'un, ce tour est AUSSI le premier, et un nœud portant des outils
    tournerait sans aucun. `ge=1` sur le réglage l'acceptait. Silencieux, donc
    exactement ce que l'AC4 interdit — et pire que l'échec bruyant d'avant.
    """
    complete, _ = _scripted(_completion())

    with pytest.raises(ValueError, match="must be >= 2 when tools are offered"):
        await run_tool_loop(
            complete=complete,
            messages=[ChatMessage(role="user", content="x")],
            tools=[_TOOL],
            execute=_echo,
            max_iterations=1,
        )


@pytest.mark.asyncio
async def test_a_single_iteration_loop_without_tools_stays_legal() -> None:
    """Le pendant : sans outil offert, une seule itération est le cas normal
    — c'est la boucle qui dégénère en un appel unique."""
    complete, _ = _scripted(_completion(text="une seule passe"))

    result = await run_tool_loop(
        complete=complete,
        messages=[ChatMessage(role="user", content="x")],
        tools=None,
        execute=_echo,
        max_iterations=1,
    )

    assert result.iterations == 1
    assert result.completion.text == "une seule passe"
