"""Unit tests — Event dataclass invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from agentive_backend.shared.event_bus.types import Event


def _make_event(**overrides: object) -> Event:
    base: dict[str, object] = {
        "id": uuid4(),
        "event_type": "system.started",
        "payload": {"version": "0.1.0"},
        "correlation_id": uuid4(),
        "created_at": datetime.now(UTC),
        "tenant_id": None,
    }
    base.update(overrides)
    return Event(**base)  # type: ignore[arg-type]


def test_event_is_frozen() -> None:
    event = _make_event()
    with pytest.raises((AttributeError, TypeError)):
        event.event_type = "system.shutdown"  # type: ignore[misc]


def test_event_equals_by_value_but_is_not_hashable() -> None:
    """Frozen dataclass equals by field values; payload dict prevents hashing.

    P22 — the prior name (``test_event_is_hashable``) lied about its intent:
    the `payload: dict[str, Any]` field is unhashable, so ``hash(event)``
    raises ``TypeError``. We document equality (which DOES work) and the
    deliberate hash limitation in one place.
    """
    e1 = _make_event()
    # Build an Event with the same field values — should equal but be a
    # different object identity.
    e2 = Event(
        id=e1.id,
        event_type=e1.event_type,
        payload=e1.payload,
        correlation_id=e1.correlation_id,
        created_at=e1.created_at,
        tenant_id=e1.tenant_id,
    )
    assert e1 == e2
    # Hash falls back to the tuple-hash of fields; payload (dict) is
    # unhashable so the call raises. Document explicitly so a future
    # contributor doesn't waste time "fixing" the dataclass.
    with pytest.raises(TypeError):
        hash(e1)


def test_event_required_fields() -> None:
    """Missing positional/required field must raise at construction."""
    with pytest.raises(TypeError):
        Event()  # type: ignore[call-arg]
