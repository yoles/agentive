"""Pytest fixtures — root conftest.

For Sprint 0, we keep fixtures minimal. ``testcontainers`` fixtures for
ephemeral Postgres are added in Story 1.5 (integration tests of repositories).
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Force async tests to run on asyncio (not trio)."""
    return "asyncio"
