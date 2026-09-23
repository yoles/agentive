"""Unit tests for the MCP deadline/teardown race guard (CR 2026-09-06, P15).

``discover_tools`` and ``call_tool`` both wrap their work in
``asyncio.timeout``. When the deadline fires under the setrlimit sandbox
fallback, the ``stdio_client`` teardown can win the race and surface an
exception GROUP instead of the bare ``CancelledError`` that ``asyncio.timeout``
knows how to convert. Both handlers then fall back on ``cm.expired()``.

That fallback used to convert *anything* into a timeout, including a
concurrent cancellation — which silently defeated cooperative shutdown. These
tests pin the guard that decides whether the deadline may claim the group.
They are async because the guard reads ``asyncio.current_task()``, which needs
a running loop.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agentive_backend.infra.mcp.client import _deadline_won_the_race


@pytest.mark.asyncio
async def test_a_group_that_beat_the_deadline_is_not_a_timeout() -> None:
    """No deadline, no timeout: the original group must reach the caller
    untouched rather than being relabelled."""
    assert _deadline_won_the_race(expired=False) is False


@pytest.mark.asyncio
async def test_an_expired_deadline_claims_the_teardown_group() -> None:
    assert _deadline_won_the_race(expired=True) is True


@pytest.mark.asyncio
async def test_a_pending_external_cancellation_outranks_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.timeout.__aexit__`` withdraws its OWN cancellation request
    before re-raising, so a non-zero ``Task.cancelling()`` here can only come
    from somebody else (an enclosing timeout, a shutdown). Turning that into
    an ``MCPExecutionTimeoutError`` would swallow the cancellation and the
    caller would never stop."""
    monkeypatch.setattr(asyncio, "current_task", lambda: SimpleNamespace(cancelling=lambda: 1))

    assert _deadline_won_the_race(expired=True) is False


@pytest.mark.asyncio
async def test_no_running_task_falls_back_to_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: with no task there is no cancellation to protect."""
    monkeypatch.setattr(asyncio, "current_task", lambda: None)

    assert _deadline_won_the_race(expired=True) is True
