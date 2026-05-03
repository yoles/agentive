"""Integration fixtures for auth tests — Story 1.7.

Provides database fixtures for tests that need to verify outbox_events rows
(audit event tests). Tests that don't need a DB (middleware auth, rotation)
use minimal in-memory FastAPI apps and don't require these fixtures.

Note: ``clean_repository_tables`` is intentionally NOT imported here because
it is ``autouse=True`` in the repositories conftest — importing it would
trigger DB setup for ALL tests in this package, including those that don't
need a database (e.g. ``test_rotate_token.py``, ``test_middleware_auth.py``).
"""

from __future__ import annotations

# Import only what's needed — NOT clean_repository_tables (autouse=True would
# force DB setup on every test in this directory).
from tests.integration.repositories.conftest import (  # noqa: F401
    app_session_factory,
    audit_admin_session_factory,
    migrated_db,
    owner_session_factory,
    roles_provisioned,
    seed_session_factory,
)
