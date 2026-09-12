"""Unit tests — the `AGENTIVE_DRY_RUN_*` deployment knobs (Story 4.4 T1.1).

Sibling of `test_config_routing.py`, and written for the same reason: a
malformed value on an OPTIONAL knob must never take the process down or
turn a safety net into permanent noise. Every case below is a review
finding (P1/P2), not a hypothetical.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentive_backend.features.workflow_engine.recovery import (
    _DEFAULT_RETRY_BASE_DELAY_S,
    _DEFAULT_RETRY_MAX_DELAY_S,
    _ROUTING_ESCALATION_TIMEOUT_S_DEFAULT,
)
from agentive_backend.shared.config import Settings


def test_dry_run_knobs_keep_their_documented_defaults() -> None:
    """The four defaults `.env.example` advertises."""
    settings = Settings()
    assert settings.dry_run_history_limit == 20
    assert settings.dry_run_fallback_input_tokens == 500
    assert settings.dry_run_fallback_output_tokens == 500
    assert settings.dry_run_budget_cap_usd is None


@pytest.mark.parametrize("raw", ["", "   "])
def test_budget_cap_when_set_but_blank_should_disable_the_check(raw: str) -> None:
    """`.env.example` documents "vide/absent = vérification désactivée", and
    `AGENTIVE_DRY_RUN_BUDGET_CAP_USD=` is the natural way to express it in a
    copied `.env`. Before the fix, `""` was neither a valid `Decimal` nor
    `None`, so `Settings` failed to instantiate at import time and the WHOLE
    backend refused to boot over an optional knob."""
    assert Settings(AGENTIVE_DRY_RUN_BUDGET_CAP_USD=raw).dry_run_budget_cap_usd is None  # type: ignore[call-arg]


def test_budget_cap_when_absent_should_disable_the_check() -> None:
    assert Settings().dry_run_budget_cap_usd is None


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf"])
def test_budget_cap_when_not_finite_should_be_rejected(raw: str) -> None:
    """`ge=` does not reject NaN. Worse than the float case that motivated
    the routing guard: `Decimal('1') > Decimal('NaN')` RAISES
    `InvalidOperation` instead of returning False, so a NaN cap booted
    cleanly and then 500'd every priced Dry Run."""
    with pytest.raises(ValidationError):
        Settings(AGENTIVE_DRY_RUN_BUDGET_CAP_USD=raw)  # type: ignore[call-arg]


@pytest.mark.parametrize("raw", ["-1", "-0.000001"])
def test_budget_cap_when_negative_should_be_rejected(raw: str) -> None:
    """A negative cap sits below every possible estimate, so
    `budget_cap_exceeded` fires on every workflow forever — the risk becomes
    permanent noise and stops carrying any signal."""
    with pytest.raises(ValidationError):
        Settings(AGENTIVE_DRY_RUN_BUDGET_CAP_USD=raw)  # type: ignore[call-arg]


@pytest.mark.parametrize("raw", ["0", "50.0"])
def test_budget_cap_when_valid_should_be_kept_as_decimal(raw: str) -> None:
    """`0` is a legitimate cap (flag every non-free workflow), not a
    disabled one — that distinction is `None`'s job."""
    cap = Settings(AGENTIVE_DRY_RUN_BUDGET_CAP_USD=raw).dry_run_budget_cap_usd  # type: ignore[call-arg]
    assert cap == Decimal(raw)


@pytest.mark.parametrize(
    "alias",
    ["AGENTIVE_DRY_RUN_FALLBACK_INPUT_TOKENS", "AGENTIVE_DRY_RUN_FALLBACK_OUTPUT_TOKENS"],
)
def test_fallback_tokens_when_absurdly_large_should_be_rejected(alias: str) -> None:
    """These feed `Decimal(tokens) / 1_000_000 * price` then `.quantize()`.
    Past ~1e22 that overflows the decimal context's 28-digit precision and
    raises `InvalidOperation` — a 500 on every priced Dry Run, caused by an
    env var. The `le=` bound keeps the arithmetic in range."""
    with pytest.raises(ValidationError):
        Settings(**{alias: 10**30})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "alias",
    ["AGENTIVE_DRY_RUN_FALLBACK_INPUT_TOKENS", "AGENTIVE_DRY_RUN_FALLBACK_OUTPUT_TOKENS"],
)
def test_fallback_tokens_when_zero_or_negative_should_be_rejected(alias: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{alias: 0})  # type: ignore[arg-type]


