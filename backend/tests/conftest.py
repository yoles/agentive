"""Pytest fixtures — root conftest.

Provides a ``postgres_container`` fixture using ``testcontainers`` so
integration tests can stand up an ephemeral Postgres + pgvector instance
without the developer having to pre-provision anything on the host.

Sprint 0 ships this scaffolding; Story 1.5 will add repository-level
integration tests that consume it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Force async tests to run on asyncio (not trio)."""
    return "asyncio"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[object]:
    """Ephemeral Postgres 17 + pgvector container for integration tests.

    Lazy-imports ``testcontainers`` so unit tests that don't need Postgres
    (e.g. the health-endpoint smoke tests) don't pay the dependency cost.

    Skips automatically if Docker is not available — useful in constrained
    CI environments where only pure-Python tests should run.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover
        pytest.skip(f"testcontainers not available: {exc}")

    image = "pgvector/pgvector:pg17"
    container = PostgresContainer(
        image=image,
        username="agentive_owner",
        password="test_owner",
        dbname="agentive_test",
        driver="psycopg",
    )
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def database_url(postgres_container: object) -> str:
    """Return the SQLAlchemy URL for the ephemeral Postgres.

    Depends on :func:`postgres_container` so the container is alive for the
    duration of the session.
    """
    # testcontainers PostgresContainer exposes `get_connection_url()`.
    return postgres_container.get_connection_url()  # type: ignore[attr-defined]
