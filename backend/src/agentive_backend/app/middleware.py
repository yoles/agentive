"""HTTP middlewares : correlation_id + auth token."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agentive_backend.shared.auth import verify_token
from agentive_backend.shared.correlation import (
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
)
from agentive_backend.shared.event_bus import publish_and_commit
from agentive_backend.shared.logging import get_logger

CORRELATION_HEADER = "X-Correlation-ID"

# Routes that bypass auth — exact-string match (NOT ``startswith``).
#
# We use exact match because a ``startswith("/api/v1/docs")`` allowlist would
# accidentally permit any path that happens to share the prefix, e.g.
# ``/api/v1/docs/leak`` or a future ``/api/v1/docs-internal``. ASGI servers
# normalize ``..`` segments before middleware sees the path, so traversal
# attacks like ``/api/v1/docs/../admin`` are NOT the concern here — prefix
# leakage is.
_PUBLIC_ROUTES: frozenset[str] = frozenset(
    {
        "/health",
        "/ready",
        "/api/v1/docs",
        "/api/v1/openapi.json",
        "/api/v1/redoc",
    }
)

_log = get_logger(__name__)

# Module-level set holds strong references to fire-and-forget tasks so the
# garbage collector cannot collect them before they complete.  Each task
# removes itself on completion via ``add_done_callback``.
_background_tasks: set[asyncio.Task[None]] = set()


def _parse_incoming_correlation_id(raw: str | None) -> str | None:
    """Return a sanitized UUID string if ``raw`` is a valid UUID, else ``None``.

    Any non-UUID, malformed, or control-char-containing value is dropped to
    prevent log injection, HTTP response splitting, and memory DoS via
    arbitrarily long headers.
    """
    if raw is None:
        return None
    # Guard against oversized payloads — valid UUIDs are 36 chars.
    if len(raw) > 64:
        return None
    try:
        # uuid.UUID rejects control chars, CR/LF, and any non-hex noise.
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        return None


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Inject a validated correlation_id into every request + response.

    Resolution order :
    1. Use ``X-Correlation-ID`` from the incoming request if it parses as a UUID.
    2. Otherwise generate a new UUID v7 via :func:`new_correlation_id`.

    Client-supplied values that are not valid UUIDs are DROPPED silently (a
    new ID is generated). This prevents log injection via CR/LF or control
    characters, HTTP response splitting, and memory DoS.

    The correlation_id is bound to the async context (ContextVar) so logs +
    events bus + OTel spans can pick it up transparently.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(CORRELATION_HEADER)
        cid = _parse_incoming_correlation_id(incoming) or new_correlation_id()
        set_correlation_id(cid)
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        return response


async def _publish_token_used_event(
    factory: async_sessionmaker[AsyncSession],
    path: str,
    method: str,
) -> None:
    """Fire-and-forget outbox publish for ``system.token.used``.

    Failures are swallowed and logged at ERROR level — a DB hiccup must not
    block API responses, but the audit gap MUST be observable to operators.
    Values are captured before the task is scheduled so the ``Request``
    object can be GC'd safely while this coroutine is pending.
    """
    try:
        async with factory() as session:
            await publish_and_commit(
                session,
                "system.token.used",
                {"actor": "api_token", "endpoint": path, "method": method},
            )
    except Exception:
        # P17 — escalate from warning to error so the audit gap is visible
        # in monitoring (warnings are commonly filtered out in dashboards).
        # ``log.exception`` includes the traceback for diagnosis.
        _log.exception("auth.audit_event_publish_failed", endpoint=path)


class AuthTokenMiddleware(BaseHTTPMiddleware):
    """Bearer token auth — validates ``Authorization: Bearer <token>`` on
    all ``/api/v1/*`` routes.

    Public routes (``/health``, ``/ready``, OpenAPI docs) bypass auth.
    Invalid or missing tokens return RFC 7807 ``application/problem+json``
    responses with HTTP 401.

    On success, an audit event ``system.token.used`` is published
    fire-and-forget via ``asyncio.create_task`` so a transient DB failure
    never blocks the authenticated request.

    The current effective token hash lives in ``app.state.auth_token_hash``
    (initialised by :func:`lifespan._init_auth_token`). The
    rotation endpoint updates this attribute atomically within the
    single-threaded asyncio event loop.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # P2 — CORS preflight requests (OPTIONS with Access-Control-Request-*)
        # are sent by browsers WITHOUT credentials per the CORS spec, so
        # AuthTokenMiddleware would 401 every preflight and break every
        # browser-based call to /api/v1/*. Skip auth on OPTIONS so the inner
        # CORSMiddleware can handle the preflight short-circuit.
        if request.method == "OPTIONS":
            return await call_next(request)

        # Public routes bypass auth entirely.
        if request.url.path in _PUBLIC_ROUTES:
            return await call_next(request)

        # P1 — middleware may run in test apps that did not execute the
        # full lifespan, so ``app.state.auth_token_hash`` could be missing.
        # Returning a clean 503 beats an opaque AttributeError → 500.
        stored_hash: str | None = getattr(request.app.state, "auth_token_hash", None)
        if stored_hash is None:
            _log.error("auth.middleware_state_not_initialised")
            return JSONResponse(
                status_code=503,
                content={
                    "type": "/errors/auth/not-configured",
                    "title": "Authentication not configured",
                    "status": 503,
                    "correlation_id": get_correlation_id(),
                },
                media_type="application/problem+json",
            )

        # ── Extract Bearer token (P7: case-insensitive scheme per RFC 7235) ─
        auth_header = request.headers.get("Authorization", "")
        scheme, _, raw_token = auth_header.partition(" ")
        # P11 — also reject `Bearer ` (empty token after stripping).
        if scheme.lower() != "bearer" or not raw_token.strip():
            return JSONResponse(
                status_code=401,
                content={
                    "type": "/errors/auth/missing-token",
                    "title": "Missing authentication token",
                    "status": 401,
                    "correlation_id": get_correlation_id(),
                },
                media_type="application/problem+json",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # ── Validate token ────────────────────────────────────────────────
        if not verify_token(raw_token, stored_hash):
            return JSONResponse(
                status_code=401,
                content={
                    "type": "/errors/auth/invalid-token",
                    "title": "Invalid authentication token",
                    "status": 401,
                    "correlation_id": get_correlation_id(),
                },
                media_type="application/problem+json",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # ── Audit event (fire-and-forget) ─────────────────────────────────
        # Capture values before calling call_next — the request may be
        # mutated or GC'd while the background task is pending.
        path = request.url.path
        method = request.method
        # P1 — defensive: if session_factory is missing (test app), skip the
        # audit publish silently rather than crashing the request. The 503
        # earlier would have caught a missing auth state; this branch
        # specifically handles partial setups (auth state set but DB factory
        # not wired).
        factory: async_sessionmaker[AsyncSession] | None = getattr(
            request.app.state, "session_factory", None
        )
        if factory is not None:
            task = asyncio.create_task(
                _publish_token_used_event(factory, path, method),
                name="auth.token_used",
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)

        return await call_next(request)
