"""Node-level retry policy — pure, I/O-free (Story 4.6 T8.1, AC3, défer D13).

Story 2.2 validated and persisted ``agent_templates.config["error_policy"]``
(``on_timeout``/``max_retries``/``backoff_strategy``) and said in as many
words that the dispatcher which APPLIES it would arrive in Story 4.6. This
module is the pure half of that dispatcher: it turns free-form JSONB into a
:class:`ResolvedErrorPolicy` and computes the delay between two attempts.
The loop that consumes it lives in ``engine/agent_node.py``.

**Three retry mechanisms exist in this codebase and merging them is the
mistake this module exists to prevent:**

======================  ====================================  ==============
What                    Who                                   Status
======================  ====================================  ==============
Chain fallback          ``LLMRouter.complete()`` (Story 1.6)   delivered
  provider A fails retriably -> try provider B with the
  equivalent model
Node retry              ``execute_agent_node`` (THIS, D13)     this story
  the WHOLE chain failed -> re-run the whole chain, with
  backoff, ``max_retries`` times
Intra-provider retry    ``retriable_same_provider`` bucket     Story 9.5
  429 from A -> wait, re-try A without switching provider      (empty by
                                                               decision)
======================  ====================================  ==============

Hence the semantics of ``on_timeout`` encoded in
:attr:`ResolvedErrorPolicy.effective_retries`: ``"fail_fast"`` means **zero
NODE retries**, never "no fallback". Disabling the provider chain would
contradict NFR12, which is not negotiable per template — an agent that truly
wants a single provider says so with ``provider_chain: ["anthropic"]``, the
gesture ``docs/runbooks/llm-usage.md`` already documents as canonical.

Defensive on every read: ``config`` is free-form JSONB that an import, a
migration or a test may have filled with anything, so a bad value falls back
to the Story 2.2 default instead of raising inside a node (same posture as
``agent_node._resolve_llm_params`` on ``llm_params``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

OnTimeout = Literal["retry_with_backoff", "fail_fast", "fallback_provider"]
BackoffStrategy = Literal["exponential", "linear", "constant"]

# Story 2.2 `ErrorPolicy` defaults, mirrored (not imported — `.import-linter`
# Contract 1 forbids `features.workflow_engine` importing
# `features.agent_registry`). Keep in sync manually.
DEFAULT_ON_TIMEOUT: Final[OnTimeout] = "retry_with_backoff"
DEFAULT_MAX_RETRIES: Final = 3
DEFAULT_BACKOFF_STRATEGY: Final[BackoffStrategy] = "exponential"

#: Ceiling on the max_retries a RESOLVED policy may report, mirroring Story
#: 2.2's ``Field(le=10)``. Re-enforced here because the schema only guards the
#: HTTP path, while the JSONB column can hold anything.
#:
#: It does NOT bound what the runtime executes, and it does NOT feed
#: ``recovery``'s stale threshold — :data:`MAX_RUNTIME_RETRIES` does
#: both, and is the only one ``recovery`` imports. Its whole remaining job is
#: to keep :class:`ResolvedErrorPolicy` inside the schema's contract, so that
#: the ``error_policy_retries_capped`` warning reports a "requested" value a
#: template author could actually have written.
#:
#: Raising this changes nothing about worst-case node duration. Raising
#: :data:`MAX_RUNTIME_RETRIES` changes it for every run in the process —
#: the two are not interchangeable, whatever their names suggest.
MAX_ERROR_POLICY_RETRIES: Final = 10

#: What the RUNTIME will actually execute, whatever a template asks for
#: (Story 4.6 T8.5). Deliberately BELOW :data:`MAX_ERROR_POLICY_RETRIES`, and
#: the divergence is the point: the schema validates an INTENTION a template
#: author may express, this constant guarantees a PROCESS INVARIANT.
#:
#: ``recovery.derive_stale_threshold_s`` computes it from the worst-case
#: duration of a single node, retries and backoff included. Derived against
#: 10 retries it exceeds an hour — so a genuinely crashed run would sit
#: unexamined for over an hour, defeating the recovery worker entirely. The
#: alternative to capping here was inflating that threshold, and between "a
#: template can make crash detection an hour slow for every OTHER run in the
#: process" and "a template's 8th retry is not honoured", the second is the
#: lesser harm. Exceeding it is logged, never silent.
MAX_RUNTIME_RETRIES: Final = 3

_ON_TIMEOUT_VALUES: Final[frozenset[str]] = frozenset(
    {"retry_with_backoff", "fail_fast", "fallback_provider"}
)
_BACKOFF_VALUES: Final[frozenset[str]] = frozenset({"exponential", "linear", "constant"})

#: Attempt index past which ``base * 2**n`` is guaranteed to exceed any
#: plausible ``max_s`` — used to cap BEFORE the exponentiation, so a corrupt
#: attempt counter can never raise ``OverflowError`` inside a node.
_MAX_EXPONENT: Final = 32


@dataclass(frozen=True, slots=True)
class ResolvedErrorPolicy:
    """A template's error policy, normalised and always safe to act on."""

    on_timeout: OnTimeout
    max_retries: int
    backoff_strategy: BackoffStrategy

    @property
    def effective_retries(self) -> int:
        """How many times the node re-runs the WHOLE provider chain.

        Zero for ``fail_fast`` and ``fallback_provider``: both mean "do not
        retry this node", and neither disables the chain itself — the chain
        is applied by ``LLMRouter.complete()`` on every single attempt,
        including the first and only one.
        """
        if self.on_timeout in ("fail_fast", "fallback_provider"):
            return 0
        return self.max_retries


