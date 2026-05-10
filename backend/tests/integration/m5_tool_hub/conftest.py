"""Integration fixtures for ``features.m5_tool_hub`` tests — Story 2.5.

Reuses the m2 conftest helpers (Postgres bootstrap + ``make_e2e_app``
which now includes both m2 and m5 routers — Story 2.5 T10.1).
"""

from __future__ import annotations

from tests.integration.m2_agent_registry.conftest import (  # noqa: F401
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
