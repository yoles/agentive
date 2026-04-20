"""HTTP middlewares : correlation_id.

Auth token middleware will be added in Story 1.7.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agentive_backend.shared.correlation import new_correlation_id, set_correlation_id

CORRELATION_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Inject a correlation_id into every request + response.

    Priority order :
    1. Use ``X-Correlation-ID`` from the incoming request if present (UUID format expected).
    2. Otherwise generate a new UUID v7 via :func:`new_correlation_id`.

    The correlation_id is bound to the async context (ContextVar) so logs +
    events bus + OTel spans can pick it up transparently.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        incoming = request.headers.get(CORRELATION_HEADER)
        cid = incoming or new_correlation_id()
        set_correlation_id(cid)
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        return response
