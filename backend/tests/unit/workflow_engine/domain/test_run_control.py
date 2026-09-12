"""Pure transition table for run control — Story 4.6 T2.5, AC1.

The whole point of :mod:`domain.run_control` is that the 409 decision is
made in ONE place, by a pure function, so it can be tested exhaustively
rather than by sampling: the matrix below is the full cartesian product of
3 actions x 5 statuses, with no hole for an implementation to slip through.
"""

from __future__ import annotations

import itertools

import pytest

from agentive_backend.features.workflow_engine.domain.run_control import (
    RUN_STATUSES,
    TERMINAL_EVENT_SUFFIXES,
    TERMINAL_STATUS_EVENT_TYPES,
    TERMINAL_STATUSES,
    RunAction,
    Transition,
    resolve_transition,
)
from agentive_backend.shared.exceptions import ConflictError

# (action, current_status) -> expected Transition. EVERY legal pair, and
# only those; anything absent must raise. Mirrors the AC1 table verbatim.
_LEGAL: dict[tuple[RunAction, str], Transition] = {
    ("pause", "running"): Transition(
        allowed_from=("running",), signal="pause", immediate_status=None
    ),
    # `overrides=("pause",)` — a cancel may replace a pause the driver has
    # not reached yet. The reverse is deliberately NOT true.
    ("cancel", "running"): Transition(
        allowed_from=("running", "paused"),
        signal="cancel",
        immediate_status=None,
        overrides=("pause",),
    ),
    ("cancel", "paused"): Transition(
        allowed_from=("running", "paused"), signal=None, immediate_status="cancelled"
    ),
    ("resume", "paused"): Transition(
        allowed_from=("paused",), signal=None, immediate_status="running"
    ),
}

_ACTIONS: tuple[RunAction, ...] = ("pause", "resume", "cancel")


@pytest.mark.parametrize(("action", "status"), sorted(itertools.product(_ACTIONS, RUN_STATUSES)))
def test_resolve_transition_when_every_action_status_pair_should_match_the_ac1_matrix(
    action: RunAction, status: str
) -> None:
    expected = _LEGAL.get((action, status))
    if expected is None:
        with pytest.raises(ConflictError):
            resolve_transition(action, status)
        return
    assert resolve_transition(action, status) == expected


def test_resolve_transition_when_matrix_is_enumerated_should_cover_fifteen_pairs() -> None:
    """Guard against a status being added without extending the matrix."""
    assert len(RUN_STATUSES) == 5
    assert len(list(itertools.product(_ACTIONS, RUN_STATUSES))) == 15


@pytest.mark.parametrize(
    ("action", "status", "allowed"),
    [
        ("pause", "paused", ("running",)),
        ("resume", "running", ("paused",)),
        ("cancel", "completed", ("running", "paused")),
    ],
)
def test_resolve_transition_when_illegal_should_carry_allowed_from_in_context(
    action: RunAction, status: str, allowed: tuple[str, ...]
) -> None:
    """The 409 body must tell the caller what WOULD have been legal."""
    with pytest.raises(ConflictError) as excinfo:
        resolve_transition(action, status)
    context = excinfo.value.context
    assert context["current_status"] == status
    assert tuple(context["allowed_from"]) == allowed


def test_resolve_transition_when_status_is_unknown_should_conflict_not_crash() -> None:
    """A row carrying a status no domain literal covers (a future story, a
    manual DB edit) must degrade to 409, never to a KeyError 500."""
    with pytest.raises(ConflictError):
        resolve_transition("pause", "pausing")


def test_terminal_statuses_when_read_should_include_cancelled() -> None:
    """Single source of truth — `router.py`'s SSE loop imports THIS set.
    Without `cancelled` a client hangs until `_MAX_STREAM_DURATION_S`."""
    assert frozenset({"completed", "error", "cancelled"}) == TERMINAL_STATUSES


def test_terminal_statuses_when_read_should_exclude_paused() -> None:
    """A paused run can still resume — the SSE stream must stay open."""
    assert "paused" not in TERMINAL_STATUSES


def test_allowed_from_when_read_should_agree_with_the_transition_table() -> None:
    """`allowed_from` answers "what WOULD have worked?" in every 409 body, and
    it used to be a hand-written literal sitting beside a comment claiming it
    was "derived from the table above so the two can never disagree". They
    could: adding a legal pair left every 409 advertising the old set.

    Recomputing the expectation from `resolve_transition` itself — the only
    function that decides what is legal — is what makes this a check rather
    than a restatement of the same literal.
    """
    for action in ("pause", "resume", "cancel"):
        legal = tuple(
            status
            for status in RUN_STATUSES
            if _resolves(action, status)  # type: ignore[arg-type]
        )
        # Every legal pair reports the SAME set, and that set is exactly the
        # statuses from which this action actually resolves.
        for status in legal:
            transition = resolve_transition(action, status)  # type: ignore[arg-type]
            assert frozenset(transition.allowed_from) == frozenset(legal)
        # …and an illegal start is told that same set, not a stale copy.
        for status in RUN_STATUSES:
            if status in legal:
                continue
            with pytest.raises(ConflictError) as excinfo:
                resolve_transition(action, status)  # type: ignore[arg-type]
            assert frozenset(excinfo.value.context["allowed_from"]) == frozenset(legal)


def _resolves(action: RunAction, status: str) -> bool:
    try:
        resolve_transition(action, status)
    except ConflictError:
        return False
    return True


# ─── Review lot 8 (P-E) — the event side of the terminal vocabulary ────


def test_every_terminal_status_declares_the_event_it_publishes() -> None:
    """The guard that makes a fourth terminal status impossible to half-add.

    `router.py`'s SSE loop closes on two things — a terminal STATUS read
    from the row, and a terminal EVENT received from the bus. T5.4 moved the
    status set here and declared the duplication closed, while the event set
    stayed a literal in `router.py`. Adding a status to one and forgetting
    the other leaves every client of a run that reaches it hanging until the
    one-hour stream ceiling: precisely the bug T5.4 was written to fix, still
    reachable through the other half.
    """
    assert set(TERMINAL_STATUS_EVENT_TYPES) == TERMINAL_STATUSES


def test_the_status_and_event_vocabularies_are_not_interchangeable() -> None:
    """Why this is a table and not an alias.

    `error` is a reserved event name in the browser `EventSource` API — it
    fires on transport failure — so the run's failure event is named
    `failed`. Aliasing the two sets would make the SSE loop watch for an
    event nobody publishes, and it would never see a run fail.
    """
    assert "error" in TERMINAL_STATUSES
    assert "error" not in TERMINAL_EVENT_SUFFIXES
    assert "failed" in TERMINAL_EVENT_SUFFIXES
    assert "failed" not in TERMINAL_STATUSES


def test_terminal_event_suffixes_are_read_off_the_published_event_types() -> None:
    """Derived, not retyped: renaming an event class's `event_type` must
    move this set with it rather than leave a stale literal behind.

    Asserted against the three names a browser client actually listens for.
    The first version recomputed the production expression character for
    character, which no implementation could fail (review lot 10, T4).
    """
    from agentive_backend.shared.contracts.events import WorkflowRunFailedEvent

    assert WorkflowRunFailedEvent.event_type.endswith(".failed")
    assert {"completed", "failed", "cancelled"} == TERMINAL_EVENT_SUFFIXES
    assert all("." not in suffix for suffix in TERMINAL_EVENT_SUFFIXES)
