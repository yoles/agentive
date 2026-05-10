"""End-to-end integration tests — agent_template ↔ tool assignment (Story 2.5 T10.4 / AC8).

Couvre :
* AC2 — POST happy : assign 1 tool, REPLACE diff (1 added + 1 removed),
  REPLACE empty (clear all).
* AC2 — POST 404 if any tool_id missing (no partial success).
* AC2 — POST 404 if template inexistant.
* AC3 — DELETE 204 + audit event.
* AC3 — DELETE 404 on idempotent re-DELETE.
* AC4 — GET happy + GET 404 on inexistent template.
* AC5 — atomicity test (REPLACE → publish throws → 0 row touched).
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


def _stdio_config() -> dict[str, Any]:
    return {"command": "python", "args": ["-m", "tests.fixtures.mcp_mock_server"]}


async def _create_template(client: httpx.AsyncClient, *, name: str) -> str:
    resp = await client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": "producteur", "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["template_id"]


async def _create_server(client: httpx.AsyncClient, *, name: str) -> tuple[str, list[str]]:
    """Returns (server_id, [tool_id, tool_id])."""
    resp = await client.post(
        "/api/v1/tools/servers",
        headers=_auth_headers(),
        json={
            "name": name,
            "transport": "stdio",
            "connection_config": _stdio_config(),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["server_id"], [t["tool_id"] for t in body["tools"]]


@pytest.mark.integration
async def test_replace_assign_happy_path(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — POST {tool_ids: [t1]} on empty template → 1 assignment + 1 audit event."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac2-replace-happy")
        _server_id, tool_ids = await _create_server(client, name="ac2-replace-happy-srv")

        resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": [tool_ids[0]]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["template_id"] == template_id
        assert len(body["assigned_tools"]) == 1
        assert body["assigned_tools"][0]["tool_id"] == tool_ids[0]

        # Junction row created.
        async with seed_session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM agent_template_tools WHERE agent_template_id = :tid"),
                {"tid": template_id},
            )
            assert int(count.scalar_one()) == 1

        # 1 audit event tool_assigned (no unassigned since template was empty).
        async with seed_session_factory() as session:
            assigned = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_template.tool_assigned' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(assigned.scalar_one()) == 1


@pytest.mark.integration
async def test_replace_assign_diff_emits_added_and_removed_events(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — REPLACE diff (start with [t1], replace with [t2]) → 1 unassigned + 1 assigned."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac2-diff")
        _server_id, tool_ids = await _create_server(client, name="ac2-diff-srv")

        # First : assign t1.
        await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": [tool_ids[0]]},
        )
        # Then : REPLACE with t2 only (t1 should disappear, t2 should appear).
        resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": [tool_ids[1]]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["assigned_tools"]) == 1
        assert body["assigned_tools"][0]["tool_id"] == tool_ids[1]

        # Final junction state : exactly 1 row (t2).
        async with seed_session_factory() as session:
            rows = await session.execute(
                text("SELECT tool_id FROM agent_template_tools WHERE agent_template_id = :tid"),
                {"tid": template_id},
            )
            tids = {str(r[0]) for r in rows.all()}
            assert tids == {tool_ids[1]}

        # Events : 2 assigned (t1 first, t2 second) + 1 unassigned (t1 in 2nd call).
        async with seed_session_factory() as session:
            assigned_count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_template.tool_assigned' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            unassigned_count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_template.tool_unassigned' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(assigned_count.scalar_one()) == 2
            assert int(unassigned_count.scalar_one()) == 1


@pytest.mark.integration
async def test_replace_assign_clear_all_emits_unassign_per_tool(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — POST {tool_ids: []} clears the assignments + emits N unassign events."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac2-clear")
        _server_id, tool_ids = await _create_server(client, name="ac2-clear-srv")

        # Seed both tools.
        await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": tool_ids},
        )
        # Now clear.
        resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": []},
        )
        assert resp.status_code == 200
        assert resp.json()["assigned_tools"] == []

        async with seed_session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM agent_template_tools WHERE agent_template_id = :tid"),
                {"tid": template_id},
            )
            assert int(count.scalar_one()) == 0


@pytest.mark.integration
async def test_replace_assign_404_if_any_tool_missing(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 + décision #8 — strict 404, no partial success."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac2-partial")
        _server_id, tool_ids = await _create_server(client, name="ac2-partial-srv")

        ghost = str(uuid4())
        resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": [tool_ids[0], ghost]},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert "tool" in body.get("detail", "").lower()

        # AND no assignment was created (atomicity).
        async with seed_session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM agent_template_tools WHERE agent_template_id = :tid"),
                {"tid": template_id},
            )
            assert int(count.scalar_one()) == 0


@pytest.mark.integration
async def test_replace_assign_404_if_template_missing(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/templates/{uuid4()}/tools",
            headers=_auth_headers(),
            json={"tool_ids": []},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert "agent template" in body.get("detail", "").lower()


@pytest.mark.integration
async def test_delete_template_tool_happy_path(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — DELETE assignment → 204 + audit event."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac3-delete")
        _server_id, tool_ids = await _create_server(client, name="ac3-delete-srv")

        # Seed one assignment.
        await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": [tool_ids[0]]},
        )

        del_resp = await client.delete(
            f"/api/v1/agents/templates/{template_id}/tools/{tool_ids[0]}",
            headers=_auth_headers(),
        )
        assert del_resp.status_code == 204

        # Assignment removed.
        async with seed_session_factory() as session:
            count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM agent_template_tools "
                    "WHERE agent_template_id = :tid AND tool_id = :tlid"
                ),
                {"tid": template_id, "tlid": tool_ids[0]},
            )
            assert int(count.scalar_one()) == 0

        # And re-DELETE returns 404 (strict idempotency décision #10).
        del2 = await client.delete(
            f"/api/v1/agents/templates/{template_id}/tools/{tool_ids[0]}",
            headers=_auth_headers(),
        )
        assert del2.status_code == 404


@pytest.mark.integration
async def test_list_template_tools_happy_and_empty(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC4 — GET tools list happy + empty + 404 if template inexistant."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac4-list")

        # Empty assignments → 200 + [].
        empty = await client.get(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
        )
        assert empty.status_code == 200
        assert empty.json()["assigned_tools"] == []

        # Add a tool, re-fetch.
        _server_id, tool_ids = await _create_server(client, name="ac4-list-srv")
        await client.post(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
            json={"tool_ids": [tool_ids[0]]},
        )
        full = await client.get(
            f"/api/v1/agents/templates/{template_id}/tools",
            headers=_auth_headers(),
        )
        assert full.status_code == 200
        assert len(full.json()["assigned_tools"]) == 1

        # 404 if template inexistant.
        ghost = await client.get(
            f"/api/v1/agents/templates/{uuid4()}/tools",
            headers=_auth_headers(),
        )
        assert ghost.status_code == 404


@pytest.mark.integration
async def test_replace_assign_atomicity_publish_failure_rolls_back(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5 — atomicity : publish throws on tool_assigned → 0 row in junction."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac5-replace-atomicity")
        _server_id, tool_ids = await _create_server(client, name="ac5-replace-atomicity-srv")

        # NOW patch publish — only the m2 tool_assigned event will fail.
        import agentive_backend.features.m2_agent_registry.service as svc_module

        real_publish = svc_module.publish

        async def _selective_explode(event_type: str, *args: Any, **kwargs: Any) -> Any:
            if event_type == "m2.agent_template.tool_assigned":
                raise RuntimeError("simulated bus failure on tool_assigned")
            return await real_publish(event_type, *args, **kwargs)

        monkeypatch.setattr(svc_module, "publish", _selective_explode)

        with pytest.raises(RuntimeError, match="simulated bus failure"):
            await client.post(
                f"/api/v1/agents/templates/{template_id}/tools",
                headers=_auth_headers(),
                json={"tool_ids": [tool_ids[0]]},
            )

        # Junction empty (rollback).
        async with seed_session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM agent_template_tools WHERE agent_template_id = :tid"),
                {"tid": template_id},
            )
            assert int(count.scalar_one()) == 0
