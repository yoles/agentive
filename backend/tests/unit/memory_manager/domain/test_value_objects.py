"""Unit tests — Memory Manager domain value objects (Sprint 2 DDD, audit A-13).

Framework-free, zero I/O, microsecond-fast — the point of scoping the retention
rule as a domain concept before the feature exists is that it is testable
without a DB, a namespace row, or the app.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from agentive_backend.features.memory_manager.domain.value_objects import (
    SECONDS_MAX,
    DecayFunction,
    DecayPolicy,
    DomainValidationError,
    EmbeddingBackend,
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


# ─── EmbeddingBackend ─────────────────────────────────────────────


def test_embedding_backend_matches_the_three_wired_values() -> None:
    assert {b.value for b in EmbeddingBackend} == {"local", "cloud", "voyage"}


def test_embedding_backend_default_is_cloud_by_convention() -> None:
    # No `default_for_type`-style table for `EmbeddingBackend` (T1.2) — the
    # single global default is a plain enum member, mirrored by the DB
    # column's `server_default="cloud"` and `NamespaceRepo.create`'s own
    # `embedding_backend: str = "cloud"` default.
    assert EmbeddingBackend.CLOUD.value == "cloud"


def test_embedding_backend_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="banana"):
        EmbeddingBackend("banana")


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


def test_retention_policy_rejects_zero_seconds() -> None:
    """Zero is not "no lifecycle" (that is ``None``), it is "expire/archive
    immediately" — silently destructive on the very next archival pass
    (code review Story 3.3, IG1)."""
    with pytest.raises(DomainValidationError, match="default_ttl_seconds"):
        RetentionPolicy(default_ttl_seconds=0)
    with pytest.raises(DomainValidationError, match="archive_after_seconds"):
        RetentionPolicy(archive_after_seconds=0)


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


def test_expires_at_rejects_a_zero_override() -> None:
    with pytest.raises(DomainValidationError, match="ttl_override_seconds"):
        RetentionPolicy().expires_at(_CREATED, ttl_override_seconds=0)


# ─── archivable_at ────────────────────────────────────────────────


def test_archivable_at_none_when_policy_never_archives() -> None:
    assert RetentionPolicy().archivable_at(_CREATED) is None


def test_archivable_at_offsets_from_created() -> None:
    policy = RetentionPolicy(archive_after_seconds=86400)
    assert policy.archivable_at(_CREATED) == _CREATED + timedelta(days=1)


# ─── default_for_type (Story 3.2 AC1) ─────────────────────────────


def test_default_for_type_contextuelle_is_7_days() -> None:
    policy = RetentionPolicy.default_for_type(NamespaceType.CONTEXTUELLE)
    assert policy.default_ttl_seconds == 604_800
    assert policy.archive_after_seconds is None


def test_default_for_type_operationnelle_is_90_days() -> None:
    policy = RetentionPolicy.default_for_type(NamespaceType.OPERATIONNELLE)
    assert policy.default_ttl_seconds == 7_776_000


def test_default_for_type_metier_is_365_days() -> None:
    policy = RetentionPolicy.default_for_type(NamespaceType.METIER)
    assert policy.default_ttl_seconds == 31_536_000


def test_default_for_type_client_is_unlimited() -> None:
    policy = RetentionPolicy.default_for_type(NamespaceType.CLIENT)
    assert policy.default_ttl_seconds is None


def test_default_for_type_covers_every_namespace_type() -> None:
    """Guards the module-level exhaustiveness assertion's intent: every

    `NamespaceType` member must resolve without the internal `KeyError`
    that `default_for_type` used to leak as an opaque 500 (code review
    Story 3.2, P7).
    """
    for ns_type in NamespaceType:
        RetentionPolicy.default_for_type(ns_type)


# ─── DecayPolicy — construction / validation croisée (Story 3.4 T1.3) ──


def test_decay_policy_defaults_to_none_function() -> None:
    policy = DecayPolicy()
    assert policy.function is DecayFunction.NONE
    assert policy.half_life_seconds is None
    assert policy.horizon_seconds is None
    assert policy.threshold_seconds is None
    assert policy.factor is None


def test_decay_policy_exponential_requires_half_life() -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.EXPONENTIAL)


def test_decay_policy_exponential_rejects_sub_minimum_half_life() -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=0)


def test_decay_policy_accepts_the_ceiling_exactly() -> None:
    policy = DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=SECONDS_MAX)

    assert policy.half_life_seconds == SECONDS_MAX


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("above the ceiling", SECONDS_MAX + 1),
        # The three shapes a hand-seeded JSONB can take that a one-sided
        # `< SECONDS_MIN` check let through (code review Story 3.4, P5).
        ("infinity", math.inf),
        ("not a number", math.nan),
        ("int past float range", 10**400),
    ],
)
@pytest.mark.parametrize(
    ("function", "field"),
    [
        (DecayFunction.EXPONENTIAL, "half_life_seconds"),
        (DecayFunction.LINEAR, "horizon_seconds"),
    ],
)
def test_decay_policy_rejects_out_of_range_durations(
    label: str, value: float, function: DecayFunction, field: str
) -> None:
    """Each of these used to be accepted, with `decay_policy_valid` still

    `True`. `inf` silently neutralised the decay (`age / inf == 0.0`, factor
    `1.0`) and a huge int raised `OverflowError` from inside `decay_factor`,
    i.e. a 500 on a search rather than a degraded ranking.
    """
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=function, **{field: value})


def test_decay_policy_rejects_an_out_of_range_duration_from_mapping() -> None:
    """The gate that matters: this is the path a legacy DB row travels."""
    with pytest.raises(DomainValidationError):
        DecayPolicy.from_mapping({"function": "exponential", "half_life_seconds": math.inf})


@pytest.mark.parametrize(
    ("field", "value"),
    [("horizon_seconds", 60), ("threshold_seconds", 60), ("factor", 0.5)],
)
def test_decay_policy_exponential_rejects_foreign_parameters(field: str, value: object) -> None:
    """A parameter incoherent with the function is REJECTED, never silently

    ignored — the exact trap settled by code review Story 3.2 BS1.
    """
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=60, **{field: value})  # type: ignore[arg-type]


def test_decay_policy_linear_requires_horizon() -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.LINEAR)


@pytest.mark.parametrize(
    ("field", "value"),
    [("half_life_seconds", 60), ("threshold_seconds", 60), ("factor", 0.5)],
)
def test_decay_policy_linear_rejects_foreign_parameters(field: str, value: object) -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=60, **{field: value})  # type: ignore[arg-type]


def test_decay_policy_step_requires_threshold_and_factor() -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.STEP, threshold_seconds=60)
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.STEP, factor=0.5)


@pytest.mark.parametrize("factor", [-0.01, 1.01])
def test_decay_policy_step_rejects_factor_outside_unit_interval(factor: float) -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(function=DecayFunction.STEP, threshold_seconds=60, factor=factor)


@pytest.mark.parametrize(("field", "value"), [("half_life_seconds", 60), ("horizon_seconds", 60)])
def test_decay_policy_step_rejects_foreign_parameters(field: str, value: int) -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(
            function=DecayFunction.STEP,
            threshold_seconds=60,
            factor=0.5,
            **{field: value},  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("half_life_seconds", 60),
        ("horizon_seconds", 60),
        ("threshold_seconds", 60),
        ("factor", 0.5),
    ],
)
def test_decay_policy_none_rejects_every_parameter(field: str, value: object) -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy(**{field: value})  # type: ignore[arg-type]


# ─── DecayPolicy — from_mapping / to_mapping (T1.4, T1.5) ─────────


def test_decay_policy_from_mapping_none_returns_default() -> None:
    assert DecayPolicy.from_mapping(None) == DecayPolicy()


def test_decay_policy_from_mapping_empty_returns_default() -> None:
    assert DecayPolicy.from_mapping({}) == DecayPolicy()


def test_decay_policy_from_mapping_unknown_function_raises() -> None:
    with pytest.raises(DomainValidationError):
        DecayPolicy.from_mapping({"function": "sigmoid"})


def test_decay_policy_none_round_trips_to_empty_mapping() -> None:
    assert DecayPolicy().to_mapping() == {}
    assert DecayPolicy.from_mapping(DecayPolicy().to_mapping()) == DecayPolicy()


@pytest.mark.parametrize(
    "policy",
    [
        DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=2_592_000),
        DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=86_400),
        DecayPolicy(function=DecayFunction.STEP, threshold_seconds=3_600, factor=0.25),
    ],
)
def test_decay_policy_round_trips_through_mapping(policy: DecayPolicy) -> None:
    assert DecayPolicy.from_mapping(policy.to_mapping()) == policy


def test_decay_policy_to_mapping_emits_only_its_own_parameters() -> None:
    mapping = DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=60).to_mapping()
    assert mapping == {"function": "exponential", "half_life_seconds": 60}


# ─── DecayPolicy.decay_factor — valeurs exactes (AC3, T1.6) ───────


def test_decay_factor_none_is_one_everywhere() -> None:
    policy = DecayPolicy()
    for age_days in (0, 1, 3_650):
        now = _CREATED + timedelta(days=age_days)
        assert policy.decay_factor(_CREATED, now) == 1.0


def test_decay_factor_exponential_is_exactly_half_at_half_life() -> None:
    policy = DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=2_592_000)
    now = _CREATED + timedelta(seconds=2_592_000)
    assert policy.decay_factor(_CREATED, now) == pytest.approx(0.5)


def test_decay_factor_exponential_is_one_quarter_at_two_half_lives() -> None:
    policy = DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=100)
    now = _CREATED + timedelta(seconds=200)
    assert policy.decay_factor(_CREATED, now) == pytest.approx(0.25)


def test_decay_factor_linear_is_exactly_zero_at_horizon() -> None:
    policy = DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=86_400)
    now = _CREATED + timedelta(seconds=86_400)
    assert policy.decay_factor(_CREATED, now) == pytest.approx(0.0)


def test_decay_factor_linear_is_half_at_mid_horizon() -> None:
    policy = DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=100)
    now = _CREATED + timedelta(seconds=50)
    assert policy.decay_factor(_CREATED, now) == pytest.approx(0.5)


def test_decay_factor_linear_stays_at_zero_past_the_horizon() -> None:
    policy = DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=100)
    now = _CREATED + timedelta(seconds=10_000)
    assert policy.decay_factor(_CREATED, now) == 0.0


def test_decay_factor_step_switches_exactly_past_the_threshold() -> None:
    policy = DecayPolicy(function=DecayFunction.STEP, threshold_seconds=100, factor=0.2)
    assert policy.decay_factor(_CREATED, _CREATED + timedelta(seconds=100)) == pytest.approx(1.0)
    assert policy.decay_factor(_CREATED, _CREATED + timedelta(seconds=101)) == pytest.approx(0.2)


@pytest.mark.parametrize(
    "policy",
    [
        DecayPolicy(),
        DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=60),
        DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=60),
        DecayPolicy(function=DecayFunction.STEP, threshold_seconds=60, factor=0.1),
    ],
)
def test_decay_factor_is_one_for_a_future_created_at(policy: DecayPolicy) -> None:
    """AC1 — clock drift clamps the age to 0, never a factor above 1."""
    now = _CREATED - timedelta(days=1)
    assert policy.decay_factor(_CREATED, now) == 1.0


@pytest.mark.parametrize(
    "policy",
    [
        DecayPolicy(),
        DecayPolicy(function=DecayFunction.EXPONENTIAL, half_life_seconds=60),
        DecayPolicy(function=DecayFunction.LINEAR, horizon_seconds=60),
        DecayPolicy(function=DecayFunction.STEP, threshold_seconds=60, factor=0.1),
    ],
)
def test_decay_factor_stays_within_unit_interval(policy: DecayPolicy) -> None:
    for age_seconds in (0, 1, 59, 60, 61, 10_000, 10_000_000):
        factor = policy.decay_factor(_CREATED, _CREATED + timedelta(seconds=age_seconds))
        assert 0.0 <= factor <= 1.0
