"""Unit tests — Memory Manager domain value objects (Sprint 2 DDD, audit A-13).

Framework-free, zero I/O, microsecond-fast — the point of scoping the retention
rule as a domain concept before the feature exists is that it is testable
without a DB, a namespace row, or the app.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agentive_backend.features.memory_manager.domain.value_objects import (
    DomainValidationError,
    NamespaceType,
    RetentionPolicy,
)

_CREATED = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


# ─── NamespaceType ────────────────────────────────────────────────


def test_namespace_type_matches_the_db_check_values() -> None:
    assert {t.value for t in NamespaceType} == {
        "client",
        "metier",
        "operationnelle",
        "contextuelle",
    }


def test_namespace_type_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="banana"):
        NamespaceType("banana")


# ─── RetentionPolicy — construction & round-trip ──────────────────


def test_retention_policy_defaults_are_none() -> None:
    policy = RetentionPolicy()
    assert policy.default_ttl_seconds is None
    assert policy.archive_after_seconds is None


def test_retention_policy_rejects_negative_seconds() -> None:
    with pytest.raises(DomainValidationError, match="default_ttl_seconds"):
        RetentionPolicy(default_ttl_seconds=-1)
    with pytest.raises(DomainValidationError, match="archive_after_seconds"):
        RetentionPolicy(archive_after_seconds=-5)


def test_retention_policy_round_trip_emits_only_present_keys() -> None:
    raw = {"default_ttl_seconds": 3600, "archive_after_seconds": 86400}
    assert RetentionPolicy.from_mapping(raw).to_mapping() == raw


def test_retention_policy_empty_round_trip() -> None:
    assert RetentionPolicy.from_mapping({}).to_mapping() == {}
    assert RetentionPolicy.from_mapping(None).to_mapping() == {}


def test_retention_policy_partial_round_trip() -> None:
    policy = RetentionPolicy.from_mapping({"default_ttl_seconds": 60})
    assert policy.to_mapping() == {"default_ttl_seconds": 60}
    assert "archive_after_seconds" not in policy.to_mapping()


# ─── expires_at ───────────────────────────────────────────────────


def test_expires_at_none_when_no_ttl() -> None:
    assert RetentionPolicy().expires_at(_CREATED) is None


def test_expires_at_uses_policy_default() -> None:
    policy = RetentionPolicy(default_ttl_seconds=3600)
    assert policy.expires_at(_CREATED) == _CREATED + timedelta(seconds=3600)


def test_expires_at_override_wins_over_default() -> None:
    policy = RetentionPolicy(default_ttl_seconds=3600)
    assert policy.expires_at(_CREATED, ttl_override_seconds=60) == _CREATED + timedelta(seconds=60)


def test_expires_at_override_on_policy_without_default() -> None:
    assert RetentionPolicy().expires_at(_CREATED, ttl_override_seconds=10) == _CREATED + timedelta(
        seconds=10
    )


def test_expires_at_rejects_negative_override() -> None:
    with pytest.raises(DomainValidationError, match="ttl_override_seconds"):
        RetentionPolicy().expires_at(_CREATED, ttl_override_seconds=-1)


# ─── archivable_at ────────────────────────────────────────────────


def test_archivable_at_none_when_policy_never_archives() -> None:
    assert RetentionPolicy().archivable_at(_CREATED) is None


def test_archivable_at_offsets_from_created() -> None:
    policy = RetentionPolicy(archive_after_seconds=86400)
    assert policy.archivable_at(_CREATED) == _CREATED + timedelta(days=1)
