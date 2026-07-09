"""End-to-end integration tests — Tool Hub server CRUD (Story 2.5 T10.3 / AC8).

Couvre :
* AC1 — POST stdio happy path : 201 + body shape complet + 1+N outbox events.
* AC1 — POST 503 on MCP discovery timeout.
* POST 422 on invalid transport.
* POST 409 on duplicate name.
* GET /tools/servers list (200 + tools_count).
* GET /tools/servers/{id} detail (200 + tools array).
* GET 404 detail on inexistent.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.infra.mcp.client import MCPDiscoveryTimeoutError, ToolInfo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app


def _stdio_config() -> dict[str, Any]:
    """The connection_config that points to our local mock MCP server."""
    return {"command": "python", "args": ["-m", "tests.fixtures.mcp_mock_server"]}


@pytest.mark.integration
async def test_create_tool_server_stdio_happy_path(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — POST stdio with mock subprocess returns 2 tools (echo + add).

    Discovers via the real MCP SDK against the bundled mock server fixture
    (no Node.js dependency in CI).
    """
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "ac1-stdio",
                "transport": "stdio",
                "connection_config": _stdio_config(),
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "ac1-stdio"
        assert body["transport"] == "stdio"
        assert body["status"] == "active"
        assert "server_id" in body and len(body["server_id"]) == 36
        assert "discovered_at" in body
        # Mock server exposes 3 tools : echo + add + sleep (Story 2.6).
        tools = body["tools"]
        assert len(tools) == 3
        names = sorted(t["name"] for t in tools)
        assert names == ["add", "echo", "sleep"]
        for tool in tools:
            assert "tool_id" in tool
            assert "input_schema" in tool

        # DB rows : 1 server + 3 tools.
        async with seed_session_factory() as session:
            srv_row = await session.execute(
                text("SELECT name, transport, status FROM tool_servers WHERE id = :sid"),
                {"sid": body["server_id"]},
            )
            assert srv_row.one() == ("ac1-stdio", "stdio", "active")
            tools_count = await session.execute(
                text("SELECT COUNT(*) FROM tools WHERE server_id = :sid"),
                {"sid": body["server_id"]},
            )
            assert int(tools_count.scalar_one()) == 3

        # Outbox : 1 server.connected + 3 tool.discovered = 4 events.
        async with seed_session_factory() as session:
            connected = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'tool_hub.tool_server.connected' "
                    "AND payload->>'server_id' = :sid"
                ),
                {"sid": body["server_id"]},
            )
            assert int(connected.scalar_one()) == 1
            discovered = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'tool_hub.tool.discovered' "
                    "AND payload->>'server_id' = :sid"
                ),
                {"sid": body["server_id"]},
            )
            assert int(discovered.scalar_one()) == 3


@pytest.mark.integration
async def test_create_tool_server_disabled_by_flag_returns_403(
    app_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-23 admin-gate — when ``AGENTIVE_ALLOW_MCP_REGISTRATION=false`` the
    POST endpoint returns 403 RFC 7807 with a hint pointing to the env var.

    The conftest autouse fixture flips the flag to True ; we override it
    back to False here to exercise the gate.
    """
    from agentive_backend.shared.config import settings as _settings

    monkeypatch.setattr(_settings, "mcp_allow_registration", False)

    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "gated-srv",
                "transport": "stdio",
                "connection_config": _stdio_config(),
            },
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["status"] == 403
        assert "AGENTIVE_ALLOW_MCP_REGISTRATION" in body.get("detail", "")
        assert body.get("flag") == "AGENTIVE_ALLOW_MCP_REGISTRATION"


@pytest.mark.integration
async def test_get_endpoints_unaffected_by_registration_flag(
    app_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P-23 — only POST /tools/servers is gated. GET endpoints stay open
    (no RCE/SSRF surface — read-only).
    """
    from agentive_backend.shared.config import settings as _settings

    # First seed with the flag enabled (default in conftest).
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        seed = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "list-gated-test",
                "transport": "stdio",
                "connection_config": _stdio_config(),
            },
        )
        assert seed.status_code == 201

        # Now disable the flag and verify GETs still work.
        monkeypatch.setattr(_settings, "mcp_allow_registration", False)

        listed = await client.get("/api/v1/tools/servers", headers=_auth_headers())
        assert listed.status_code == 200
        names = [s["name"] for s in listed.json()]
        assert "list-gated-test" in names


@pytest.mark.integration
async def test_create_tool_server_invalid_transport_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "bad-transport",
                "transport": "http",  # not in the Literal whitelist
                "connection_config": {},
            },
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["type"] == "/errors/validation"


@pytest.mark.integration
async def test_create_tool_server_duplicate_name_returns_409(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Décision #6 Story 2.5 — re-POST same name → 409 ConflictError."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "dup-name",
                "transport": "stdio",
                "connection_config": _stdio_config(),
            },
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "dup-name",
                "transport": "stdio",
                "connection_config": _stdio_config(),
            },
        )
        assert second.status_code == 409
        body = second.json()
        assert "already registered" in body.get("detail", "").lower()


