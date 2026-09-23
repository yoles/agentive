"""End-to-end integration tests — `PUT /api/v1/agents/templates/{id}` (Story 2.2 AC7).

Pattern miroir de :mod:`tests.integration.agent_registry.test_create_template_e2e` —
``httpx.AsyncClient`` + ``ASGITransport`` (single event loop).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.agent_registry.test_create_template_e2e import (
    _auth_headers,
    _count_outbox,
    _make_app,
)


async def _seed_template(
    client: httpx.AsyncClient, *, name: str, archetype: str = "producteur"
) -> str:
    resp = await client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": archetype, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["template_id"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.integration
async def test_put_template_with_system_prompt_bumps_version_and_inserts_prompt(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 happy — PUT avec system_prompt → version=2 + prompt v2 + outbox event."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _seed_template(client, name="Story 2.2 Bump")
        resp = await client.put(
            f"/api/v1/agents/templates/{template_id}",
            headers=_auth_headers(),
            json={
                "system_prompt": "Tu es un agent Producteur expert TypeScript.",
                "llm_model": "claude-3-5-sonnet-20241022",
                "llm_params": {"temperature": 0.2, "max_tokens": 4096},
                "provider_chain": ["anthropic"],
                "error_policy": {
                    "on_timeout": "retry_with_backoff",
                    "max_retries": 3,
                    "backoff_strategy": "exponential",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == 2
        assert body["template_id"] == template_id
        assert body["config"]["system_prompt"].startswith("Tu es un agent Producteur")
        assert body["config"]["llm_model"] == "claude-3-5-sonnet-20241022"

    async with seed_session_factory() as session:
        # agent_templates row updated
        result = await session.execute(
            text("SELECT version, config FROM agent_templates WHERE id = :tid"),
            {"tid": template_id},
        )
        row = result.one()
        assert row[0] == 2
        assert row[1]["llm_model"] == "claude-3-5-sonnet-20241022"

        # prompts row inserted with version=2
        result = await session.execute(
            text(
                "SELECT version, content FROM prompts"
                " WHERE agent_template_id = :tid ORDER BY version DESC LIMIT 1"
            ),
            {"tid": template_id},
        )
        prompt_row = result.one()
        assert prompt_row[0] == 2
        assert "TypeScript" in prompt_row[1]

    # Outbox event emitted
    count = await _count_outbox(seed_session_factory, "agent_registry.agent_template.updated")
    assert count >= 1


@pytest.mark.integration
async def test_put_template_without_system_prompt_no_bump_no_prompt_insert(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 partial — PUT sans system_prompt → pas de bump version, pas de prompt insert."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _seed_template(client, name="Story 2.2 NoBump")
        resp = await client.put(
            f"/api/v1/agents/templates/{template_id}",
            headers=_auth_headers(),
            json={"llm_params": {"temperature": 0.5, "max_tokens": 8192}},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == 1  # NO bump
        assert body["config"]["llm_params"] == {"temperature": 0.5, "max_tokens": 8192}

    async with seed_session_factory() as session:
        # No prompt row inserted for this template
        result = await session.execute(
            text("SELECT COUNT(*) FROM prompts WHERE agent_template_id = :tid"),
            {"tid": template_id},
        )
        assert int(result.scalar_one()) == 0


@pytest.mark.integration
async def test_put_template_not_found_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — UUID valide mais inexistant → 404 RFC 7807."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/agents/templates/00000000-0000-0000-0000-000000000000",
            headers=_auth_headers(),
            json={"system_prompt": "x"},
        )
        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["type"] == "/errors/not-found"
        assert "not found" in body.get("detail", "").lower()


@pytest.mark.integration
async def test_put_template_invalid_uuid_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 / D4 — UUID invalide en path → 422 (FastAPI Path UUID)."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put(
            "/api/v1/agents/templates/not-a-uuid",
            headers=_auth_headers(),
            json={"system_prompt": "x"},
        )
        assert resp.status_code == 422


@pytest.mark.integration
async def test_put_template_extra_field_rejected_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — extra=forbid : champ inconnu → 422."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _seed_template(client, name="Story 2.2 Extra")
        resp = await client.put(
            f"/api/v1/agents/templates/{template_id}",
            headers=_auth_headers(),
            json={"system_prompt": "x", "rogue_field": "leak"},
        )
        assert resp.status_code == 422


@pytest.mark.integration
async def test_get_template_detail_returns_full_config(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC6 — GET détail retourne config complet (bons défauts archetype + champs édités)."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _seed_template(client, name="Story 2.2 GetDetail")
        # GET fresh template (version=1, archetype defaults)
        resp = await client.get(f"/api/v1/agents/templates/{template_id}", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["template_id"] == template_id
        assert body["version"] == 1
        assert "prompt_base" in body["config"]
        assert body["config"]["role"] == "producer"

        # 404 on missing UUID
        resp = await client.get(
            "/api/v1/agents/templates/00000000-0000-0000-0000-000000000001",
            headers=_auth_headers(),
        )
        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"


@pytest.mark.integration
async def test_put_template_atomicity_rollback_on_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Atomicité P-02 — si event_bus.publish raise, ni le bump version ni le prompt n'apparaissent."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    # Seed first (no monkey-patch active).
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _seed_template(client, name="Story 2.2 Atomicity")

    # Snapshot baseline state via seed session (BYPASSRLS).
    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT version FROM agent_templates WHERE id = :tid"),
            {"tid": template_id},
        )
        baseline_version = int(result.scalar_one())
        result = await session.execute(
            text("SELECT COUNT(*) FROM prompts WHERE agent_template_id = :tid"),
            {"tid": template_id},
        )
        baseline_prompt_count = int(result.scalar_one())

    # Patch publish to RAISE — must trigger rollback of the entire tx.
    import agentive_backend.features.agent_registry.service as svc_module

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated outbox publish failure")

    monkeypatch.setattr(svc_module, "publish", _boom)

    # P-07 fix (Story 2.2 review 2026-05-09) — l'assertion canonique de
    # l'atomicité est : « QUEL QUE SOIT le code HTTP retourné (200, 500,
    # ou exception non-handled), l'état DB doit être UNCHANGED ». La
    # version précédente du test asservissait `pytest.raises(RuntimeError)`
    # à un détail de plomberie ASGITransport qui pourrait changer si un
    # global exception handler 500 était ajouté à l'app. On capture
    # l'exception ou la response, peu importe — on vérifie ensuite la DB.
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                f"/api/v1/agents/templates/{template_id}",
                headers=_auth_headers(),
                json={"system_prompt": "should rollback"},
            )
            # Si un handler global converti l'exception en 500, on l'accepte —
            # l'atomicité de la transaction est testée par la DB ci-dessous.
            assert resp.status_code in (500, 502, 503), (
                f"Expected server error (500/502/503), got {resp.status_code}"
            )
    except RuntimeError as exc:
        # Pas de handler global — l'exception remonte, ce qui est aussi
        # un signal valide que la transaction n'a pas commit.
        assert "simulated outbox publish failure" in str(exc)

    # CONTRACT D'ATOMICITÉ : version unchanged + 0 prompt row pour ce template.
    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT version FROM agent_templates WHERE id = :tid"),
            {"tid": template_id},
        )
        assert int(result.scalar_one()) == baseline_version
        result = await session.execute(
            text("SELECT COUNT(*) FROM prompts WHERE agent_template_id = :tid"),
            {"tid": template_id},
        )
        assert int(result.scalar_one()) == baseline_prompt_count
