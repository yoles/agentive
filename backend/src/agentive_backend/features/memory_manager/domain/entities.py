"""Memory Manager domain entities — the read-time lifecycle rule.

:class:`MemoryChunk` here is the framework-free domain view (NOT the ORM row in
``infra/db/models``). It owns the answer to "is this chunk expired / archivable
*now*", so that rule is unit-testable in microseconds without a DB, a clock
monkeypatch, or the app — the concrete win the audit (§4.5) asked for before
``memory_manager`` is implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agentive_backend.features.memory_manager.domain.value_objects import RetentionPolicy


@dataclass(frozen=True, slots=True)
class MemoryChunk:
    """A retained memory chunk's lifecycle state.

    Only the fields the lifecycle rule needs — the full content/embedding live
    on the ORM row. ``expires_at`` is the absolute instant computed at write
    time (see :meth:`RetentionPolicy.expires_at`); ``archived_at`` is set once
    the chunk has actually been archived.
    """

    created_at: datetime
    expires_at: datetime | None = None
    archived_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        """True iff the chunk has a past expiry instant.

        A chunk with no ``expires_at`` (policy TTL ``None``) never expires.
        """
        return self.expires_at is not None and now >= self.expires_at

    def is_archived(self) -> bool:
        return self.archived_at is not None

    def is_archivable(self, policy: RetentionPolicy, now: datetime) -> bool:
        """True iff the chunk is not yet archived and has reached the policy's
        archival age. An expired chunk is not "archivable" — expiry (deletion)
        takes precedence over archival."""
        if self.is_archived() or self.is_expired(now):
            return False
        archivable_at = policy.archivable_at(self.created_at)
        return archivable_at is not None and now >= archivable_at


__all__ = ["MemoryChunk"]