# ─── Revue du 2026-09-12 (P-16) — Story 4.7 : les défauts documentés ───────


def test_handoff_summary_knobs_keep_their_documented_defaults() -> None:
    """Only the TIMEOUT had a parity assertion, and only incidentally (T5.6
    needed it for the staleness derivation). `claude-haiku-4-5` and `512` are
    what `.env.example` advertises and what every deployment inherits by
    omission: a drift between the two is invisible until a run bills against
    the wrong model."""
    settings = Settings()
    assert settings.workflow_handoff_summary_model == "claude-haiku-4-5"
    assert settings.workflow_handoff_summary_max_tokens == 512
    assert settings.workflow_handoff_summary_timeout_s == 20.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_MODEL": ""},
        {"AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_MAX_TOKENS": 0},
        {"AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_TIMEOUT_S": 0.0},
        {"AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_TIMEOUT_S": float("inf")},
        # I-02 — the ceilings that were missing. `routing_escalation_*`, the
        # twin knobs, have carried both since Story 4.6 for exactly this
        # reason: `recovery.derive_stale_threshold_s` multiplies the timeout,
        # so unbounded it moved the staleness window (at 600 s: past three
        # hours, a crashed run sitting unexamined that long) and the
        # worst-case `pause`/`cancel` latency, by one env var.
        {"AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_TIMEOUT_S": 600.0},
        {"AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_TIMEOUT_S": 60.0},
        {"AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_MAX_TOKENS": 99_999},
    ],
)
def test_handoff_summary_knobs_refuse_a_nonsense_value(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)  # type: ignore[arg-type]


def test_the_handoff_summary_kill_switch_defaults_to_on() -> None:
    """I-04 — AC2's default stands: this switch only makes it REVERSIBLE
    without editing every consuming template under incident."""
    assert Settings().workflow_handoff_summary_enabled is True
    assert (
        Settings(AGENTIVE_WORKFLOW_HANDOFF_SUMMARY_ENABLED=False).workflow_handoff_summary_enabled
        is False
    )  # type: ignore[call-arg]


def test_the_handoff_ceiling_preserves_the_thirty_minute_promise() -> None:
    """I-02 — the property the ceiling is DERIVED from, not decoration.

    `derive_stale_threshold_s` adds this term as `h * 2 * 2.5` = `5h` on top
    of 1517.5 s at otherwise-default settings, and `DEFAULT_STALE_THRESHOLD_S`
    commits in writing to staying under 30 min. `1517.5 + 5h < 1800` gives
    `h < 56.5` — which is why the ceiling is 45.0 and not
    `routing_escalation_timeout_s`'s 60.0: at 60.0 the window reaches 30.3
    min and breaks that promise through a knob nobody would think to check.

    (The 30-minute promise is a property of the DEFAULTS of the OTHER knobs —
    `test_the_escalation_timeout_is_bounded_by_config` already documents that
    the worst legal combination of all four is ~57 min, and why that is
    forced rather than chosen.)
    """
    from agentive_backend.features.workflow_engine.recovery import derive_stale_threshold_s

    at_ceiling = derive_stale_threshold_s(
        base_delay_s=_DEFAULT_RETRY_BASE_DELAY_S,
        max_delay_s=_DEFAULT_RETRY_MAX_DELAY_S,
        escalation_timeout_s=_ROUTING_ESCALATION_TIMEOUT_S_DEFAULT,
        handoff_summary_timeout_s=45.0,
    )
    assert at_ceiling < 30 * 60
