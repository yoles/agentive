"""End-to-end: ``connection_config`` is encrypted at rest (audit M-09 / Story 9.2).

Proves the two halves of :class:`agentive_backend.infra.db.types.EncryptedJSONB`:

* the raw JSONB column holds a Fernet envelope, not the plaintext credentials —
  a DB dump no longer leaks MCP secrets (audit 5.6b) ;
* the value read back through the ORM/API is transparently decrypted, so the
  round-trip is lossless.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app

_SECRET = f"top-secret-{uuid4().hex}"


def _stdio_config_with_secret() -> dict[str, Any]:
    """Mock-server config carrying a credential under an ``env`` secret key."""
    return {
        "command": "python",
        "args": ["-m", "tests.fixtures.mcp_mock_server"],
        "env": {"MCP_API_TOKEN": _SECRET},
    }


@pytest.mark.integration
async def test_connection_config_encrypted_at_rest(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "enc-at-rest",
                "transport": "stdio",
                "connection_config": _stdio_config_with_secret(),
            },
        )
        assert resp.status_code == 201, resp.text
        server_id = resp.json()["server_id"]

    # (1) At rest: the raw column is a ciphertext envelope, secret absent.
    async with seed_session_factory() as session:
        raw = await session.execute(
            text("SELECT connection_config::text FROM tool_servers WHERE id = :sid"),
            {"sid": server_id},
        )
        stored = raw.scalar_one()
        assert '"__enc__"' in stored and '"fernet"' in stored
        assert _SECRET not in stored, "credential leaked in clear in the JSONB column"
        assert "MCP_API_TOKEN" not in stored, "config keys leaked in clear at rest"

    # (2) Read back through the ORM: transparently decrypted, lossless.
    from agentive_backend.infra.db.models import ToolServer

    async with seed_session_factory() as session:
        server = await session.get(ToolServer, server_id)
        assert server is not None
        assert server.connection_config["env"]["MCP_API_TOKEN"] == _SECRET
        assert server.connection_config["command"] == "python"
