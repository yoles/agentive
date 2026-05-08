"""System lifecycle events — emitted by ``app.lifespan``.

These are the canonical Sprint 0 events used as smoke signals (publish →
outbox → NOTIFY → worker → handler). They double as the reference for
modules adding their own contracts.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field


class SystemStartedEvent(BaseModel):
    """Emitted once at the end of :func:`app.lifespan.lifespan` startup."""

    event_type: ClassVar[str] = "system.app.started"

    version: str = Field(..., description="Application semver, e.g. ``0.1.0``")


class SystemShutdownEvent(BaseModel):
    """Emitted once when the FastAPI lifespan teardown begins."""

    event_type: ClassVar[str] = "system.app.shutdown"

    uptime_seconds: float = Field(..., ge=0)
