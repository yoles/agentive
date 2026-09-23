"""Unit tests — MemoryChunk lifecycle rule (Sprint 2 DDD, audit A-13).

The read-time "expired / archivable now" decision, testable without a DB or a
real clock — a fixed ``now`` is passed in (the service injects the clock).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentive_backend.features.memory_manager.domain.entities import MemoryChunk
from agentive_backend.features.memory_manager.domain.value_objects import RetentionPolicy

_CREATED = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


# ─── is_expired ───────────────────────────────────────────────────


def test_chunk_without_expiry_never_expires() -> None:
    chunk = MemoryChunk(created_at=_CREATED, expires_at=None)
    assert chunk.is_expired(_CREATED + timedelta(days=3650)) is False


def test_chunk_expired_when_now_at_or_past_expiry() -> None:
    expires = _CREATED + timedelta(hours=1)
    chunk = MemoryChunk(created_at=_CREATED, expires_at=expires)
    assert chunk.is_expired(expires - timedelta(seconds=1)) is False
    assert chunk.is_expired(expires) is True  # boundary: >= is expired
    assert chunk.is_expired(expires + timedelta(seconds=1)) is True


# ─── is_archivable ────────────────────────────────────────────────


def test_chunk_not_archivable_when_policy_never_archives() -> None:
    chunk = MemoryChunk(created_at=_CREATED)
    assert chunk.is_archivable(RetentionPolicy(), _CREATED + timedelta(days=999)) is False


def test_chunk_archivable_once_age_reached() -> None:
    policy = RetentionPolicy(archive_after_seconds=86400)
    chunk = MemoryChunk(created_at=_CREATED)
    assert chunk.is_archivable(policy, _CREATED + timedelta(hours=23)) is False
    assert chunk.is_archivable(policy, _CREATED + timedelta(days=1)) is True


def test_already_archived_chunk_is_not_archivable_again() -> None:
    policy = RetentionPolicy(archive_after_seconds=1)
    chunk = MemoryChunk(
        created_at=_CREATED,
        archived_at=_CREATED + timedelta(seconds=5),
    )
    assert chunk.is_archived() is True
    assert chunk.is_archivable(policy, _CREATED + timedelta(days=1)) is False


def test_expired_chunk_takes_precedence_over_archival() -> None:
    # Expiry (deletion) wins: an expired chunk should not be reported as
    # archivable even though it has reached the archival age.
    policy = RetentionPolicy(archive_after_seconds=60)
    chunk = MemoryChunk(created_at=_CREATED, expires_at=_CREATED + timedelta(seconds=30))
    now = _CREATED + timedelta(seconds=120)
    assert chunk.is_expired(now) is True
    assert chunk.is_archivable(policy, now) is False
