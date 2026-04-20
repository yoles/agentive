"""Event schemas — published on the event bus.

**Stub Sprint 0** — filled progressively starting Story 1.4 (event bus runtime).

Naming convention : `module.entity.action`
    - m2.agent.created
    - m3.workflow.started, m3.workflow.completed, m3.workflow.failed
    - m4.chunk.indexed, m4.chunk.archived
"""

from __future__ import annotations


def describe_event(event_type: str) -> dict[str, object]:
    """Placeholder — event schema registry implemented Story 1.4."""
    raise NotImplementedError(
        "Event schema registry implemented Story 1.4 (event bus runtime)."
    )
