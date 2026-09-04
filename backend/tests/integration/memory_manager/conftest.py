"""Integration fixtures for ``features.memory_manager`` (Story 3.1).

Reuses the shared ``make_e2e_app`` (Postgres bootstrap + agents/tools/
playground/memory routers — Story 3.1 extended it with the memory router).
"""

from __future__ import annotations

from tests.integration.agent_registry.conftest import (  # noqa: F401
    E2E_AUTH_TOKEN,
    app_session_factory,
    audit_admin_session_factory,
    e2e_auth_headers,
    make_e2e_app,
    migrated_db,
    owner_session_factory,
    roles_provisioned,
    seed_session_factory,
)
