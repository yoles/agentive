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


class ToolLoopLimitError(LLMError):
    """A ceiling was reached before the model stopped asking for tools.

    Subclasses :class:`LLMError` so the existing error classification and the
    workflow engine's ``error_policy`` dispatcher route it like any other LLM
    fault, rather than it being a new exception family nobody handles.
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
    """The full transcript, tool results included, ready to be persisted."""
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

            On the wall-clock path the tokens already billed are LOST from the
            result, because there is no result. That is true of the other two
            ceilings as well, and it is the caller's failure path that records
            the node as failed; no ceiling here fabricates a partial success.
        ValueError: a ceiling is not strictly positive. A ``max_iterations``
            of 0 would make the function return without ever calling the
            model, which no caller can mean.
    """
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")
    if max_tool_calls < 0:
        raise ValueError(f"max_tool_calls must be >= 0, got {max_tool_calls}")
    if not math.isfinite(max_wall_clock_s) or max_wall_clock_s <= 0:
        raise ValueError(f"max_wall_clock_s must be finite and > 0, got {max_wall_clock_s!r}")

    transcript: list[ChatMessage] = list(messages)
    iterations = 0
    tool_calls_made = 0
    total_input = 0
    total_output = 0
    total_cost: Decimal | None = None

    # `asyncio.timeout` rather than a deadline checked between iterations: a
    # check between turns bounds nothing when a single turn is what hangs, and
    # a single turn contains both an LLM call and up to `max_tool_calls` tool
    # executions. This cancels wherever the loop actually is.
    deadline = asyncio.timeout(max_wall_clock_s)
    try:
        async with deadline:
            while True:
                completion = await complete(transcript, tools)
                iterations += 1
                total_input += completion.input_tokens
                total_output += completion.output_tokens
                if completion.cost_estimate_usd is not None:
                    total_cost = (total_cost or Decimal(0)) + completion.cost_estimate_usd

                if not completion.tool_calls:
                    # The model answered. This is the ONLY exit that returns a result.
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
                        f"{len(completion.tool_calls)} more requested, ceiling {max_tool_calls}"
                    )
                if iterations >= max_iterations:
                    # Checked AFTER the tool-call ceiling and only when the model is
                    # still asking: a model that answers on its last allowed turn has
                    # not exceeded anything, and failing it would be wrong.
                    raise ToolLoopLimitError(
                        f"iteration ceiling reached: {iterations} LLM calls, ceiling "
                        f"{max_iterations}, and the model is still requesting tools"
                    )

                # The assistant turn that REQUESTED the tools must stay in the
                # transcript: without it the provider sees results answering nothing,
                # and Anthropic rejects the exchange outright.
                transcript.append(ChatMessage(role="assistant", content=completion.text))
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
            f"{iterations} LLM call(s), {tool_calls_made} tool call(s) made"
        ) from None
