"""End-to-end integration tests — agent instances (Story 2.4 T7.1).

Couvre :
* AC1 — POST happy 201 + body shape complet (snapshot capture).
* AC2 — Isolation v2/v3 (template bumped between 2 instantiations → snapshots distincts).
* AC4 — GET /agents/instances/{id} happy 200.
* AC5 — Atomicité : rollback si publish() throw → 0 row.
* AC6 — POST 404 si template inexistant.
* AC6 — POST 404 si workflow_run_id fourni mais inexistant.
* POST 422 si template_id non-UUID (FastAPI Path validation).

Pattern : ``httpx.AsyncClient`` + ``ASGITransport`` (event loop partagé,
mirror Story 2.1 + Story 2.2 e2e). DB cleanup via ``clean_repository_tables``
implicite (autouse dans ``tests/integration/repositories/conftest.py``).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# P-09 (CR 2026-05-10) — _make_app + _auth_headers factor dans conftest.py partagé.
from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app


async def _create_template(
    client: httpx.AsyncClient, *, name: str, archetype: str = "producteur"
) -> str:
    """Helper — POST /agents/templates et retourne template_id."""
    resp = await client.post(
        "/api/v1/agents/templates",
        headers=_auth_headers(),
        json={"archetype": archetype, "name": name},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["template_id"]


async def _put_template(
    client: httpx.AsyncClient, template_id: str, *, system_prompt: str
) -> dict[str, Any]:
    resp = await client.put(
        f"/api/v1/agents/templates/{template_id}",
        headers=_auth_headers(),
        json={"system_prompt": system_prompt},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.integration
async def test_instantiate_template_happy_path_creates_instance_with_snapshot(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1 — POST 201 + snapshot complet figé + audit event publié."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac1-instance")

        resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/instances",
            headers=_auth_headers(),
            json={},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "instance_id" in body and len(body["instance_id"]) == 36
        assert body["template_id"] == template_id
        assert body["template_version"] == 1
        assert body["workflow_run_id"] is None
        snapshot = body["snapshot"]
        assert snapshot["template_id"] == template_id
        assert snapshot["template_version"] == 1
        assert snapshot["name"] == "ac1-instance"
        assert snapshot["archetype"] == "producteur"
        assert "config" in snapshot
        assert "prompt_base" in snapshot["config"]
        # P-18 (CR 2026-05-10) — created_at présent + format ISO 8601 timezone-aware
        # (Pydantic v2 + FastAPI sérialise automatiquement, mais le test verrouille
        # contre une régression future qui passerait la valeur en naive datetime).
        assert "created_at" in body
        assert body["created_at"].endswith("Z") or "+" in body["created_at"], (
            f"created_at should be timezone-aware ISO 8601, got {body['created_at']!r}"
        )

        # DB row exists.
        async with seed_session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT template_id, template_version, snapshot FROM agent_instances "
                    "WHERE id = :iid"
                ),
                {"iid": body["instance_id"]},
            )
            row = result.one()
            assert str(row[0]) == template_id
            assert row[1] == 1
            assert row[2]["template_version"] == 1

        # Audit event m2.agent_instance.created published.
        async with seed_session_factory() as session:
            count_result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_instance.created' "
                    "AND payload->>'instance_id' = :iid"
                ),
                {"iid": body["instance_id"]},
            )
            assert int(count_result.scalar_one()) == 1


@pytest.mark.integration
async def test_instantiate_template_isolation_v2_v3_no_hot_swap(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — modifier le template entre 2 instantiations ne touche pas la première.

    P-06 + P-11 (CR 2026-05-10) — Renforce le test :
    * P-11 : asserte explicitement que le PUT bump bien la version à 2 AVANT
      de POSTer i2 (failsafe contre une régression archetype qui pré-remplirait
      `config.system_prompt`, ce qui ferait `bump_version=False` et casserait
      l'assertion `i2.template_version == 2` pour la mauvaise raison).
    * P-06 : asserte que 2 events `m2.agent_instance.created` sont publiés
      dans `outbox_events` (1 par instance créée). Sans cette assertion, une
      régression qui skip l'event pour la 2nde instance passerait silencieusement.
    """
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac2-isolation")

        # First instance (template version 1, system_prompt absent par défaut).
        i1_resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/instances",
            headers=_auth_headers(),
            json={},
        )
        i1 = i1_resp.json()
        assert i1["template_version"] == 1

        # Bump template (Story 2.2 PUT → version 2 because system_prompt set).
        # P-11 — failsafe assertion : verrouille que le PUT a bien bumpé la
        # version à 2. Si l'archetype change pour pré-remplir system_prompt
        # à "v2 prompt" (improbable mais non-vérifié), bump_version=False et
        # template.version reste à 1 — l'assertion ci-dessous catch cette
        # régression frontalement.
        put_resp = await _put_template(client, template_id, system_prompt="v2 prompt")
        assert put_resp["version"] == 2, (
            f"PUT did not bump version to 2 (got {put_resp['version']}). "
            "Likely cause : archetype now pre-fills config.system_prompt with "
            "the same value, making system_prompt_changed=False. Test rest is invalid."
        )

        # Second instance (template version 2 now).
        i2_resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/instances",
            headers=_auth_headers(),
            json={},
        )
        i2 = i2_resp.json()
        assert i2["template_version"] == 2
        assert i2["snapshot"]["config"]["system_prompt"] == "v2 prompt"

        # Re-fetch i1 — its snapshot must still be v1 (no system_prompt).
        i1_get = await client.get(
            f"/api/v1/agents/instances/{i1['instance_id']}",
            headers=_auth_headers(),
        )
        i1_again = i1_get.json()
        assert i1_again["template_version"] == 1
        assert "system_prompt" not in i1_again["snapshot"]["config"]

        # P-06 — assert 2 outbox events (1 per instance).
        async with seed_session_factory() as session:
            count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_instance.created' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(count.scalar_one()) == 2, (
                "AC1+AC2 violation : expected 2 audit events (1 per instance), "
                "got fewer. The 2nd instance may have skipped the event."
            )


