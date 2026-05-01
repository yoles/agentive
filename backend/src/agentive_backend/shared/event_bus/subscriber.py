"""Subscription registry — process-local list of handlers.

Process-local: the registry lives in this module's namespace. There is one
registry per Python process. When the SaaS migration to multi-instance lands
(cf. ``event-bus-migration-trigger.md``), each instance keeps its own
subscribers and the broker (Redis Streams) handles cross-instance fan-out.

Concurrency note (P16)
----------------------
Reads and writes to ``_SUBSCRIPTIONS`` are protected by a ``threading.Lock``
rather than ``asyncio.Lock``. CPython's GIL already makes ``list.append`` /
``list.remove`` atomic at the bytecode level, but holding a real lock around
all three call sites (subscribe, remove, snapshot) makes the invariant
explicit and survives PyPy / no-GIL builds. The asyncio.Lock approach used
in the initial implementation only protected ``subscribe()`` while
``_match_subscriptions`` and ``_remove`` ran lock-free — relying on the GIL
by accident.
"""

from __future__ import annotations

import contextlib
import re
import sys
import threading

from agentive_backend.shared.event_bus.types import EventHandler, Subscription

_SUBSCRIPTIONS: list[Subscription] = []
_LOCK = threading.Lock()


async def subscribe(
    event_type_or_pattern: str | re.Pattern[str],
    handler: EventHandler,
) -> Subscription:
    """Register a handler for an exact ``event_type`` string or a regex pattern.

    Returns the :class:`Subscription` so callers can unsubscribe later.
    Patterns must be pre-compiled :class:`re.Pattern` objects — passing raw
    strings always means "exact match".
    """
    sub = Subscription(matcher=event_type_or_pattern, handler=handler)
    with _LOCK:
        _SUBSCRIPTIONS.append(sub)
    return sub


def _match_subscriptions(event_type: str) -> list[Subscription]:
    """Return all subscriptions matching ``event_type`` (snapshot of registry).

    Sync — called from the dispatch hot path. We hold the lock briefly to
    take a stable snapshot, then iterate outside the critical section so
    handler dispatch never blocks new ``subscribe()`` calls.
    """
    with _LOCK:
        snapshot = list(_SUBSCRIPTIONS)
    return [sub for sub in snapshot if sub.matches(event_type)]


def _remove(sub: Subscription) -> None:
    """Remove a subscription from the registry (idempotent)."""
    with _LOCK, contextlib.suppress(ValueError):
        _SUBSCRIPTIONS.remove(sub)


def _clear_subscriptions_for_tests() -> None:
    """Reset the global registry between tests. Never call in production.

    Gated (P15): refuses to run unless pytest is loaded into ``sys.modules``,
    which prevents accidental use from a feature module that bypassed
    ``import-linter`` Contract 4 (e.g. via ``importlib``).
    """
    if "pytest" not in sys.modules:
        raise RuntimeError(
            "_clear_subscriptions_for_tests() called outside a pytest run. "
            "This helper is for tests only — handlers in production must "
            "unsubscribe via Subscription.unsubscribe()."
        )
    with _LOCK:
        _SUBSCRIPTIONS.clear()
