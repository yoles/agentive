"""HTTP middlewares : correlation_id.

Auth token middleware will be added in Story 1.7.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agentive_backend.shared.correlation import new_correlation_id, set_correlation_id

CORRELATION_HEADER = "X-Correlation-ID"


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


class AuthTokenMiddleware(BaseHTTPMiddleware):
    """Bearer token auth — **skeleton Sprint 0**, full implementation Story 1.7.

    Intentionally NOT registered in :func:`agentive_backend.app.main.create_app`
    yet — the check is a pass-through. Story 1.7 will :
    - Validate ``Authorization: Bearer <token>`` against a bcrypt-hashed
      value stored in the secrets store.
    - Publish an ``m0.token.used`` audit event with correlation_id.
    - Respond 401 RFC 7807 on invalid / missing token for `/api/v1/*` routes.
    - Expose public routes (``/health``, ``/ready``) via a route-allowlist.

    The class exists now so imports + wiring can be set up in the same place
    across feature epics, and so the docstring promise in :mod:`app` matches
    reality.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Pass-through until Story 1.7.
        return await call_next(request)
