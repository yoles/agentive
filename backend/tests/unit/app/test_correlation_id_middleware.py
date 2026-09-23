"""Unit tests — ``CorrelationIdMiddleware`` (Story 4.9 T7).

Driven directly at the ASGI level (hand-built scope/receive/send), not
through ``TestClient``: the leak T7.1 closes is about the ContextVar
surviving past the request it belongs to on an ASGI caller that does not
give every request its own ``asyncio.Task`` — which is exactly what calling
the middleware twice, back to back, in the SAME test coroutine reproduces.
``TestClient`` cannot observe this from a test's own thread (it drives the
app from a separate worker thread with its own context), so it would not
catch a regression here.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentive_backend.app.middleware import CORRELATION_HEADER, CorrelationIdMiddleware
from agentive_backend.shared.correlation import get_correlation_id, set_correlation_id


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/test",
        "headers": headers or [],
        "query_string": b"",
        "root_path": "",
    }


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _make_send(sink: list[dict[str, Any]]) -> Any:
    async def _send(message: dict[str, Any]) -> None:
        sink.append(message)

    return _send


async def _ok_app(_scope: Any, _receive: Any, send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": b"{}"})


async def _app_with_existing_correlation_header(_scope: Any, _receive: Any, send: Any) -> None:
    """Simulates a downstream layer that already stamped the header — the
    scenario T7.2 must not duplicate."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(CORRELATION_HEADER.encode(), b"stale-downstream-value")],
        }
    )
    await send({"type": "http.response.body", "body": b"{}"})


def _header_values(messages: list[dict[str, Any]], name: bytes) -> list[bytes]:
    start = next(m for m in messages if m["type"] == "http.response.start")
    return [value for key, value in start["headers"] if key.lower() == name.lower()]


@pytest.mark.asyncio
async def test_correlation_id_is_reset_after_the_request_completes() -> None:
    """T7.1 — without a reset, a ContextVar bound during one request stays
    bound in any context that reuses this coroutine's Task afterward (this
    test's own, since pytest-asyncio runs the whole test as one Task)."""
    set_correlation_id("pre-existing-outer-value")
    middleware = CorrelationIdMiddleware(_ok_app)  # type: ignore[arg-type]

    await middleware(_scope(), _receive, _make_send([]))

    assert get_correlation_id() == "pre-existing-outer-value"


@pytest.mark.asyncio
async def test_correlation_id_does_not_leak_between_two_requests_on_the_same_task() -> None:
    """The scenario the docstring names explicitly: an ASGI caller that
    drives two requests through the SAME Task (this repo's own test suite,
    via ``httpx.ASGITransport``, can do exactly this)."""
    middleware = CorrelationIdMiddleware(_ok_app)  # type: ignore[arg-type]

    sink_a: list[dict[str, Any]] = []
    await middleware(
        _scope(headers=[(CORRELATION_HEADER.encode(), b"11111111-1111-7111-8111-111111111111")]),
        _receive,
        _make_send(sink_a),
    )
    assert get_correlation_id() is None  # reset after request A, not leaked into this Task

    sink_b: list[dict[str, Any]] = []
    await middleware(_scope(), _receive, _make_send(sink_b))
    second_cid = _header_values(sink_b, CORRELATION_HEADER.encode())[0].decode()

    assert get_correlation_id() is None  # reset after request B too
    # Request B got its OWN fresh id — never request A's, which a leak would
    # have made `set_correlation_id`'s `or new_correlation_id()` never reach.
    assert second_cid != "11111111-1111-7111-8111-111111111111"


@pytest.mark.asyncio
async def test_correlation_header_is_not_duplicated_when_already_present() -> None:
    """T7.2 — a downstream layer that already set this header must not end
    up with two `X-Correlation-ID` lines in the response."""
    middleware = CorrelationIdMiddleware(_app_with_existing_correlation_header)  # type: ignore[arg-type]
    sink: list[dict[str, Any]] = []

    await middleware(_scope(), _receive, _make_send(sink))

    values = _header_values(sink, CORRELATION_HEADER.encode())
    assert len(values) == 1
    assert values[0] != b"stale-downstream-value"  # this middleware's value wins
