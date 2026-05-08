"""Integration test fixtures for the LLM layer.

The autouse ``_no_external_http`` fixture is the central guard rail for
NFR9 / Sprint 0 cost discipline: any test that accidentally tries to
reach ``api.anthropic.com`` or ``api.openai.com`` fails loudly before
spending real money. The whitelist covers test-internal hosts only
(``localhost``, ``127.0.0.1``, ``host.docker.internal``, plus the IPs
``testcontainers`` may pick at runtime).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest

_LOCAL_HOSTS_WHITELIST: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "host.docker.internal",
        # FastAPI TestClient defaults to this host name for in-process
        # ASGI requests — never reaches the network.
        "testserver",
        # testcontainers IPs are resolved at runtime; we accept any
        # 172.x.x.x bridge address via a prefix check below.
    }
)


def _is_external(url: str | httpx.URL) -> bool:
    parsed = httpx.URL(url)
    host = (parsed.host or "").strip()
    if not host:
        return False  # relative URLs / unknown — let them through
    if host in _LOCAL_HOSTS_WHITELIST:
        return False
    # Docker bridge networks 172.16.0.0/12 cover testcontainers default.
    return not host.startswith(("172.", "10.", "192.168."))


@pytest.fixture(autouse=True)
def _no_external_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail any HTTP call that targets a non-local host."""
    real_async_send = httpx.AsyncClient.send
    real_sync_send = httpx.Client.send

    async def guarded_async_send(
        self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        if _is_external(request.url):
            raise RuntimeError(
                f"Refusing external HTTP call in CI/tests: {request.method} {request.url}. "
                "Use MockProvider or httpx.MockTransport instead."
            )
        return await real_async_send(self, request, *args, **kwargs)

    def guarded_sync_send(
        self: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        if _is_external(request.url):
            raise RuntimeError(
                f"Refusing external HTTP call in CI/tests: {request.method} {request.url}. "
                "Use MockProvider or httpx.MockTransport instead."
            )
        return real_sync_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_send)
    monkeypatch.setattr(httpx.Client, "send", guarded_sync_send)
    yield
