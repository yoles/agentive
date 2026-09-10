"""End-to-end integration tests : `GET /api/v1/agents/templates/{id}/diversity-check`
(Story 2.8 T3.6, AC3).

Monte l'app via les helpers partagés du conftest du package
(``make_e2e_app`` / ``e2e_auth_headers``, P-09 du CR 2.4).
"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.contracts.diversity import (
    INCOMPLETE_CONFIG_REASON,
    SAME_CONFIG_REASON,
)

from .conftest import e2e_auth_headers, make_e2e_app


async def _seed_template(client: httpx.AsyncClient, *, archetype: str) -> str:
    """Create a template with a collision-proof name (P-10).

    Local to this module rather than imported from another test module :
    ``make_e2e_app``/``e2e_auth_headers`` are the shared helpers (conftest,
    P-09 du CR 2.4), the seeding body is three lines and the ``uuid4()``
    suffix removes the need for hand-picked "Controller A" ... "Controller F"
    names.
    """
    resp = await client.post(
        "/api/v1/agents/templates",
        headers=e2e_auth_headers(),
        json={"archetype": archetype, "name": f"Story 2.8 {archetype} {uuid4()}"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["template_id"])


async def _configure_llm(
    client: httpx.AsyncClient,
    template_id: str,
    *,
    model: str,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> None:
    resp = await client.put(
        f"/api/v1/agents/templates/{template_id}",
        headers=e2e_auth_headers(),
        json={
            "llm_model": model,
            "llm_params": {"temperature": temperature, "max_tokens": max_tokens},
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.integration
async def test_diversity_check_same_model_and_params_is_not_diverse(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        producer_id = await _seed_template(client, archetype="producteur")
        await _configure_llm(client, controller_id, model="claude-3-5-sonnet-20241022")
        await _configure_llm(client, producer_id, model="claude-3-5-sonnet-20241022")

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_diverse"] is False
        assert body["reason"] == SAME_CONFIG_REASON


@pytest.mark.integration
async def test_diversity_check_different_model_is_diverse(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        producer_id = await _seed_template(client, archetype="producteur")
        await _configure_llm(client, controller_id, model="claude-3-5-sonnet-20241022")
        await _configure_llm(client, producer_id, model="gpt-4o")

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_diverse"] is True


@pytest.mark.integration
async def test_diversity_check_same_model_different_max_tokens_is_diverse(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        producer_id = await _seed_template(client, archetype="producteur")
        await _configure_llm(
            client, controller_id, model="claude-3-5-sonnet-20241022", max_tokens=4096
        )
        await _configure_llm(
            client, producer_id, model="claude-3-5-sonnet-20241022", max_tokens=8192
        )

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_diverse"] is True


@pytest.mark.integration
async def test_diversity_check_neither_configured_returns_none(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        producer_id = await _seed_template(client, archetype="producteur")
        # Neither template ever went through the Story 2.2 PUT, so neither has
        # llm_model/llm_params. P-09 : the two one-sided cases are covered by the
        # two tests below, this one is deliberately the both-missing case.

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_diverse"] is None
        assert body["reason"] == INCOMPLETE_CONFIG_REASON


@pytest.mark.integration
async def test_diversity_check_only_producer_configured_returns_none(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-09 : one-sided config must stay ``None``, never a falsely diverse ``True``."""
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        producer_id = await _seed_template(client, archetype="producteur")
        await _configure_llm(client, producer_id, model="claude-3-5-sonnet-20241022")

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_diverse"] is None
        assert body["reason"] == INCOMPLETE_CONFIG_REASON
        assert body["controller_model"] is None
        assert body["producer_model"] == "claude-3-5-sonnet-20241022"


@pytest.mark.integration
async def test_diversity_check_only_controller_configured_returns_none(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-09 : symmetric counterpart of the test above."""
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        producer_id = await _seed_template(client, archetype="producteur")
        await _configure_llm(client, controller_id, model="gpt-4o")

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_diverse"] is None
        assert body["reason"] == INCOMPLETE_CONFIG_REASON
        assert body["controller_model"] == "gpt-4o"
        assert body["producer_model"] is None


@pytest.mark.integration
async def test_diversity_check_same_template_for_both_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """P-04 : self-comparison is a client error, not a ``is_diverse=False`` verdict."""
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _seed_template(client, archetype="controleur")

        resp = await client.get(
            f"/api/v1/agents/templates/{template_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": template_id},
        )

        assert resp.status_code == 422, resp.text
        assert "distinct" in resp.json()["detail"]


@pytest.mark.integration
async def test_diversity_check_unknown_controller_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        producer_id = await _seed_template(client, archetype="producteur")
        missing_controller_id = str(uuid4())

        resp = await client.get(
            f"/api/v1/agents/templates/{missing_controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 404, resp.text
        assert missing_controller_id in resp.json()["detail"]


@pytest.mark.integration
async def test_diversity_check_unknown_producer_returns_404(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        missing_producer_id = str(uuid4())

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": missing_producer_id},
        )

        assert resp.status_code == 404, resp.text
        assert missing_producer_id in resp.json()["detail"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# I-01 : the controller side is typed, the controlled side is free
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.integration
async def test_diversity_check_non_controller_in_controller_slot_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A producteur/producteur pair is not an FR15 couple (Story 2.8 I-01)."""
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        not_a_controller_id = await _seed_template(client, archetype="producteur")
        producer_id = await _seed_template(client, archetype="producteur")

        resp = await client.get(
            f"/api/v1/agents/templates/{not_a_controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": producer_id},
        )

        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "controleur" in detail
        assert "producteur" in detail


@pytest.mark.integration
async def test_diversity_check_swapped_ids_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Swapped ids used to answer 200 because the verdict is symmetric (I-01)."""
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        producer_id = await _seed_template(client, archetype="producteur")
        await _configure_llm(client, controller_id, model="claude-3-5-sonnet-20241022")
        await _configure_llm(client, producer_id, model="gpt-4o")

        resp = await client.get(
            f"/api/v1/agents/templates/{producer_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": controller_id},
        )

        assert resp.status_code == 422, resp.text
        assert producer_id in resp.json()["detail"]


@pytest.mark.integration
async def test_diversity_check_accepts_non_producteur_controlled_template(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FR15 constrains the Contrôleur role only, not the reviewed agent (I-01).

    A Contrôleur reviewing an ``analyste`` is a valid pair, and the response
    echoes both archetypes so the caller sees what the verdict covered.
    """
    app = make_e2e_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        controller_id = await _seed_template(client, archetype="controleur")
        analyst_id = await _seed_template(client, archetype="analyste")
        await _configure_llm(client, controller_id, model="claude-3-5-sonnet-20241022")
        await _configure_llm(client, analyst_id, model="gpt-4o")

        resp = await client.get(
            f"/api/v1/agents/templates/{controller_id}/diversity-check",
            headers=e2e_auth_headers(),
            params={"producer_template_id": analyst_id},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_diverse"] is True
        assert body["controller_archetype"] == "controleur"
        assert body["producer_archetype"] == "analyste"
