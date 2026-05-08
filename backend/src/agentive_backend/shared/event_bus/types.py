"""Event bus core types — Event envelope + Subscription handle.

These types are part of the public API consumed by feature handlers.
They are intentionally minimal and immutable so handlers cannot mutate
state shared across dispatches.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

EventHandler = Callable[["Event"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Event:
    """Immutable event envelope dispatched to subscribers.

    Attributes
    ----------
    id
        Server-generated UUID of the outbox row.
    event_type
        Canonical name following ``module.entity.action`` (cf. ``naming``).
    payload
        Arbitrary JSON-serializable mapping. Handlers MUST treat as read-only.
    correlation_id
        End-to-end traceability ID propagated through logs / spans / events.
    created_at
        Timestamp of the outbox INSERT (server-side ``DEFAULT now()``).
    tenant_id
        SaaS-readiness — ``None`` in single-tenant MVP.
    """

    id: UUID
    event_type: str
    payload: dict[str, Any]
    correlation_id: UUID
    created_at: datetime
    tenant_id: UUID | None = None


@dataclass
class Subscription:
    """Handle returned by :func:`subscribe` — used to cancel a registration.

    Tests should call :meth:`unsubscribe` in teardown to keep the global
    registry clean between runs.

    ``handler`` and ``matcher`` are required at construction (P25) — a
    Subscription with no handler is meaningless and was a footgun in the
    initial design (silently skipped during dispatch).
    """

    matcher: str | re.Pattern[str]
    handler: EventHandler
    id: UUID = field(default_factory=uuid4)

    def matches(self, event_type: str) -> bool:
        """Return True if this subscription matches the given event type.

        Uses :meth:`re.Pattern.fullmatch` (P9) — partial matches surprise
        callers (``re.compile(r"m3")`` would otherwise match ``m3.x.malicious``).
        """
        if isinstance(self.matcher, str):
            return self.matcher == event_type
        return bool(self.matcher.fullmatch(event_type))

    def unsubscribe(self) -> None:
        """Remove this subscription from the global registry.

        ``subscriber`` is imported here (not at module top) because
        ``subscriber`` imports :class:`Subscription` from this module —
        a top-level import would create a cycle at module load time.
        """
        from agentive_backend.shared.event_bus import subscriber

        subscriber._remove(self)