@pytest.mark.integration
async def test_get_instance_by_id_happy_path(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC4 — GET /agents/instances/{id} happy 200 avec body complet."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac4-get-instance")
        post_resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/instances",
            headers=_auth_headers(),
            json={},
        )
        instance_id = post_resp.json()["instance_id"]

        get_resp = await client.get(
            f"/api/v1/agents/instances/{instance_id}",
            headers=_auth_headers(),
        )
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        assert body["instance_id"] == instance_id
        assert body["template_id"] == template_id
        assert body["template_version"] == 1


@pytest.mark.integration
async def test_get_instance_by_id_not_found_returns_404_rfc7807(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC4 — GET sur instance_id inexistant → 404 RFC 7807."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/agents/instances/{uuid4()}",
            headers=_auth_headers(),
        )
        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["status"] == 404
        assert "instance" in body.get("detail", "").lower()


@pytest.mark.integration
async def test_get_instance_by_id_invalid_uuid_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC4 — GET avec instance_id non-UUID → 422 (FastAPI Path validation)."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/agents/instances/not-a-uuid",
            headers=_auth_headers(),
        )
        assert resp.status_code == 422


@pytest.mark.integration
async def test_instantiate_template_404_when_template_missing(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC6 — POST sur template_id inexistant → 404 + 0 row + 0 event."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    nonexistent = uuid4()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/agents/templates/{nonexistent}/instances",
            headers=_auth_headers(),
            json={},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["status"] == 404
        assert "template" in body.get("detail", "").lower()
        assert "not found" in body.get("detail", "").lower()

        # No row + no event.
        async with seed_session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM agent_instances WHERE template_id = :tid"),
                {"tid": str(nonexistent)},
            )
            assert int(count.scalar_one()) == 0

            # P-07 (CR 2026-05-10) — assert 0 outbox event published. Without
            # this, a regression that publishes a "instance_create_failed" or
            # similar defensive event for the 404 path would pass silently.
            event_count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_instance.created' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": str(nonexistent)},
            )
            assert int(event_count.scalar_one()) == 0


