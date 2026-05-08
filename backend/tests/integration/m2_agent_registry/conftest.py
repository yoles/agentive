"""Integration fixtures for ``features.m2_agent_registry`` tests — Story 2.1.

Reuses Postgres + Alembic + role-provisioning machinery from the
``repositories`` package. ``clean_repository_tables`` is intentionally NOT
imported here — it's ``autouse=True`` in the repos conftest and would force
DB setup for every test in this directory, including the registry-only
lifespan test that doesn't need Postgres at all (same pattern as
``tests/integration/auth/conftest.py``).

Tests that DO need DB cleanup explicitly import the fixture and apply it
per-test (none currently — the e2e tests use unique names per test).
"""

from __future__ import annotations

# Reuse the repositories Postgres bootstrap (testcontainer + roles + alembic).
from tests.integration.repositories.conftest import (  # noqa: F401
    app_session_factory,
    audit_admin_session_factory,
    migrated_db,
    owner_session_factory,
    roles_provisioned,
    seed_session_factory,
)
