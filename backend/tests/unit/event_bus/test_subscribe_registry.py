"""Unit tests — subscriber registry / pattern matching (no DB)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agentive_backend.shared.event_bus import subscribe
from agentive_backend.shared.event_bus.subscriber import (
    _clear_subscriptions_for_tests,
    _match_subscriptions,
)
from agentive_backend.shared.event_bus.types import Event


@pytest.fixture(autouse=True)
async def _isolate_registry() -> AsyncIterator[None]:
    _clear_subscriptions_for_tests()
    yield
    _clear_subscriptions_for_tests()


async def _noop(_event: Event) -> None:
    return None


async def test_exact_match_only_matches_exact_string() -> None:
    sub = await subscribe("system.started", _noop)

    assert _match_subscriptions("system.started") == [sub]
    assert _match_subscriptions("system.shutdown") == []
    assert _match_subscriptions("m3.workflow.started") == []


async def test_pattern_match_uses_regex() -> None:
    sub = await subscribe(re.compile(r"^m3\..*$"), _noop)

    matched = _match_subscriptions("m3.workflow.started")
    assert matched == [sub]
    assert _match_subscriptions("m4.chunk.indexed") == []


async def test_unsubscribe_removes_from_registry() -> None:
    sub = await subscribe("system.started", _noop)
    assert _match_subscriptions("system.started") == [sub]

    sub.unsubscribe()
    assert _match_subscriptions("system.started") == []


async def test_multiple_handlers_same_event_all_returned() -> None:
    sub_a = await subscribe("system.started", _noop)
    sub_b = await subscribe("system.started", _noop)

    matched = _match_subscriptions("system.started")
    assert sub_a in matched
    assert sub_b in matched
    assert len(matched) == 2


async def test_subscription_matches_uses_event_type() -> None:
    """Subscription.matches is the canonical pattern — used by registry too."""
    event = Event(
        id=uuid4(),
        event_type="m3.workflow.started",
        payload={},
        correlation_id=uuid4(),
        created_at=datetime.now(UTC),
        tenant_id=None,
    )
    sub_str = await subscribe("m3.workflow.started", _noop)
    sub_pattern = await subscribe(re.compile(r"^m3\..*$"), _noop)
    sub_other = await subscribe("m4.chunk.indexed", _noop)

    assert sub_str.matches(event.event_type) is True
    assert sub_pattern.matches(event.event_type) is True
    assert sub_other.matches(event.event_type) is False
