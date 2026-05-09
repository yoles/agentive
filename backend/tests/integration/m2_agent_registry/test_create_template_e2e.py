"""End-to-end integration tests — `POST /api/v1/agents/templates` (Story 2.1 T4.1).

Pattern : `httpx.AsyncClient` + `ASGITransport` (same event loop as the test
runner) so background tasks complete before assertions, mirroring
`tests/integration/auth/test_middleware_audit_event.py` (Story 1.7).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.app.middleware import AuthTokenMiddleware, CorrelationIdMiddleware
from agentive_backend.features.m2_agent_registry import load_registry
from agentive_backend.features.m2_agent_registry import router as agents_router
from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.exceptions import AgentiveError

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_app(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    token: str = "integration-test-token",
) -> FastAPI:
    """Build a minimal FastAPI app exercising the full Story 2.1 stack."""
    app = FastAPI()
    app.state.auth_token_hash = token
    app.state.session_factory = session_factory
    app.state.archetype_registry = load_registry()

    app.add_middleware(AuthTokenMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(agents_router, prefix="/api/v1")

    # Mirror app.main RFC 7807 handlers so the response shape is identical
    # to production. The handlers are duplicated verbatim — same approach
    # used in tests/unit/api/test_rfc7807_handler.py (Story 1.9).
    @app.exception_handler(AgentiveError)
    async def _handle_agentive_error(  # pragma: no cover — verbatim of app.main handler
        _request: Request, exc: AgentiveError
    ) -> JSONResponse:
        body: dict[str, Any] = {
            "type": exc.type,
            "title": exc.title,
            "status": exc.status,
            "correlation_id": get_correlation_id(),
        }
        if exc.detail:
            body["detail"] = exc.detail
        if exc.context:
            reserved = {"type", "title", "status", "correlation_id", "detail"}
            for k, v in exc.context.items():
                if k not in reserved:
                    body[k] = v
        return JSONResponse(
            status_code=exc.status, content=body, media_type="application/problem+json"
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_req_validation(  # pragma: no cover — verbatim of app.main handler
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Strip `input` (P-06 — credentials/PII echo) AND `ctx` (Pydantic
        # places the original ValueError instance under ctx.error, which is
        # not JSON-serializable for `value_error`-typed errors raised by
        # custom validators). Mirrors `app.main.handle_validation_error`.
        sanitized_errors = [
            {k: v for k, v in err.items() if k not in {"input", "ctx"}} for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "type": "/errors/validation",
                "title": "Validation failed",
                "status": status.HTTP_422_UNPROCESSABLE_ENTITY,
                "correlation_id": get_correlation_id(),
                "detail": "Request body validation failed",
                "errors": sanitized_errors,
            },
            media_type="application/problem+json",
        )

    return app


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer integration-test-token"}


async def _count_outbox(
    factory: async_sessionmaker[AsyncSession],
    event_type: str,
    *,
    name: str | None = None,
) -> int:
    """Count outbox rows for ``event_type``.

    Story 2.1 P-03 — When ``name`` is provided, the count is scoped to
    rows whose ``payload->>'name'`` matches. Without scoping, parallel /
    ordered tests in the same session would observe each other's rows
    (the m2 conftest deliberately drops the autouse ``clean_repository_tables``
    fixture so the registry-only lifespan tests don't pay testcontainer setup).
    """
    sql = "SELECT COUNT(*) FROM outbox_events WHERE event_type = :t"
    params: dict[str, str] = {"t": event_type}
    if name is not None:
        sql += " AND payload->>'name' = :n"
        params["n"] = name
    async with factory() as session:
        result = await session.execute(text(sql), params)
        return int(result.scalar_one())


async def _fetch_outbox_payload(
    factory: async_sessionmaker[AsyncSession],
    event_type: str,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Fetch the most recent outbox row for ``event_type`` (optionally filtered by ``name``)."""
    sql = "SELECT correlation_id, payload FROM outbox_events WHERE event_type = :t"
    params: dict[str, str] = {"t": event_type}
    if name is not None:
        sql += " AND payload->>'name' = :n"
        params["n"] = name
    sql += " ORDER BY created_at DESC LIMIT 1"
    async with factory() as session:
        result = await session.execute(text(sql), params)
        row = result.one()
        return {"correlation_id": str(row[0]), "payload": row[1]}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.integration
async def test_create_template_happy_path_persists_row_and_publishes_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 + AC6 — POST 201 → row in agent_templates + outbox event."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/templates",
            headers=_auth_headers(),
            json={"archetype": "producteur", "name": "Code Producer"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "Code Producer"
        assert body["archetype"] == "producteur"
        assert body["version"] == 1
        assert "template_id" in body and len(body["template_id"]) == 36

        # DB assertion via seed session (BYPASSRLS) — row exists.
        async with seed_session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT name, archetype, version, config FROM agent_templates WHERE id = :tid"
                ),
                {"tid": body["template_id"]},
            )
            row = result.one()
            assert row[0] == "Code Producer"
            assert row[1] == "producteur"
            assert row[2] == 1
            config = row[3]
            assert set(config.keys()) == {
                "prompt_base",
                "input_contract",
                "output_contract",
                "role",
            }
            assert config["role"] == "producer"

        # Audit event published (AC6) — scoped by name (Story 2.1 P-03 fix).
        count = await _count_outbox(
            seed_session_factory, "m2.agent_template.created", name="Code Producer"
        )
        assert count == 1
        event = await _fetch_outbox_payload(
            seed_session_factory, "m2.agent_template.created", name="Code Producer"
        )
        assert event["payload"]["name"] == "Code Producer"
        assert event["payload"]["archetype"] == "producteur"
        assert event["payload"]["version"] == 1
        assert event["payload"]["actor"] == "system"


