"""Run-control state machine — pure, I/O-free (Story 4.6 T2, AC1).

The single source of truth for "is this pause/resume/cancel legal right
now?". Every 409 this feature returns is decided HERE, by
:func:`resolve_transition`, so the answer cannot drift between the HTTP
layer, the service and the driver.

Why a ``control_signal`` COLUMN and not ``pausing``/``cancelling`` statuses
(the design decision this module encodes):

``pause`` and ``cancel`` on a LIVE run are REQUESTS, not transitions. The
driver is mid-superstep somewhere; it will observe the request at the next
superstep boundary and only then move the row. Modelling the request as a
status would have meant teaching four separate pieces of code about it —
``claim_stale_running`` (filters ``status = "running"``),
``update_status(only_if_status=...)`` (compares against ONE value),
``_resume_run``'s ``status != "running"`` guard, and ``_abandon_one``'s
``only_if_status="running"`` — four chances to miss a case. A separate
column leaves all four untouched: the run stays ``running`` while it runs.

It also makes the mechanism multi-worker by construction. The signal
travels through the database, not through an in-process registry, so a
``pause`` issued on API replica A reaches a run driven by replica B. And a
run whose request was never observed (the process died in between) stays
``running``, hence stays eligible for the recovery sweep — which resumes it
and makes it observe the signal it missed.

``paused`` and ``cancelled``, by contrast, ARE real statuses: they describe
where the run IS, not what someone asked for. ``workflow_runs.status`` is a
free ``String(50)`` (``infra/db/models.py``), so they need no migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from agentive_backend.shared.contracts.events import (
    WorkflowRunCancelledEvent,
    WorkflowRunCompletedEvent,
    WorkflowRunFailedEvent,
)
from agentive_backend.shared.exceptions import ConflictError

#: What a caller can ask for. NOT the same vocabulary as
#: :data:`ControlSignal`: ``resume`` is never a signal (there is no live
#: driver to observe it — it starts a new one).
RunAction = Literal["pause", "resume", "cancel"]

#: What gets written to ``workflow_runs.control_signal`` for a LIVE run,
#: to be observed by the driver at its next superstep boundary.
ControlSignal = Literal["pause", "cancel"]

RunStatus = Literal["running", "paused", "completed", "error", "cancelled"]

#: Every status a run row can carry, in the order of the AC1 matrix. Used by
#: the exhaustive transition test; a new status added without extending
#: :data:`_TRANSITIONS` fails there rather than at runtime.
RUN_STATUSES: Final[tuple[RunStatus, ...]] = (
    "running",
    "paused",
    "completed",
    "error",
    "cancelled",
)

#: Statuses from which a run never moves again. THE definition — Story 4.6
#: T2.4: ``features/workflow_engine/router.py``'s SSE loop imports this
#: instead of redefining its own literal, which is how ``cancelled`` used to
#: be missing there (a cancelled run left its SSE clients connected until the
#: one-hour ``_MAX_STREAM_DURATION_S`` ceiling).
#:
#: ``paused`` is deliberately NOT terminal: a paused run can resume, so its
#: stream must stay open. This is the kind of asymmetry a hurried reader
#: "fixes" in the wrong direction — it is intentional.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({"completed", "error", "cancelled"})

#: The event each terminal status publishes when the run settles there.
#:
#: T2.4 moved :data:`TERMINAL_STATUSES` here and said the duplication was
#: closed; it was closed by half. ``router.py`` kept a SECOND literal for the
#: event side (``frozenset({"completed", "failed", "cancelled"})``) right
#: beside the import — so a fourth terminal status would still have to be
#: remembered in two places, which is the failure mode the move was meant to
#: end.
#:
#: It could not simply be aliased, and that is the whole reason it survived:
#: **the mapping is not the identity**. The status is ``error`` and the event
#: is ``failed`` — deliberately, because ``error`` is a reserved event name in
#: the browser ``EventSource`` API (see ``router.py``'s note). An alias would
#: have made the SSE loop miss the one terminal event it is most likely to
#: receive. A table is what the asymmetry requires.
#:
#: Read off the event classes rather than retyped, so a renamed event updates
#: this by construction. Coverage of :data:`TERMINAL_STATUSES` is asserted by
#: ``tests/unit/workflow_engine/domain/test_run_control.py`` — the same
#: posture :data:`RUN_STATUSES` takes with ``_TRANSITIONS``: a new terminal
#: status fails a test rather than silently leaving a stream open.
TERMINAL_STATUS_EVENT_TYPES: Final[dict[RunStatus, str]] = {
    "completed": WorkflowRunCompletedEvent.event_type,
    "error": WorkflowRunFailedEvent.event_type,
    "cancelled": WorkflowRunCancelledEvent.event_type,
}

#: Last dotted segment of each terminal event type — what the SSE loop
#: compares its ``event.event_type.rsplit(".", 1)[-1]`` against.
TERMINAL_EVENT_SUFFIXES: Final[frozenset[str]] = frozenset(
    event_type.rsplit(".", 1)[-1] for event_type in TERMINAL_STATUS_EVENT_TYPES.values()
)


@dataclass(frozen=True, slots=True)
class Transition:
    """What one legal (action, current status) pair does.

    Exactly one of ``signal``/``immediate_status`` is meaningful per pair:

    * ``signal`` set, ``immediate_status`` ``None`` — DEFERRED. The row keeps
      its status and gains a ``control_signal``; the driver settles it at the
      next superstep boundary. HTTP ``202``.
    * ``immediate_status`` set, ``signal`` ``None`` — IMMEDIATE. No driver is
      alive to observe anything (a ``paused`` run has none), so the caller's
      own request moves the row.
    """

    #: Statuses this ACTION accepts, all of them — not just the one matched.
    #: Surfaced in the 409 context so the caller learns what would work.
    allowed_from: tuple[RunStatus, ...]
    signal: ControlSignal | None
    immediate_status: RunStatus | None
    #: Pending signals this action is allowed to REPLACE. Empty for every
    #: action but ``cancel``.
    #:
    #: The write guard is otherwise ``control_signal IS NULL``, so whoever
    #: asks first wins — right between two requests of equal weight, wrong for
    #: the one operator sequence that matters: "pause… no, kill it". Worse, a
    #: process dying between writing a signal and observing it left the run
    #: answering 409 to ``pause``, ``resume`` AND ``cancel`` until the recovery
    #: sweep: roughly 25 minutes of a run nobody can control.
    #:
    #: Escalation is allowed in the TERMINAL direction only: ``cancel``
    #: overrides a pending ``pause``, never the reverse. A pause must not be
    #: able to revoke a cancellation someone was already told they had.
    overrides: tuple[ControlSignal, ...] = ()


# The AC1 table, verbatim and exhaustive: (action, status) -> what it does.
# Any pair absent from this mapping is a 409 — there is no fallthrough, no
# "probably fine" case.
#
# This is THE source, and the only one. `allowed_from` and `_ALLOWED_FROM`
# below are both computed from it, because the previous shape wrote the same
# fact in three places — once per `Transition`, plus a hand-maintained
# `_ALLOWED_FROM` whose comment claimed to be "derived from the table above
# so the two can never disagree" while being a literal nobody kept in sync.
# Adding a pair here now updates every 409 message by construction.
#: One entry: (signal to write, immediate status, pending signals it may replace).
_Effect = tuple[ControlSignal | None, RunStatus | None, tuple[ControlSignal, ...]]

_EFFECTS: Final[dict[tuple[RunAction, RunStatus], _Effect]] = {
    ("pause", "running"): ("pause", None, ()),
    # `cancel` may replace a pending `pause` — see `Transition.overrides`.
    ("cancel", "running"): ("cancel", None, ("pause",)),
    # No driver is running for a paused run, so nobody would ever observe a
    # signal here — the cancellation has to be terminal on the spot (200,
    # not 202: the code IS the message about whether the effect is deferred).
    ("cancel", "paused"): (None, "cancelled", ()),
    ("resume", "paused"): (None, "running", ()),
}

#: Per-action set of legal starting statuses, in declaration order. Answers
#: "what WOULD have worked?" for an action whose current status matched
#: nothing.
_ALLOWED_FROM: Final[dict[RunAction, tuple[RunStatus, ...]]] = {
    action: tuple(status for act, status in _EFFECTS if act == action) for action, _ in _EFFECTS
}

_TRANSITIONS: Final[dict[tuple[RunAction, RunStatus], Transition]] = {
    (action, status): Transition(
        allowed_from=_ALLOWED_FROM[action],
        signal=signal,
        immediate_status=immediate,
        overrides=overrides,
    )
    for (action, status), (signal, immediate, overrides) in _EFFECTS.items()
}


def resolve_transition(action: RunAction, current: str) -> Transition:
    """Return the :class:`Transition` for ``action`` from ``current``, or raise.

    ``current`` is typed ``str``, not :data:`RunStatus`, on purpose: it comes
    straight off a free ``String(50)`` column. A row carrying something no
    literal covers (a status from a future story, a manual DB edit) must get
    a 409 like any other illegal start — never a ``KeyError`` turned 500.
    """
    transition = _TRANSITIONS.get((action, current))  # type: ignore[arg-type]
    if transition is None:
        raise ConflictError(
            detail=(
                f"cannot {action} a run in status {current!r} "
                f"(allowed from: {', '.join(_ALLOWED_FROM[action])})"
            ),
            context={
                "current_status": current,
                "allowed_from": list(_ALLOWED_FROM[action]),
                "action": action,
            },
        )
    return transition


__all__ = [
    "RUN_STATUSES",
    "TERMINAL_EVENT_SUFFIXES",
    "TERMINAL_STATUSES",
    "TERMINAL_STATUS_EVENT_TYPES",
    "ControlSignal",
    "RunAction",
    "RunStatus",
    "Transition",
    "resolve_transition",
]