def _coerce_max_retries(raw: Any) -> int:
    # `isinstance(True, int)` is True in Python — a JSON `true` would become
    # `1 retry` instead of falling back to the default.
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        return DEFAULT_MAX_RETRIES
    return min(raw, MAX_ERROR_POLICY_RETRIES)


def resolve_error_policy(config: dict[str, Any]) -> ResolvedErrorPolicy:
    """Read ``config["error_policy"]`` defensively (défer D13).

    Every unusable field independently falls back to its Story 2.2 default,
    field by field: a policy that names a valid ``max_retries`` but a
    nonsense ``backoff_strategy`` keeps the retries and defaults only the
    strategy.
    """
    raw = config.get("error_policy")
    policy = raw if isinstance(raw, dict) else {}

    on_timeout_raw = policy.get("on_timeout")
    on_timeout: OnTimeout = (
        on_timeout_raw  # type: ignore[assignment]
        if isinstance(on_timeout_raw, str) and on_timeout_raw in _ON_TIMEOUT_VALUES
        else DEFAULT_ON_TIMEOUT
    )

    strategy_raw = policy.get("backoff_strategy")
    backoff_strategy: BackoffStrategy = (
        strategy_raw  # type: ignore[assignment]
        if isinstance(strategy_raw, str) and strategy_raw in _BACKOFF_VALUES
        else DEFAULT_BACKOFF_STRATEGY
    )

    return ResolvedErrorPolicy(
        on_timeout=on_timeout,
        max_retries=_coerce_max_retries(policy.get("max_retries")),
        backoff_strategy=backoff_strategy,
    )


def backoff_delay_s(
    attempt: int, policy: ResolvedErrorPolicy, *, base_s: float, max_s: float
) -> float:
    """Seconds to wait before retry number ``attempt`` (0-based), capped.

    ``exponential``: ``base * 2**attempt`` — ``linear``:
    ``base * (attempt + 1)`` — ``constant``: ``base``. Every result is
    clamped to ``max_s`` (``AGENTIVE_WORKFLOW_RETRY_MAX_DELAY_S``).

    The clamp is not cosmetic. It is what keeps the worst-case node duration
    finite, which is in turn what lets ``recovery.derive_stale_threshold_s``
    stay derivable — without it, a retrying node eventually becomes
    indistinguishable from a crashed one and gets claimed and re-executed in
    parallel on the same ``thread_id``.

    A negative ``attempt`` clamps to 0 rather than producing a fractional or
    negative delay (``asyncio.sleep`` of a negative value returns instantly,
    which would silently turn the backoff into a hot loop).
    """
    n = max(attempt, 0)
    if policy.backoff_strategy == "constant":
        delay = base_s
    elif policy.backoff_strategy == "linear":
        delay = base_s * (n + 1)
    else:
        # Cap the EXPONENT before computing the power: `2 ** 5000` raises
        # `OverflowError` on floats, and this runs inside a node.
        delay = base_s * float(2 ** min(n, _MAX_EXPONENT))
    return min(delay, max_s)


__all__ = [
    "DEFAULT_BACKOFF_STRATEGY",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_ON_TIMEOUT",
    "MAX_ERROR_POLICY_RETRIES",
    "MAX_RUNTIME_RETRIES",
    "BackoffStrategy",
    "OnTimeout",
    "ResolvedErrorPolicy",
    "backoff_delay_s",
    "resolve_error_policy",
]
