"""Event bus — PostgreSQL LISTEN/NOTIFY + Outbox Pattern.

Public API
----------
:func:`publish`
    INSERT an event into ``outbox_events`` within the caller's transaction.
:func:`publish_and_commit`
    INSERT, commit the session, then NOTIFY the worker. Fire-and-forget.
:func:`emit_notify`
    Standalone NOTIFY helper for callers that own the commit themselves.
:func:`notify_best_effort`
    :func:`emit_notify` wrapped in the bus resilience policy (NOTIFY failure
    is logged, never raised — the worker's poll fallback picks the event up).
:func:`subscribe`
    Register an async handler for an exact event_type or a regex pattern.
:class:`OutboxWorker`
    Consumer + replay loop. Wired in :mod:`agentive_backend.app.lifespan`.

Feature modules MUST go through this package — direct imports of
``shared.event_bus.publisher`` / ``.outbox`` / ``.subscriber`` from
``features/*`` are blocked by ``import-linter`` Contract 4.
"""

from __future__ import annotations

from agentive_backend.shared.event_bus.exceptions import (
    InvalidEventTypeError,
    MissingCorrelationIdError,
    OutboxWorkerNotRunningError,
)
from agentive_backend.shared.event_bus.outbox import OutboxWorker
from agentive_backend.shared.event_bus.publisher import (
    OUTBOX_CHANNEL,
    emit_notify,
    notify_best_effort,
    publish,
    publish_and_commit,
)
from agentive_backend.shared.event_bus.subscriber import subscribe
from agentive_backend.shared.event_bus.types import Event, EventHandler, Subscription

__all__ = [
    "OUTBOX_CHANNEL",
    "Event",
    "EventHandler",
    "InvalidEventTypeError",
    "MissingCorrelationIdError",
    "OutboxWorker",
    "OutboxWorkerNotRunningError",
    "Subscription",
    "emit_notify",
    "notify_best_effort",
    "publish",
    "publish_and_commit",
    "subscribe",
]
