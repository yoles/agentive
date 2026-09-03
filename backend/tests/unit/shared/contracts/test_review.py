"""Unit tests : ReviewComment/ControllerReview (Story 2.8 T1.4, AC1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentive_backend.shared.contracts.review import ControllerReview, ReviewComment


def test_review_comment_valid_construction() -> None:
    comment = ReviewComment(
        location="backend/src/foo.py:42",
        severity="blocking",
        message="Nom de variable ambigu.",
        suggested_fix="Renommer en `elapsed_ms`.",
    )
    assert comment.location == "backend/src/foo.py:42"
    assert comment.severity == "blocking"
    assert comment.suggested_fix == "Renommer en `elapsed_ms`."


def test_review_comment_suggested_fix_defaults_to_none() -> None:
    comment = ReviewComment(
        location="field.name", severity="question", message="Pourquoi ce choix ?"
    )
    assert comment.suggested_fix is None


def test_review_comment_invalid_severity_raises() -> None:
    with pytest.raises(ValidationError):
        ReviewComment(location="x", severity="critical", message="msg")  # type: ignore[arg-type]


def test_review_comment_forbids_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ReviewComment(  # type: ignore[call-arg]
            location="x", severity="blocking", message="msg", unexpected_field="oops"
        )


def test_controller_review_valid_construction() -> None:
    review = ControllerReview(
        comments=[
            ReviewComment(location="x", severity="blocking", message="msg"),
        ],
        verdict="needs_fix",
    )
    assert review.verdict == "needs_fix"
    assert len(review.comments) == 1


def test_controller_review_invalid_verdict_raises() -> None:
    with pytest.raises(ValidationError):
        ControllerReview(comments=[], verdict="rejected")  # type: ignore[arg-type]


def test_controller_review_forbids_unknown_field() -> None:
    with pytest.raises(ValidationError):
        ControllerReview(comments=[], verdict="pass", extra_field=1)  # type: ignore[call-arg]
