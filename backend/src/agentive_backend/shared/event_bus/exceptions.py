"""Event bus exceptions — RFC 7807 ready (mapped via FastAPI handler)."""

from __future__ import annotations

from agentive_backend.shared.exceptions import AgentiveError


class MissingCorrelationIdError(AgentiveError):
    """Raised when ``publish()`` is called without a bound correlation_id.

    The middleware sets a correlation_id for every HTTP request. Background
    tasks (scheduler, lifespan hooks) MUST pass ``correlation_id=`` explicitly.
    """

    type = "/errors/event-bus/missing-correlation"
    title = "Missing correlation_id"
    status = 500


class InvalidEventTypeError(AgentiveError):
    """Raised when an event_type does not match ``module.entity.action``."""

    type = "/errors/event-bus/invalid-event-type"
    title = "Invalid event type"
    status = 422


class OutboxWorkerNotRunningError(AgentiveError):
    """Raised on operations that require :class:`OutboxWorker` to be started."""

    type = "/errors/event-bus/worker-not-running"
    title = "Outbox worker not running"
    status = 503
