"""Integration tests — ``POST /api/v1/admin/rotate-token`` — Story 1.7 (AC4)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentive_backend.api.admin.rotate_token import router as rotate_token_router
from agentive_backend.app.middleware import AuthTokenMiddleware, CorrelationIdMiddleware


def _make_app(token: str = "change_me") -> FastAPI:
    """Minimal FastAPI app with auth middleware + rotate-token endpoint.

    Includes a stub ``/api/v1/protected`` route so we can verify that the
    OLD token is rejected on a route that WOULD otherwise have succeeded —
    rather than relying on a non-existent route (where every request would
    be rejected by the middleware regardless of token validity).
    """
    app = FastAPI()
    app.state.auth_token_hash = token
    # Minimal stub — rotation publishes an audit event, but failure is
    # swallowed.  None session_factory triggers the except branch.
    app.state.session_factory = None

    app.add_middleware(AuthTokenMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(rotate_token_router, prefix="/api/v1/admin")

    # P9 — register a real protected route so "old token rejected" is
    # tested on a path that returns 200 with a valid token.
    @app.get("/api/v1/protected")
    async def _protected() -> dict[str, str]:
        return {"ok": "true"}

    return app


def test_rotate_token_with_valid_token_returns_200() -> None:
    """Valid current token → 200 with new_token + note."""
    app = _make_app("change_me")
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/admin/rotate-token",
        headers={"Authorization": "Bearer change_me"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "new_token" in body
    assert "note" in body
    # New token must be non-empty and different from the old one.
    assert body["new_token"]
    assert body["new_token"] != "change_me"


def test_rotate_token_response_has_no_store_cache_control() -> None:
    """P6 — Cache-Control: no-store prevents proxy caching of the token."""
    app = _make_app("change_me")
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/admin/rotate-token",
        headers={"Authorization": "Bearer change_me"},
    )
    assert response.status_code == 200
    cache_control = response.headers.get("cache-control", "")
    assert "no-store" in cache_control
    assert response.headers.get("pragma") == "no-cache"


def test_rotate_token_old_token_rejected_after_rotation() -> None:
    """After rotation, the OLD token must return 401 on a route that exists.

    P9 — Previously this test hit a non-existent route, so 401 was guaranteed
    by the middleware regardless of token validity (false positive).  Now it
    hits ``/api/v1/protected`` which returns 200 with a valid token.
    """
    app = _make_app("change_me")
    client = TestClient(app, raise_server_exceptions=False)

    # Sanity: old token works on the protected route BEFORE rotation.
    pre = client.get("/api/v1/protected", headers={"Authorization": "Bearer change_me"})
    assert pre.status_code == 200, "test setup invalid — old token must work pre-rotation"

    # Rotate
    rotate_resp = client.post(
        "/api/v1/admin/rotate-token",
        headers={"Authorization": "Bearer change_me"},
    )
    assert rotate_resp.status_code == 200
    new_token = rotate_resp.json()["new_token"]

    # The OLD token must now be rejected on the SAME protected route.
    old_resp = client.get(
        "/api/v1/protected",
        headers={"Authorization": "Bearer change_me"},
    )
    assert old_resp.status_code == 401
    assert old_resp.json()["type"] == "/errors/auth/invalid-token"

    # The NEW token must work on the protected route.
    new_resp = client.get(
        "/api/v1/protected",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert new_resp.status_code == 200


# ─── P5 — Concurrent rotation must not produce ghost tokens ──────────────────


@pytest.mark.asyncio
async def test_concurrent_rotation_serialised_no_ghost_tokens() -> None:
    """Two concurrent rotations: only the LAST one's token is active.

    Without the asyncio.Lock, both calls would race the
    ``app.state.auth_token_hash = new_hash`` assignment — the first caller
    might think their token is active when it has already been overwritten
    by the second. With the lock, both rotations complete in serialized
    order and exactly one token (the second one's) is active at the end.
    """
    app = _make_app("change_me")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        # Two concurrent rotations.
        r1, r2 = await asyncio.gather(
            client.post(
                "/api/v1/admin/rotate-token",
                headers={"Authorization": "Bearer change_me"},
            ),
            client.post(
                "/api/v1/admin/rotate-token",
                headers={"Authorization": "Bearer change_me"},
            ),
        )

        # Both calls SHOULD succeed (HTTP-level), but only one of the two
        # returned tokens is the last winner.
        # Note: the second call's auth check happens AFTER the first
        # rotation completed, so the second call uses the NEW token created
        # by the first rotation — wait, no: both call sites use Bearer
        # "change_me" which is the original. The lock serializes them, so:
        # - Call A acquires lock, generates token_A, replaces hash → returns token_A.
        # - Call B is blocked at the lock; when A releases, the hash is now
        #   token_A's hash. Call B's auth check ALREADY happened before
        #   the lock (in the middleware, before the handler runs). So both
        #   succeed at auth (with the original "change_me"), and both
        #   produce a new token. The LAST one to acquire the lock wins.
        assert r1.status_code == 200
        assert r2.status_code == 200
        token_a = r1.json()["new_token"]
        token_b = r2.json()["new_token"]
        assert token_a != token_b

        # Exactly one of {token_a, token_b} now works on the protected route.
        # Without the lock, the test would be racy — with the lock, the
        # last rotation deterministically wins.
        a_resp = await client.get(
            "/api/v1/protected", headers={"Authorization": f"Bearer {token_a}"}
        )
        b_resp = await client.get(
            "/api/v1/protected", headers={"Authorization": f"Bearer {token_b}"}
        )
        # Exactly one must work. We don't assert WHICH one (event-loop
        # scheduling is implementation-defined), but the XOR must hold.
        a_works = a_resp.status_code == 200
        b_works = b_resp.status_code == 200
        assert a_works ^ b_works, (
            f"Expected exactly one token to work after concurrent rotation: "
            f"token_a={a_resp.status_code}, token_b={b_resp.status_code}"
        )