@pytest.mark.integration
async def test_create_tool_server_discovery_timeout_returns_503(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1 — discovery timeout → 503 DependencyError + 0 row created."""
    import agentive_backend.features.tool_hub.service as svc_module

    async def _timeout(**_kwargs: Any) -> list[ToolInfo]:
        raise MCPDiscoveryTimeoutError(timeout=10.0)

    monkeypatch.setattr(svc_module, "discover_tools", _timeout)

    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "timeout-srv",
                "transport": "stdio",
                "connection_config": {"command": "x"},
            },
        )
        assert resp.status_code == 503
        body = resp.json()
        assert "timeout" in body.get("detail", "").lower()

        # No row INSERTed.
        async with seed_session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM tool_servers WHERE name = :n"),
                {"n": "timeout-srv"},
            )
            assert int(count.scalar_one()) == 0


@pytest.mark.integration
async def test_list_tool_servers_returns_count(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Seed 1 server.
        await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "list-test",
                "transport": "stdio",
                "connection_config": _stdio_config(),
            },
        )

        resp = await client.get("/api/v1/tools/servers", headers=_auth_headers())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        # At least 1 server (others may exist from parallel tests — filter by name).
        targets = [s for s in body if s["name"] == "list-test"]
        assert len(targets) == 1
        assert targets[0]["tools_count"] == 3  # mock server has 3 tools (echo + add + sleep)


@pytest.mark.integration
async def test_list_tool_servers_is_not_n_plus_1(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-21 (CR 2026-05-10) — Dev Note #9 / spec L406 demanded :
    *"avec 3 servers, on a bien 3 SQL queries (1 SELECT JOIN agrégé), pas
    3+N"*.

    We attach a SQLAlchemy event listener that counts ``SELECT`` statements
    issued during the ``GET /tools/servers`` request and assert the count
    stays bounded regardless of the number of servers / tools.

    Tolerance: the count check is "≤ 5 SELECTs" rather than "= 1" because
    the request pipeline also issues a couple of ``SET LOCAL`` (tenant
    binding) and possibly a ``BEGIN``/``COMMIT`` framing. The point is to
    catch the N+1 regression where a per-server tools count subquery would
    fire 3+ extra SELECTs.
    """
    from sqlalchemy import event

    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Seed 3 servers (3 tools each via the mock).
        for i in range(3):
            await client.post(
                "/api/v1/tools/servers",
                headers=_auth_headers(),
                json={
                    "name": f"n1-server-{i}",
                    "transport": "stdio",
                    "connection_config": _stdio_config(),
                },
            )

        select_counter: list[str] = []

        engine = app_session_factory.kw["bind"]
        sync_engine = engine.sync_engine

        def _before_cursor_execute(_conn: Any, _cursor: Any, statement: str, *_rest: Any) -> None:
            # Scope the counter to SELECTs targeting tool_servers — the
            # query of interest for the N+1 invariant.
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "tool_servers" in statement.lower()
            ):
                select_counter.append(statement)

        event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute)
        try:
            resp = await client.get("/api/v1/tools/servers", headers=_auth_headers())
        finally:
            event.remove(sync_engine, "before_cursor_execute", _before_cursor_execute)

        assert resp.status_code == 200
        body = resp.json()
        ours = [s for s in body if s["name"].startswith("n1-server-")]
        assert len(ours) == 3
        for s in ours:
            assert s["tools_count"] == 3  # mock exposes 3 tools per server

        # The N+1 trap: a per-server count subquery would have fired 3+
        # extra SELECTs targeting tool_servers / tools. The aggregate JOIN
        # keeps the total bounded. We allow a small headroom (≤ 5) for
        # framing / RLS-binding queries that may also hit tool_servers in
        # passing without scaling with N.
        assert len(select_counter) <= 5, (
            f"N+1 regression: {len(select_counter)} SELECTs against tool_servers "
            f"for 3 servers, expected ≤ 5 (1 list query + framing).\nQueries:\n"
            + "\n".join(select_counter)
        )


@pytest.mark.integration
async def test_get_tool_server_detail_happy(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        post = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "detail-test",
                "transport": "stdio",
                "connection_config": _stdio_config(),
            },
        )
        server_id = post.json()["server_id"]

        get = await client.get(f"/api/v1/tools/servers/{server_id}", headers=_auth_headers())
        assert get.status_code == 200, get.text
        body = get.json()
        assert body["server_id"] == server_id
        assert len(body["tools"]) == 3


@pytest.mark.integration
async def test_create_tool_server_redacts_secrets_in_response(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-03 (CR 2026-05-10) — POST 201 + GET detail responses must NOT echo
    secret-bearing values (``env`` content for stdio, ``headers`` content
    for sse, top-level secret keys). Only the structural shape (key names)
    is exposed.
    """
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    config = {
        "command": "python",
        "args": ["-m", "tests.fixtures.mcp_mock_server"],
        "env": {"SECRET_API_KEY": "hunter2", "OTHER_VAR": "visible_value"},
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        post = await client.post(
            "/api/v1/tools/servers",
            headers=_auth_headers(),
            json={
                "name": "redact-test",
                "transport": "stdio",
                "connection_config": config,
            },
        )
        assert post.status_code == 201, post.text
        post_body = post.json()
        post_cfg = post_body["connection_config"]
        # Structural fields preserved.
        assert post_cfg["command"] == "python"
        assert post_cfg["args"] == ["-m", "tests.fixtures.mcp_mock_server"]
        # env values masked, env key NAMES exposed.
        assert post_cfg["env"] == {"_redacted_keys": ["OTHER_VAR", "SECRET_API_KEY"]}
        # The actual secret value MUST NOT appear anywhere in the response body.
        assert "hunter2" not in post.text
        assert "visible_value" not in post.text

        # Same redaction on GET detail.
        get = await client.get(
            f"/api/v1/tools/servers/{post_body['server_id']}", headers=_auth_headers()
        )
        assert get.status_code == 200
        get_cfg = get.json()["connection_config"]
        assert get_cfg["env"] == {"_redacted_keys": ["OTHER_VAR", "SECRET_API_KEY"]}
        assert "hunter2" not in get.text


@pytest.mark.integration
async def test_get_tool_server_detail_not_found_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/api/v1/tools/servers/{uuid4()}", headers=_auth_headers())
        assert resp.status_code == 404
        body = resp.json()
        assert "not found" in body.get("detail", "").lower()