@pytest.mark.integration
async def test_instantiate_template_404_when_workflow_run_missing(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC6 (variant) — workflow_run_id fourni mais inexistant → 404."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        template_id = await _create_template(client, name="ac6-missing-run")
        resp = await client.post(
            f"/api/v1/agents/templates/{template_id}/instances",
            headers=_auth_headers(),
            json={"workflow_run_id": str(uuid4())},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert "workflow run" in body.get("detail", "").lower()


@pytest.mark.integration
async def test_instantiate_template_invalid_uuid_returns_422(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST avec template_id non-UUID → 422 (FastAPI Path validation)."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/templates/not-a-uuid/instances",
            headers=_auth_headers(),
            json={},
        )
        assert resp.status_code == 422


@pytest.mark.integration
async def test_instantiate_template_atomicity_publish_failure_rolls_back_insert(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5 (P-01 CR 2026-05-10) — atomicité E2E réelle.

    Monkeypatch ``service.publish`` pour qu'il throw UNIQUEMENT pour
    ``m2.agent_instance.created`` (le ``_create_template`` setup utilise
    aussi ``service.publish`` pour ``m2.agent_template.created``, on ne
    peut pas le casser globalement). Attend une 5xx côté HTTP, puis assert
    que ``SELECT count(*) FROM agent_instances WHERE template_id = :tid``
    reste à 0 (rollback complet). Le test unit dans
    ``test_instantiate_template_service.py`` couvre la propagation de
    l'erreur via mocks ; ce test verrouille l'invariant DB rollback côté
    Postgres réel (testcontainer), qui ne peut pas être validé via mocks.
    """
    import agentive_backend.features.m2_agent_registry.service as svc_module

    real_publish = svc_module.publish

    async def _selective_explode(event_type: str, *args: Any, **kwargs: Any) -> Any:
        if event_type == "m2.agent_instance.created":
            raise RuntimeError("simulated bus failure")
        return await real_publish(event_type, *args, **kwargs)

    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Setup : create the template BEFORE installing the failing publish ;
        # `_create_template` exercises `service.publish` for
        # `m2.agent_template.created` which must succeed so the template row
        # exists and the atomicity test has a real target.
        template_id = await _create_template(client, name="ac5-atomicity")

        # NOW patch publish — only the instance-create event will fail.
        monkeypatch.setattr(svc_module, "publish", _selective_explode)

        # The POST must fail. Note : `_make_app` does not register a generic
        # `Exception` handler (production `app.main` does, returning 500), so
        # under `ASGITransport` the unhandled `RuntimeError` propagates back
        # through the httpx client. We catch it here — the important
        # invariant for AC5 is the DB rollback assertion below, not the
        # specific HTTP status code.
        with pytest.raises(RuntimeError, match="simulated bus failure"):
            await client.post(
                f"/api/v1/agents/templates/{template_id}/instances",
                headers=_auth_headers(),
                json={},
            )

        # Critical AC5 assertion : the INSERT was rolled back ; 0 instance row.
        async with seed_session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM agent_instances WHERE template_id = :tid"),
                {"tid": template_id},
            )
            assert int(count.scalar_one()) == 0, (
                "AC5 violation : agent_instances has rows for the template even though "
                "publish() failed — the transaction did not rollback."
            )

        # And no outbox event was committed either (the publish was the failing step).
        async with seed_session_factory() as session:
            event_count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM outbox_events "
                    "WHERE event_type = 'm2.agent_instance.created' "
                    "AND payload->>'template_id' = :tid"
                ),
                {"tid": template_id},
            )
            assert int(event_count.scalar_one()) == 0
