"""The agentic tool loop (Story 5.0 AC2/AC4) — provider- and transport-agnostic.

Closes **D80**: before this module the LLM was never told it had tools, so
``finish_reason == "tool_use"`` — already mapped by both adapters since
Story 1.6 — could not physically occur. Everything else existed already
(tools declared, discovered, assigned, sandboxed and executable); what was
missing was the circuit: *offer tools → the model asks → execute → hand the
result back → repeat*.

Why the executor is INJECTED
----------------------------
``shared`` may not import ``infra`` (``.import-linter`` Contract 2), and the
real executor has to reach MCP, which lives in ``infra``. Passing it in is
therefore the only legal shape — not a stylistic preference. It also buys the
property this project keeps asking for: the loop is testable on its own, with
no MCP server, no database and no provider.

What this module deliberately does NOT do
-----------------------------------------
* **It does not wrap tool results.** ``wrap_external_input`` is applied by the
  executor, one layer down, so a caller cannot forget it. This module never
  sees an unwrapped result and has no way to bypass that.
* **It does not decide what happens after a limit is hit.** It raises a typed
  error and lets the caller choose; silently returning a partial answer would
  be the "silent degradation" the AC forbids.
* **It does not retry.** Retries, provider rotation and error policy are the
  router's and the node's job, and duplicating them here would create a second
  set of rules that drifts from the first.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from agentive_backend.shared.config import settings
from agentive_backend.shared.llm.exceptions import LLMError
from agentive_backend.shared.llm.types import ChatMessage, Completion, ToolCall, ToolDefinition

#: Signature of the injected executor: run ONE tool call, return what the
#: model should read. It must not raise for an ordinary tool failure — a
#: timeout or an error result is information the model can act on, and
#: killing the run instead would throw away everything already paid for.
ToolExecutor = Callable[[ToolCall], Awaitable[str]]

#: Plafonds par défaut, lus depuis la configuration de déploiement (AC4).
#: Résolus à l'import, comme toute constante de module : baisser un plafond
#: est une décision d'exploitation qui prend effet au redémarrage, pas une
#: bascule à chaud. Délibérément conservateurs : une boucle est non bornée
#: par nature, et CHAQUE itération est un appel LLM facturé.
DEFAULT_MAX_ITERATIONS: Final = settings.tool_loop_max_iterations
DEFAULT_MAX_TOOL_CALLS: Final = settings.tool_loop_max_tool_calls
DEFAULT_MAX_WALL_CLOCK_S: Final = settings.tool_loop_max_wall_clock_s


@dataclass(frozen=True)
class ToolLoopUsage:
    """What a loop had already SPENT when it was refused.

    Exists because a ceiling used to throw all of it away (review P4): the
    error carried a message and nothing else, so the Playground audited a run
    that had burned up to 8 billed LLM calls and 24 tool executions as
    ``input_tokens=0, output_tokens=0`` — a fabricated zero, in a codebase
    that elsewhere insists on ``None`` for "unknown" precisely so the two
    cannot be confused.

    This is the same family as IG1 of the 4.3 review and P-2 of the 4.7:
    spend that appeared nowhere. AC4 mandates the typed refusal AND that
    every iteration's cost reaches the run totals; it never said what happens
    to that cost on the refusal path, and the answer is not "it vanishes".
    """

    iterations: int
    tool_calls_made: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal | None


class ToolLoopLimitError(LLMError):
    """A ceiling was reached before the model stopped asking for tools.

    Subclasses :class:`LLMError` so it lands in the same family as every
    other LLM fault for logging and RFC 7807 rendering.

    It is deliberately NOT retried, and the caller does not consult
    ``error_policy`` for it — a ceiling is a deterministic verdict about a
    model that will not stop, so retrying only re-buys the same refusal, and
    a replayed loop re-executes tools that are not necessarily idempotent
    (cf ``docs/runbooks/rejeu-et-outils.md``). An earlier version of this
    docstring claimed the dispatcher routed it "like any other LLM fault";
    it never did — ``_complete_with_retry`` catches only
    ``LLMAllProvidersFailedError`` — and describing a mechanism that does not
    exist is worse than describing none (review P21).

    ``usage`` carries what the refused loop already spent, so the caller can
    record it instead of reporting zero.
    """

    def __init__(self, detail: str, *, usage: ToolLoopUsage) -> None:
        super().__init__(
            detail,
            context={
                "iterations": usage.iterations,
                "tool_calls_made": usage.tool_calls_made,
                "input_tokens": usage.total_input_tokens,
                "output_tokens": usage.total_output_tokens,
                "cost_usd": (
                    str(usage.total_cost_usd) if usage.total_cost_usd is not None else None
                ),
            },
        )
        self.usage = usage


class ToolLoopProtocolError(LLMError):
    """The provider said "I am calling a tool" and named none.

    ``finish_reason == "tool_use"`` with an EMPTY ``tool_calls`` cannot happen
    on a healthy exchange: either every entry was malformed and the adapter
    dropped them all, or a LangChain/provider payload changed shape under us.

    Raised rather than returned (review P12). The loop used to take the "the
    model answered" branch here and hand back a completion whose text is
    typically ``""``: ``execute_agent_node`` then best-effort-parsed an empty
    output and recorded the node as SUCCESSFUL. A silent wrong answer is the
    one outcome this module's ceilings exist to prevent — ``types.py`` already
    asserts this combination "stays detectable as the anomaly it would be",
    and nothing was detecting it.
    """


@dataclass(frozen=True)
class ToolLoopResult:
    """Outcome of a completed loop.

    ``total_*`` aggregate EVERY iteration, not just the last. That is the
    whole point of returning a result object rather than a bare
    :class:`Completion`: each turn is a billed call, and the run's totals must
    include them. Two reviews have already had to impose this rule on this
    codebase — IG1 of the Story 4.3 review (routing escalation) and P-2 of the
    4.7 review (handoff summaries), both of which shipped spend that appeared
    nowhere. A third omission of the same family would be indefensible.
    """

    completion: Completion
    """The final completion — the one that asked for no further tool."""
    messages: tuple[ChatMessage, ...]
    """The full transcript, ready to be persisted: the starting messages, every
    tool request with its result, AND the turn that finally answered.

    That last turn was missing (review P16) — the snapshot was taken before it
    was appended, so a consumer persisting this as the conversation stored a
    history that stopped mid-exchange, with the answer reachable only through
    ``completion``."""
    iterations: int
    """LLM calls made, always >= 1."""
    tool_calls_made: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal | None
    """``None`` only when NO iteration carried a price (an unpriced model),
    mirroring :class:`LLMUsage`'s own convention — never ``0`` for "unknown",
    which would be a fabricated figure indistinguishable from a real free
    call."""


def _validate_ceilings(
    *,
    has_tools: bool,
    max_iterations: int,
    max_tool_calls: int,
    max_wall_clock_s: float,
) -> None:
    """Refuse a ceiling that cannot mean anything, before any call is billed.

    Extracted from :func:`run_tool_loop` to keep it under the repo's mccabe
    gate: these guards are a flat list with no interaction between them, so
    they read better on their own than inlined ahead of the loop.
    """
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
    if has_tools and max_iterations < 2:
        # Conséquence de P15, refusée au lieu d'être subie. Le dernier tour
        # autorisé part sans outils ; quand il n'y en a QU'UN, ce tour est
        # aussi le premier, et un nœud portant des outils tournerait sans
        # aucun — silencieusement, ce que l'AC4 interdit explicitement.
        #
        # Gardé ICI plutôt que par un `ge=2` sur le réglage : cette fonction
        # a d'autres appelants que `settings`, et une borne posée sur le
        # champ ne protège que le chemin qui passe par lui. Deux itérations
        # est le minimum qui a un sens — une pour demander, une pour
        # conclure.
        raise ValueError(
            f"max_iterations must be >= 2 when tools are offered, got {max_iterations}: "
            "the last allowed turn is not offered tools, so a single-iteration loop "
            "would silently run tool-less"
        )
    if max_tool_calls < 0:
        raise ValueError(f"max_tool_calls must be >= 0, got {max_tool_calls}")
    if not math.isfinite(max_wall_clock_s) or max_wall_clock_s <= 0:
        raise ValueError(f"max_wall_clock_s must be finite and > 0, got {max_wall_clock_s!r}")


async def run_tool_loop(
    *,
    complete: Callable[
        [Sequence[ChatMessage], Sequence[ToolDefinition] | None], Awaitable[Completion]
    ],
    messages: Sequence[ChatMessage],
    tools: Sequence[ToolDefinition] | None,
    execute: ToolExecutor,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_wall_clock_s: float = DEFAULT_MAX_WALL_CLOCK_S,
) -> ToolLoopResult:
    """Run the model until it answers without asking for a tool.

    Args:
        complete: bound call into the router — the caller closes over model,
            temperature, timeout and provider chain, so this module stays
            ignorant of them.
        messages: the starting transcript.
        tools: what to offer. ``None`` or empty means the loop degenerates to
            exactly one call, which is the pre-Story-5.0 behaviour — so a
            caller with no assigned tools pays nothing for this machinery.
        execute: injected, see :data:`ToolExecutor`.
        max_iterations: ceiling on LLM calls.
        max_tool_calls: ceiling on tool executions across all iterations.
            Separate from ``max_iterations`` on purpose: one turn can request
            several tools at once, so bounding turns does not bound calls.
        max_wall_clock_s: ceiling on the loop's total elapsed time, and the
            only one of the three that is a FLAT bound rather than a factor.
            The other two bound a PRODUCT (`max_iterations` x the per-call LLM
            timeout x the provider chain, plus `max_tool_calls` x the per-tool
            timeout), and a product of ceilings is not a usable bound for
            anything downstream: ``recovery.derive_stale_threshold_s`` has to
            size the crash-detection window against a node's worst case, and
            that module states outright that a threshold above an hour defeats
            the recovery worker (Story 4.6 T8.5). This argument is what keeps
            that window flat — and it is why the derivation takes THIS number
            and not the other two.

    Raises:
        ToolLoopLimitError: a ceiling was reached while the model was still
            asking — iterations, tool calls, or wall clock. Typed and loud —
            returning the partial answer would hide a model stuck in a loop
            behind a plausible-looking result.

            No ceiling fabricates a partial success. But the spend is NOT
            lost with the result: every ``ToolLoopLimitError`` carries a
            :class:`ToolLoopUsage` (review P4) so the caller's failure path
            can record what was actually billed instead of reporting zero.
        ToolLoopProtocolError: the provider reported ``finish_reason ==
            "tool_use"`` and named no usable call (review P12).
        ValueError: a ceiling is not strictly positive. A ``max_iterations``
            of 0 would make the function return without ever calling the
            model, which no caller can mean.
    """
    _validate_ceilings(
        has_tools=bool(tools),
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        max_wall_clock_s=max_wall_clock_s,
    )

    transcript: list[ChatMessage] = list(messages)
    iterations = 0
    tool_calls_made = 0
    total_input = 0
    total_output = 0
    total_cost: Decimal | None = None

    def _usage() -> ToolLoopUsage:
        """Snapshot de la dépense engagée, pour un refus (revue P4)."""
        return ToolLoopUsage(
            iterations=iterations,
            tool_calls_made=tool_calls_made,
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_cost_usd=total_cost,
        )

    # `asyncio.timeout` rather than a deadline checked between iterations: a
    # check between turns bounds nothing when a single turn is what hangs, and
    # a single turn contains both an LLM call and up to `max_tool_calls` tool
    # executions. This cancels wherever the loop actually is.
    deadline = asyncio.timeout(max_wall_clock_s)
    try:
        async with deadline:
            while True:
                # Revue P15 — le dernier tour AUTORISÉ part sans outils.
                # `.env.example` documentait déjà ce comportement (« il ne
                # peut plus demander d'outil ») alors que le code offrait
                # `tools` à chaque tour puis refusait au modèle ce qu'il
                # venait de l'inviter à faire : 8 appels facturés et jusqu'à
                # 24 exécutions MCP jetés, là où ne pas tendre la perche rend
                # une réponse exploitable. On ne peut pas reprocher à un
                # modèle d'utiliser un outil qu'on lui présente.
                is_final_turn = iterations + 1 >= max_iterations
                # `tuple(...)` et non la liste vive : le transcript est muté
                # après cet appel (le tour demandé, ses résultats, puis la
                # réponse finale), et passer la liste elle-même donnait à
                # l'appelé une vue qui CHANGE sous lui. Un adaptateur qui la
                # stocke, un mock qui l'enregistre ou un log différé voyaient
                # un état postérieur à leur propre appel.
                completion = await complete(tuple(transcript), None if is_final_turn else tools)
                iterations += 1
                total_input += completion.input_tokens
                total_output += completion.output_tokens
                if completion.cost_estimate_usd is not None:
                    total_cost = (total_cost or Decimal(0)) + completion.cost_estimate_usd

                if not completion.tool_calls:
                    if completion.finish_reason == "tool_use":
                        # Revue P12 — anomalie, pas une réponse.
                        raise ToolLoopProtocolError(
                            "provider reported finish_reason='tool_use' with no usable "
                            f"tool call after {iterations} iteration(s) — every entry was "
                            "dropped as malformed, or the payload shape changed"
                        )
                    # The model answered. This is the ONLY exit that returns a result.
                    #
                    # Revue P16 — le tour qui RÉPOND entre lui aussi dans le
                    # transcript. Le snapshot était pris avant, donc
                    # `messages` — documenté « the full transcript, ready to
                    # be persisted » — s'arrêtait systématiquement sur un
                    # `role="tool"` et ne contenait jamais la réponse.
                    transcript.append(ChatMessage(role="assistant", content=completion.text))
                    return ToolLoopResult(
                        completion=completion,
                        messages=tuple(transcript),
                        iterations=iterations,
                        tool_calls_made=tool_calls_made,
                        total_input_tokens=total_input,
                        total_output_tokens=total_output,
                        total_cost_usd=total_cost,
                    )

                if tool_calls_made + len(completion.tool_calls) > max_tool_calls:
                    raise ToolLoopLimitError(
                        f"tool call ceiling reached: {tool_calls_made} made, "
                        f"{len(completion.tool_calls)} more requested, ceiling {max_tool_calls}",
                        usage=_usage(),
                    )
                if iterations >= max_iterations:
                    # Checked AFTER the tool-call ceiling and only when the model is
                    # still asking: a model that answers on its last allowed turn has
                    # not exceeded anything, and failing it would be wrong.
                    #
                    # Depuis P15 ce tour-là n'a plus reçu d'outils, donc ce
                    # refus ne se déclenche plus que si le modèle en réclame
                    # SANS qu'on lui en ait proposé — défensif, pas nominal.
                    raise ToolLoopLimitError(
                        f"iteration ceiling reached: {iterations} LLM calls, ceiling "
                        f"{max_iterations}, and the model is still requesting tools",
                        usage=_usage(),
                    )

                # The assistant turn that REQUESTED the tools must stay in the
                # transcript, WITH its requests: without the text the provider
                # loses the model's reasoning, and without `tool_calls` it sees
                # results answering nothing and rejects the exchange outright
                # (review P1 — the text was kept, the correlation was not, so
                # every second iteration died on a fatal 400).
                #
                # `completion.text` is routinely EMPTY on a turn that only asked
                # for tools. That is not a degenerate case to guard against: the
                # tool-use blocks are what carries the turn, and the adapters
                # render them from `tool_calls`.
                transcript.append(
                    ChatMessage(
                        role="assistant",
                        content=completion.text,
                        tool_calls=completion.tool_calls,
                    )
                )
                for call in completion.tool_calls:
                    result = await execute(call)
                    tool_calls_made += 1
                    transcript.append(
                        ChatMessage(role="tool", content=result, tool_call_id=call.id)
                    )
    except TimeoutError:
        # `expired()` distinguishes OUR deadline from a `TimeoutError` raised
        # inside the loop by something else (a provider timeout surfacing as
        # one, a tool executor that lets one escape). Re-raising the latter
        # unchanged matters: swallowing it into `ToolLoopLimitError` would
        # blame the ceiling for a failure that had nothing to do with it.
        if not deadline.expired():
            raise
        raise ToolLoopLimitError(
            f"wall-clock ceiling reached after {max_wall_clock_s}s: "
            f"{iterations} LLM call(s), {tool_calls_made} tool call(s) made",
            usage=_usage(),
        ) from None
