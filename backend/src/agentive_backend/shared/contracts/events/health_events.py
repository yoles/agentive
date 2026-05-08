"""Health-check telemetry events — emitted by ``/ready`` after each probe."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field


class HealthCheckEvent(BaseModel):
    """Snapshot of a ``/ready`` invocation outcome."""

    event_type: ClassVar[str] = "system.health.checked"

    status: str = Field(..., description="``ready`` or ``not_ready``")
    checks: dict[str, str] = Field(..., description="Sub-component → status")
