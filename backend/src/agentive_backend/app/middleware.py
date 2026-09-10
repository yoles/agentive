"""HTTP middlewares : correlation_id + auth token.

Both are pure ASGI middleware (``__call__(scope, receive, send)``), NOT
``starlette.middleware.base.BaseHTTPMiddleware``. ``BaseHTTPMiddleware``
buffers a downstream response's ENTIRE body before forwarding any of it to
the client — fine for ordinary JSON responses, but it silently breaks any
incrementally-streamed response (Story 4.2 T8.2's SSE run-events endpoint):
confirmed via T10.6 that an SSE generator wrapped by
``BaseHTTPMiddleware``-based middleware delivers NOTHING to the client until
the generator fully completes, not as each event is yielded. Since these two
middlewares wrap every request in the app, keeping them on
``BaseHTTPMiddleware`` would make that SSE endpoint non-functional in
production, not just in tests.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
    except ValueError, AttributeError, TypeError:
        return None


class CorrelationIdMiddleware:
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

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        incoming = request.headers.get(CORRELATION_HEADER)
        cid = _parse_incoming_correlation_id(incoming) or new_correlation_id()
        set_correlation_id(cid)

        async def send_with_correlation_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [*message.get("headers", []), (CORRELATION_HEADER.encode(), cid.encode())]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_correlation_header)


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


class AuthTokenMiddleware:
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

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)

        # P2 — CORS preflight requests (OPTIONS with Access-Control-Request-*)
        # are sent by browsers WITHOUT credentials per the CORS spec, so
        # AuthTokenMiddleware would 401 every preflight and break every
        # browser-based call to /api/v1/*. Skip auth on OPTIONS so the inner
        # CORSMiddleware can handle the preflight short-circuit.
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Public routes bypass auth entirely.
        if request.url.path in _PUBLIC_ROUTES:
            await self.app(scope, receive, send)
            return

        # P1 — middleware may run in test apps that did not execute the
        # full lifespan, so ``app.state.auth_token_hash`` could be missing.
        # Returning a clean 503 beats an opaque AttributeError → 500.
        stored_hash: str | None = getattr(request.app.state, "auth_token_hash", None)
        if stored_hash is None:
            _log.error("auth.middleware_state_not_initialised")
            response = JSONResponse(
                status_code=503,
                content={
                    "type": "/errors/auth/not-configured",
                    "title": "Authentication not configured",
                    "status": 503,
                    "correlation_id": get_correlation_id(),
                },
                media_type="application/problem+json",
            )
            await response(scope, receive, send)
            return

        # ── Extract Bearer token (P7: case-insensitive scheme per RFC 7235) ─
        auth_header = request.headers.get("Authorization", "")
        scheme, _, raw_token = auth_header.partition(" ")
        # P11 — also reject `Bearer ` (empty token after stripping).
        if scheme.lower() != "bearer" or not raw_token.strip():
            response = JSONResponse(
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
            await response(scope, receive, send)
            return

        # ── Validate token ────────────────────────────────────────────────
        if not verify_token(raw_token, stored_hash):
            response = JSONResponse(
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
            await response(scope, receive, send)
            return

        # ── Audit event (fire-and-forget) ─────────────────────────────────
        # Capture values before calling the downstream app — the request may
        # be mutated or GC'd while the background task is pending.
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

        await self.app(scope, receive, send)
