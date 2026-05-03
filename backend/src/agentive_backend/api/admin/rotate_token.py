"""``POST /api/v1/admin/rotate-token`` — static token rotation (Story 1.7).

Generates a new cryptographically-random API token, bcrypt-hashes it, and
replaces the in-process hash stored in ``app.state.auth_token_hash``.

MVP limitation: the new hash is in-process only — a restart will reset to
the value of ``AGENTIVE_API_TOKEN`` in the environment. Operators must
update ``.env`` (or their secrets store) to persist the rotation. In
multi-worker deployments, only the worker that handled this request will
see the new hash; siblings continue accepting the old token until restart.
This is acceptable for the MVP single-worker model and is documented
explicitly so operators avoid running multi-worker uvicorn in production.

The endpoint is protected by the AuthTokenMiddleware that wraps all
``/api/v1/*`` routes.  A valid current token is required in
``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agentive_backend.shared.auth import generate_token, hash_token
from agentive_backend.shared.event_bus import publish_and_commit
from agentive_backend.shared.logging import get_logger

router = APIRouter(tags=["admin"])
_log = get_logger(__name__)

# P5 — module-level lock serialises concurrent rotation requests. Without it,
# two parallel POSTs both bcrypt-hash a new token (~150ms each) and replace
# ``app.state.auth_token_hash``; the LOSING caller still receives a fresh
# new_token over the wire but it's already overwritten in app.state — the
# operator who saves that token to their secret store gets bricked out.
# Module-level (not app.state) so the lock survives across requests in the
# same process; asyncio.Lock is event-loop bound — fine for single-worker
# uvicorn (the MVP deployment model).
_rotation_lock = asyncio.Lock()


@router.post("/rotate-token")
async def rotate_token(request: Request) -> JSONResponse:
    """Rotate the active API token.

    1. Generates a new random token.
    2. Bcrypt-hashes it.
    3. Replaces ``app.state.auth_token_hash`` (atomic in the single-threaded
       asyncio event loop, serialized with a module-level Lock to prevent
       concurrent-rotation ghost tokens).
    4. Publishes ``system.token.rotated`` audit event.
    5. Returns the plaintext token **once** with ``Cache-Control: no-store``
       so reverse proxies / browsers / CDNs do not cache the response.
    """
    # P5 — serialize the entire generate→hash→swap→audit block.
    async with _rotation_lock:
        new_token = generate_token()
        new_hash = hash_token(new_token)

        # Atomic replacement within the asyncio event loop.
        request.app.state.auth_token_hash = new_hash

        # Publish audit event (best-effort — failure must not prevent the
        # rotation response from reaching the caller).
        factory = getattr(request.app.state, "session_factory", None)
        if factory is not None:
            try:
                async with factory() as session:
                    await publish_and_commit(
                        session,
                        "system.token.rotated",
                        {"actor": "api_token"},
                    )
            except Exception:
                # P17 — escalate audit-publish failure to error level for
                # observability. Operators MUST notice rotation audit gaps.
                _log.exception("auth.rotate_token_audit_event_failed")
        else:
            _log.warning("auth.rotate_token_audit_skipped_no_factory")

        _log.info("auth.token_rotated")

    # P6 — Cache-Control: no-store prevents proxies/CDNs from caching the
    # token response. ``Pragma: no-cache`` is the HTTP/1.0 fallback.
    body = {
        "new_token": new_token,
        "note": (
            "Save this token immediately — it will not be shown again. "
            "Update AGENTIVE_API_TOKEN in your .env to persist across restarts."
        ),
    }
    return JSONResponse(
        status_code=200,
        content=body,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
        },
    )
