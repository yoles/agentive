"""Tiny helpers — keep this module small and focused."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def now_utc() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(UTC)


def uuid_v7() -> uuid.UUID:
    """Return a fresh UUID v7 — time-ordered, RFC 9562 compliant.

    Wraps :func:`uuid.uuid7` (Python 3.14+ stdlib) for a single import path.
    Prefer this helper over direct ``uuid.uuid7()`` calls so we can swap the
    implementation (e.g. to ULID) in one place if needed.
    """
    return uuid.uuid7()
