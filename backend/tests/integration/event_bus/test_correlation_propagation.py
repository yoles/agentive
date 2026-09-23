"""AC5 — correlation_id propagates from publisher to handler ContextVar + structlog."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.event_bus import (
    OutboxWorker,
    publish_and_commit,
    subscribe,
)
from agentive_backend.shared.event_bus.types import Event

pytestmark = pytest.mark.integration


async def test_handler_receives_correlation_id_in_contextvar(
    outbox_worker: OutboxWorker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seen = asyncio.Event()
    captured: dict[str, str | None] = {}

    async def handler(event: Event) -> None:
        captured["from_event"] = str(event.correlation_id)
        captured["from_contextvar"] = get_correlation_id()
        seen.set()

    await subscribe("workflow_engine.workflow.started", handler)

    expected_cid = uuid4()
    async with session_factory() as session:
        await publish_and_commit(
            session,
            "workflow_engine.workflow.started",
            {"workflow_id": str(uuid4())},
            correlation_id=expected_cid,
        )

    await asyncio.wait_for(seen.wait(), timeout=2.0)
    assert captured["from_event"] == str(expected_cid)
    # ContextVar bound by the worker's _dispatch before invoking the handler.
    assert captured["from_contextvar"] == str(expected_cid)


async def test_handler_log_records_carry_correlation_id(
    outbox_worker: OutboxWorker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC5 step (e) — emitted structlog records include ``correlation_id`` (P12).

    The worker's ``_dispatch`` binds the event's correlation_id into the
    ContextVar before invoking the handler. We log inside the handler with
    the correlation_id read from the ContextVar, then assert ``capture_logs``
    surfaces it on the event dict.

    Note : ``structlog.testing.capture_logs`` substitutes its own minimal
    processor chain that does NOT include ``merge_contextvars`` from the
    project's normal pipeline — so we explicitly bind the value on the log
    call rather than relying on the contextvars processor.
    """
    seen = asyncio.Event()
    handler_log = structlog.get_logger("test.handler")

    async def handler(_event: Event) -> None:
        handler_log.info("handler_smoke", correlation_id=get_correlation_id())
        seen.set()

    await subscribe("workflow_engine.workflow.started", handler)

    expected_cid = uuid4()
    with structlog.testing.capture_logs() as captured_logs:
        async with session_factory() as session:
            await publish_and_commit(
                session,
                "workflow_engine.workflow.started",
                {"workflow_id": str(uuid4())},
                correlation_id=expected_cid,
            )
        await asyncio.wait_for(seen.wait(), timeout=2.0)

    handler_records = [log for log in captured_logs if log.get("event") == "handler_smoke"]
    assert handler_records, "handler structlog line not captured"
    assert handler_records[0].get("correlation_id") == str(expected_cid)
