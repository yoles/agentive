"""Unit tests — :class:`HandoffSummary` contract (Story 4.7 T1.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentive_backend.shared.contracts.handoff import HandoffSummary


def test_handoff_summary_defaults_to_empty_lists_on_all_four_fields() -> None:
    summary = HandoffSummary()

    assert summary.decisions == []
    assert summary.artifacts_refs == []
    assert summary.blockers == []
    assert summary.next_questions == []


def test_handoff_summary_accepts_the_four_declared_fields() -> None:
    summary = HandoffSummary(
        decisions=["a"], artifacts_refs=["b"], blockers=["c"], next_questions=["d"]
    )

    assert summary.decisions == ["a"]
    assert summary.artifacts_refs == ["b"]
    assert summary.blockers == ["c"]
    assert summary.next_questions == ["d"]


def test_handoff_summary_when_extra_field_given_should_reject() -> None:
    with pytest.raises(ValidationError):
        HandoffSummary.model_validate({"decisions": [], "unexpected": "field"})


def test_handoff_summary_when_field_is_not_a_list_should_reject() -> None:
    with pytest.raises(ValidationError):
        HandoffSummary.model_validate({"decisions": "not-a-list"})
