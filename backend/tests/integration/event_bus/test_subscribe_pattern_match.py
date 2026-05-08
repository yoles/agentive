"""AC4 — regex pattern subscription routes only matching events."""

from __future__ import annotations

import asyncio
import re
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.event_bus import (
    OutboxWorker,
    publish_and_commit,
    subscribe,
)
from agentive_backend.shared.event_bus.types import Event

pytestmark = pytest.mark.integration


async def test_pattern_routes_only_matching_events(
    outbox_worker: OutboxWorker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    received: list[Event] = []
    received_count = asyncio.Event()
    expected = 1

    async def handler(event: Event) -> None:
        received.append(event)
        if len(received) >= expected:
            received_count.set()

    await subscribe(re.compile(r"^m3\..*$"), handler)

    cid = uuid4()
    async with session_factory() as session:
        await publish_and_commit(
            session,
            "m3.workflow.started",
            {},
            correlation_id=cid,
        )
        await publish_and_commit(
            session,
            "m4.chunk.indexed",
            {},
            correlation_id=cid,
        )

    await asyncio.wait_for(received_count.wait(), timeout=2.0)
    # Wait a touch longer to confirm the m4 event does NOT slip into the list.
    await asyncio.sleep(0.6)

    assert len(received) == 1
    assert received[0].event_type == "m3.workflow.started"
