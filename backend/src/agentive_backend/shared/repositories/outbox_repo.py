"""Public API surface for :class:`OutboxEvent`.

The ``shared.event_bus`` package (Story 1.4) currently issues raw
``text()`` queries against ``outbox_events`` from inside the bus itself —
this is acceptable because ``shared.event_bus`` is a peer of
``shared.repositories`` (both live under ``shared/``), not a feature module.
The Contract 3 ``import-linter`` rule forbids ``sqlalchemy`` imports from
``features.*`` only.

This repo is shipped as the **public surface for downstream consumers**:
- Story 9.1 audit consumer (drains the outbox into ``audit_events``).
- Story 8.5 Event Hooks engine (subscribes to outbox events).
- Future Sprint 4+ Redis Streams migration (read pending rows during
  cutover).

The event_bus internals are **not** refactored to use this repo in Story
1.5 — see Story 1.5 "Hors scope strict" for the rationale.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from agentive_backend.infra.db.models import OutboxEvent
from agentive_backend.shared.repositories.base import BaseRepo


class OutboxRepo(BaseRepo):
    """Public API surface for OutboxEvent. ALL DB access must go through this class."""

    async def insert(
        self,
        *,
        event_id: UUID,
        correlation_id: UUID,
        event_type: str,
        payload: dict[str, Any],
        tenant_id: UUID | None = None,
    ) -> OutboxEvent:
        async with self.with_tenant(tenant_id) as session:
            event = OutboxEvent(
                id=event_id,
                correlation_id=correlation_id,
                event_type=event_type,
                payload=payload,
                tenant_id=tenant_id,
            )
            session.add(event)
            await session.flush()
            await session.refresh(event)
            return event

    async def get_unprocessed(
        self,
        *,
        limit: int = 100,
        tenant_id: UUID | None = None,
    ) -> list[OutboxEvent]:
        async with self.with_tenant(tenant_id) as session:
            # Tie-break on ``id`` so two events sharing the same
            # ``created_at`` (microsecond ties happen on bulk inserts in a
            # single transaction) deliver in a stable, deterministic order.
            stmt = (
                select(OutboxEvent)
                .where(OutboxEvent.processed_at.is_(None))
                .order_by(OutboxEvent.created_at.asc(), OutboxEvent.id.asc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def mark_processed(self, event_id: UUID, *, tenant_id: UUID | None = None) -> int:
        """Mark an event as processed. Returns rowcount (0 = already processed)."""
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id, OutboxEvent.processed_at.is_(None))
                .values(processed_at=func.now())
            )
            result = await session.execute(stmt)
            rowcount = getattr(result, "rowcount", None)
            return int(rowcount or 0)