@pytest.mark.integration
async def test_create_template_unknown_archetype_returns_422_rfc7807(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — Unknown archetype → 422 RFC 7807 + no row inserted."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/templates",
            headers=_auth_headers(),
            json={"archetype": "unknown_thing", "name": "Test"},
        )
        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["type"] == "/errors/validation"
        assert body["status"] == 422
        assert "Unknown archetype" in body.get("detail", "")
        # All 8 valid archetypes enumerated.
        for archetype_id in [
            "orchestrateur",
            "chercheur",
            "analyste",
            "producteur",
            "stratege",
            "controleur",
            "veilleur",
            "communicateur",
        ]:
            assert archetype_id in body["detail"]

        # No row inserted.
        async with seed_session_factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM agent_templates WHERE name = 'Test'")
            )
            assert int(result.scalar_one()) == 0

        # No audit event for THIS name — Story 2.1 P-03 scope-by-name avoids
        # picking up unrelated rows from earlier tests in the same session.
        count = await _count_outbox(seed_session_factory, "m2.agent_template.created", name="Test")
        assert count == 0


@pytest.mark.integration
async def test_create_template_empty_name_returns_422_rfc7807(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 + AC7 (T4.1 ≥12) — Empty `name` → 422 RFC 7807 + no row + no event."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/templates",
            headers=_auth_headers(),
            json={"archetype": "producteur", "name": ""},
        )
        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["type"] == "/errors/validation"
        assert body["status"] == 422

    # No row inserted, no audit event published.
    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM agent_templates WHERE name = ''")
        )
        assert int(result.scalar_one()) == 0
    count = await _count_outbox(seed_session_factory, "m2.agent_template.created", name="")
    assert count == 0


@pytest.mark.integration
async def test_create_template_whitespace_only_name_returns_422_rfc7807(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 + P-05 + AC7 (T4.1 ≥12) — Whitespace-only `name` → 422 (post-trim).

    The schema's `_strip_and_revalidate_name` validator rejects `"   "` after
    trimming so it is NOT persisted as three spaces (Story 2.1 P-05).
    """
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/templates",
            headers=_auth_headers(),
            json={"archetype": "producteur", "name": "   "},
        )
        assert resp.status_code == 422
        assert resp.headers["content-type"] == "application/problem+json"
        body = resp.json()
        assert body["type"] == "/errors/validation"
        # Custom validator surfaces a `value_error` mentioning the rule.
        errors_blob = str(body.get("errors", []))
        assert "blank" in errors_blob or "whitespace" in errors_blob

    # No row inserted (neither raw "   " nor stripped ""), no audit event.
    async with seed_session_factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM agent_templates WHERE name IN ('   ', '')")
        )
        assert int(result.scalar_one()) == 0
    count_blank = await _count_outbox(seed_session_factory, "m2.agent_template.created", name="")
    count_spaces = await _count_outbox(
        seed_session_factory, "m2.agent_template.created", name="   "
    )
    assert count_blank == 0
    assert count_spaces == 0


@pytest.mark.integration
async def test_create_template_duplicate_name_returns_409(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 — UniqueConstraint (name, version=1, tenant_id=NULL) → 409 RFC 7807."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/agents/templates",
            headers=_auth_headers(),
            json={"archetype": "producteur", "name": "Duplicate Tester"},
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/agents/templates",
            headers=_auth_headers(),
            json={"archetype": "producteur", "name": "Duplicate Tester"},
        )
        assert second.status_code == 409
        assert second.headers["content-type"] == "application/problem+json"
        body = second.json()
        assert body["type"] == "/errors/conflict"
        assert "already exists" in body.get("detail", "")
        assert "Duplicate Tester" in body["detail"]


@pytest.mark.integration
async def test_create_template_propagates_correlation_id(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC3 + AC6 — X-Correlation-ID propagated to outbox + response header."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    cid = "01999999-9999-7999-8999-999999999999"  # UUID v7-ish

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/agents/templates",
            headers={**_auth_headers(), "X-Correlation-ID": cid},
            json={"archetype": "chercheur", "name": "Curious George"},
        )
        assert resp.status_code == 201
        # Correlation echoed in response header (CorrelationIdMiddleware).
        assert resp.headers.get("x-correlation-id") == cid

    # Scope by name (Story 2.1 P-03) so this assertion is robust to test
    # ordering even when other happy-path tests have inserted rows.
    event = await _fetch_outbox_payload(
        seed_session_factory, "m2.agent_template.created", name="Curious George"
    )
    assert event["correlation_id"] == cid


@pytest.mark.integration
async def test_get_archetypes_list_via_full_stack(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — GET /agents/archetypes through the full middleware stack."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/agents/archetypes", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 8


@pytest.mark.integration
async def test_get_archetype_detail_via_full_stack(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC2 — GET /agents/archetypes/{id} returns prompt_base + contracts."""
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/agents/archetypes/controleur", headers=_auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "controleur"
        assert "prompt_base" in body and len(body["prompt_base"]) > 0
