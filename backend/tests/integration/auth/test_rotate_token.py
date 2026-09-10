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

        # Real ASGI concurrency gives NO ordering guarantee between two
        # in-flight requests beyond genuine suspension points — plain ASGI
        # middleware (Story 4.2: converted away from `BaseHTTPMiddleware`,
        # which breaks SSE streaming — see `app/middleware.py` docstring)
        # can let one request's handler run to completion, including its
        # `app.state.auth_token_hash` mutation, before the other's auth
        # check ever executes. So the race LOSER may now be rejected at
        # the auth layer (401) instead of receiving a 200 with a token
        # that silently never works (a genuine "ghost token", arguably a
        # worse failure mode — fail loud beats fail confusing). Both
        # outcomes uphold the actual safety property this test guards:
        # AT MOST ONE of the two rotations' tokens is ever active.
        results = [r1, r2]
        succeeded = [r for r in results if r.status_code == 200]
        rejected = [r for r in results if r.status_code == 401]
        assert len(succeeded) + len(rejected) == 2, [r.status_code for r in results]
        assert len(succeeded) >= 1, "at least one concurrent rotation must succeed"

        tokens = [r.json()["new_token"] for r in succeeded]
        assert len(set(tokens)) == len(tokens), "no two successful rotations share a token"

        # Exactly one token among the successful rotation(s) works afterward
        # — the lock guarantees a single, deterministic last writer.
        works = []
        for token in tokens:
            resp = await client.get(
                "/api/v1/protected", headers={"Authorization": f"Bearer {token}"}
            )
            works.append(resp.status_code == 200)
        assert sum(works) == 1, (
            f"Expected exactly one token to be active after concurrent rotation: {works}"
        )

        # `sum(works) == 1` is TRIVIALLY true in the single-success branch —
        # one issued token, and of course it works. The anti-ghost-token
        # invariant only bites when it is checked against the tokens that
        # must NOT work, so assert those explicitly:
        #  * the pre-rotation token is dead (a rotation really happened);
        #  * a rejected rotation issued nothing at all (no ghost).
        old_resp = await client.get(
            "/api/v1/protected", headers={"Authorization": "Bearer change_me"}
        )
        assert old_resp.status_code == 401, "the pre-rotation token must not survive"
        for rejected_resp in rejected:
            assert "new_token" not in rejected_resp.json(), (
                "a rotation rejected at the auth layer must not hand back a token"
            )


@pytest.mark.asyncio
async def test_sequential_rotations_leave_only_the_last_token_active() -> None:
    """Deterministic companion to the concurrent test above.

    Which of two racing rotations wins is decided by the ASGI scheduler, so
    the concurrent test cannot GUARANTEE it exercises the both-succeeded
    branch — on a run where one request is rejected, its central assertion
    degenerates. This test pins the same "single last writer" property with
    no race at all: every intermediate token must be dead, only the final one
    alive. Together they cover the invariant under both schedulings.
    """
    app = _make_app("change_me")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        tokens = ["change_me"]
        for _ in range(3):
            resp = await client.post(
                "/api/v1/admin/rotate-token",
                headers={"Authorization": f"Bearer {tokens[-1]}"},
            )
            assert resp.status_code == 200, resp.text
            tokens.append(resp.json()["new_token"])

        assert len(set(tokens)) == len(tokens), "every rotation must mint a distinct token"

        alive = []
        for token in tokens:
            resp = await client.get(
                "/api/v1/protected", headers={"Authorization": f"Bearer {token}"}
            )
            alive.append(resp.status_code == 200)

        assert alive == [False, False, False, True], (
            f"only the last token may remain active, got {alive}"
        )
