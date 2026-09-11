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
