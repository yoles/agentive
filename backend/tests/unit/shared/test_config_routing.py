"""Unit tests — the `AGENTIVE_ROUTING_*` deployment knobs (Story 4.3 T1.1).

Pydantic's `ge`/`le`/`gt` do NOT reject NaN: every comparison against NaN is
false, so no bound ever trips and the value sails through validation. Without
`allow_inf_nan=False`, a `NaN` threshold boots cleanly and then makes
`match.confidence >= threshold` false forever — every routing decision
escalating to the LLM, silently, with no error anywhere to trace it back to.
The catalog's own floats were already guarded this way (T3.3); these are the
same guard on the settings side.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentive_backend.shared.config import Settings


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf"])
def test_confidence_threshold_when_not_finite_should_be_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        Settings(AGENTIVE_ROUTING_CONFIDENCE_THRESHOLD=raw)  # type: ignore[call-arg]


@pytest.mark.parametrize("raw", ["nan", "inf"])
def test_escalation_timeout_when_not_finite_should_be_rejected(raw: str) -> None:
    with pytest.raises(ValidationError):
        Settings(AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S=raw)  # type: ignore[call-arg]


def test_routing_knobs_keep_their_documented_defaults() -> None:
    settings = Settings()
    assert settings.routing_confidence_threshold == 0.8
    assert settings.routing_escalation_model == "claude-haiku-4-5"
    assert settings.routing_escalation_timeout_s == 15.0
    assert settings.routing_escalation_max_tokens == 256


@pytest.mark.parametrize("raw", [-0.1, 1.1])
def test_confidence_threshold_outside_unit_interval_should_be_rejected(raw: float) -> None:
    with pytest.raises(ValidationError):
        Settings(AGENTIVE_ROUTING_CONFIDENCE_THRESHOLD=raw)  # type: ignore[call-arg]


@pytest.mark.parametrize("raw", [0.0, 1.0])
def test_confidence_threshold_at_both_bounds_should_be_accepted(raw: float) -> None:
    """Both bounds are legitimate and deliberately exploitable — that is the
    whole reason AC1 diverged from the epic's literal `>` to `>=`."""
    assert Settings(AGENTIVE_ROUTING_CONFIDENCE_THRESHOLD=raw).routing_confidence_threshold == raw  # type: ignore[call-arg]
