"""Unit tests — :func:`to_psycopg_dsn` (Story 4.2 review, finding #36).

The conversion was open-coded in five places as a bare
``url.replace("postgresql+psycopg://", "postgresql://", 1)``. That form is a
SILENT no-op on any unexpected scheme: a mistyped or future DSN sailed
through unconverted and only surfaced as a connection error somewhere far
from the mistake. These tests pin the single implementation and, above all,
the refusal.
"""

from __future__ import annotations

import pytest

from agentive_backend.shared.config import to_psycopg_dsn


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql+psycopg://app:pw@db:5432/agentive",
            "postgresql://app:pw@db:5432/agentive",
        ),
        (
            "postgresql+psycopg2://app:pw@db:5432/agentive",
            "postgresql://app:pw@db:5432/agentive",
        ),
        # Already converted — a legitimate no-op, not an error.
        ("postgresql://app:pw@db:5432/agentive", "postgresql://app:pw@db:5432/agentive"),
    ],
)
def test_strips_the_sqlalchemy_dialect_marker(url: str, expected: str) -> None:
    assert to_psycopg_dsn(url) == expected


def test_conversion_is_idempotent() -> None:
    once = to_psycopg_dsn("postgresql+psycopg://app:pw@db/agentive")
    assert to_psycopg_dsn(once) == once


@pytest.mark.parametrize(
    "url",
    [
        "mysql://app:pw@db/agentive",
        "postgres://app:pw@db/agentive",  # the short alias psycopg does accept, but we don't
        "sqlite:///:memory:",
        "not-a-url",
        "",
    ],
)
def test_unrecognised_scheme_raises_instead_of_passing_through(url: str) -> None:
    """The whole point of centralising: `.replace()` returned these untouched."""
    with pytest.raises(ValueError, match="not a recognised PostgreSQL DSN"):
        to_psycopg_dsn(url)


def test_only_the_scheme_is_rewritten() -> None:
    """A password or database name containing the marker text must survive."""
    url = "postgresql+psycopg://app:postgresql+psycopg%3A%2F%2F@db/agentive"
    assert to_psycopg_dsn(url) == "postgresql://app:postgresql+psycopg%3A%2F%2F@db/agentive"
