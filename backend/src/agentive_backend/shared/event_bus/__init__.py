"""Event bus — PostgreSQL LISTEN/NOTIFY + Outbox Pattern.

**Stub Sprint 0** — implementation complete in Story 1.4.

Future public API :
    publish(event_type: str, payload: dict) -> None
    subscribe(pattern: str, handler: Callable) -> Subscription

Feature modules communicate EXCLUSIVELY via this bus — direct imports between
`features.m*` modules are forbidden (enforced by `import-linter`).
"""

from __future__ import annotations

from typing import Any


def publish(event_type: str, payload: dict[str, Any]) -> None:
    """Publish an event via the outbox pattern.

    To be implemented in Story 1.4.
    """
    raise NotImplementedError("Event bus implemented in Story 1.4")
