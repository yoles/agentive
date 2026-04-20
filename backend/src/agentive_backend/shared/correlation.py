"""Correlation ID propagation — UUID v7 / ULID via ContextVar.

The correlation ID is set by the FastAPI middleware (see `app.middleware`) at
the entry of every request and propagated to logs + events bus + spans OTel.

Python 3.14 stdlib provides ``uuid.uuid7``. If unavailable, fallback to ULID
(not imported for now to avoid adding a dep in Sprint 0 — revisit if needed).
"""

from __future__ import annotations

from contextvars import ContextVar

from agentive_backend.shared.utils import uuid_v7

_correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def new_correlation_id() -> str:
    """Generate a new correlation ID (UUID v7 — time-ordered)."""
    return str(uuid_v7())


def set_correlation_id(value: str) -> None:
    """Bind a correlation ID to the current async context."""
    _correlation_id_var.set(value)


def get_correlation_id() -> str | None:
    """Return the correlation ID bound to the current async context, if any."""
    return _correlation_id_var.get()


def require_correlation_id() -> str:
    """Return the correlation ID — raise if none bound (should never happen post-middleware)."""
    value = _correlation_id_var.get()
    if value is None:
        raise RuntimeError(
            "No correlation_id bound to this context — middleware not executed ?"
        )
    return value
