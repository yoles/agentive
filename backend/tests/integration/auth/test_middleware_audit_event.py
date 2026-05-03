"""Integration tests — audit events ``system.token.used`` + ``system.token.rotated``
— Story 1.7 (AC3 + AC4 + post-review fix-batch P10/P13/P14).

Uses ``httpx.AsyncClient`` + ``ASGITransport`` (same event loop as the test)
so that ``asyncio.create_task`` background tasks complete before assertions.
The wait is **deterministic** — we drain the middleware's ``_background_tasks``
set rather than relying on ``asyncio.sleep`` (which is flaky on loaded CI).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.api.admin.rotate_token import router as rotate_token_router
from agentive_backend.app.middleware import (
    AuthTokenMiddleware,
    CorrelationIdMiddleware,
    _background_tasks,
)


def _make_app(
    token: str,
    session_factory: async_sessionmaker[AsyncSession] | None,
) -> FastAPI:
    app = FastAPI()
    app.state.auth_token_hash = token
    app.state.session_factory = session_factory

    app.add_middleware(AuthTokenMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(rotate_token_router, prefix="/api/v1/admin")

    @app.get("/api/v1/test")
    async def _endpoint() -> dict[str, str]:
        return {"ok": "true"}

    return app


async def _drain_background_tasks(timeout: float = 2.0) -> None:
    """P13 — deterministic wait for fire-and-forget audit tasks.

    Replaces ``asyncio.sleep(0.2)`` which was flaky on loaded CI. We snapshot
    the current pending tasks and gather them with a timeout so a stuck task
    can't hang the test indefinitely.
    """
    pending = list(_background_tasks)
    if not pending:
        return
    await asyncio.wait_for(
        asyncio.gather(*pending, return_exceptions=True),
        timeout=timeout,
    )


async def _delete_outbox_event_type(
    seed_session_factory: async_sessionmaker[AsyncSession],
    event_type: str,
) -> None:
    """Clean a single event_type from outbox_events (test isolation)."""
    async with seed_session_factory() as session:
        await session.execute(
            text("DELETE FROM outbox_events WHERE event_type = :t"),
            {"t": event_type},
        )
        await session.commit()


async def _count_outbox(
    factory: async_sessionmaker[AsyncSession],
    event_type: str,
) -> int:
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM outbox_events WHERE event_type = :t"),
            {"t": event_type},
        )
        return int(result.scalar_one())


# ─── system.token.used ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_valid_auth_publishes_token_used_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Successful auth → outbox row with event_type=system.token.used."""
    await _delete_outbox_event_type(seed_session_factory, "system.token.used")

    app = _make_app("change_me", app_session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/test", headers={"Authorization": "Bearer change_me"})
    assert response.status_code == 200

    await _drain_background_tasks()

    count = await _count_outbox(app_session_factory, "system.token.used")
    assert count >= 1, "Expected at least one system.token.used outbox row"


@pytest.mark.asyncio
async def test_invalid_auth_does_not_publish_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Failed auth (401) → no system.token.used event published."""
    await _delete_outbox_event_type(seed_session_factory, "system.token.used")

    app = _make_app("change_me", app_session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/test", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401

    await _drain_background_tasks()

    count = await _count_outbox(app_session_factory, "system.token.used")
    assert count == 0


# ─── P14 — system.token.rotated ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotation_publishes_token_rotated_event(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rotation → outbox row with event_type=system.token.rotated."""
    await _delete_outbox_event_type(seed_session_factory, "system.token.rotated")

    app = _make_app("change_me", app_session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/admin/rotate-token",
            headers={"Authorization": "Bearer change_me"},
        )
    assert response.status_code == 200

    # Rotation publishes synchronously inside the handler (not a fire-and-forget
    # task), so no drain is strictly needed — but call it anyway for symmetry
    # with the system.token.used event scheduled by the middleware.
    await _drain_background_tasks()

    count = await _count_outbox(app_session_factory, "system.token.rotated")
    assert count >= 1, "Expected one system.token.rotated outbox row"


# ─── P10 — DB down at audit time → request still 200, error logged ───────────


@pytest.mark.asyncio
async def test_audit_publish_db_failure_does_not_block_request(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the audit DB write fails, the request must still return 200.

    Simulate a "DB down" scenario by injecting a session_factory that always
    raises on entry. The auth check itself does not touch the DB (the token
    hash is in app.state) — only the fire-and-forget audit publish does.
    The middleware swallows the failure and logs at error level; the
    request response is unaffected.
    """

    class FailingFactory:
        """Async sessionmaker stub that raises on context-manager entry."""

        def __call__(self) -> FailingFactory:
            return self

        async def __aenter__(self) -> AsyncSession:  # type: ignore[empty-body]
            raise RuntimeError("simulated DB outage")

        async def __aexit__(self, *args: object) -> None:
            return None

    failing_factory = FailingFactory()
    app = _make_app("change_me", failing_factory)  # type: ignore[arg-type]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/api/v1/test", headers={"Authorization": "Bearer change_me"})

    # Request must succeed even though the audit publish failed.
    assert response.status_code == 200
    assert response.json() == {"ok": "true"}

    # Drain the background task — it should complete (with an internal
    # exception that the middleware swallows).
    await _drain_background_tasks()
    # No outbox row was written (the factory raised), but the request
    # surface is intact — this is the only behaviour we assert.
