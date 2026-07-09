"""Outbox publisher — transactional INSERT + NOTIFY post-commit.

Two public entry points
-----------------------
``publish(event_type, payload, *, session, correlation_id=None) -> UUID``
    INSERT only — caller owns the commit. Use when the publication must be
    atomic with other business writes inside the same transaction. Caller must
    invoke :func:`emit_notify` (or commit + :func:`emit_notify`) once the
    transaction is committed, otherwise the worker will only pick the event up
    on its periodic poll fallback (5s default).

``publish_and_commit(session, event_type, payload, *, correlation_id=None) -> UUID``
    Convenience wrapper — INSERT, ``await session.commit()``, then
    :func:`emit_notify`. The right choice for fire-and-forget callers
    (lifespan hooks, scheduled tasks) that own the session exclusively.

Why a separate autocommit connection for NOTIFY
-----------------------------------------------
Postgres delivers NOTIFY messages at COMMIT time. We could ``execute("NOTIFY ...")``
inside the transaction and let Postgres flush on commit — but the SQL ``NOTIFY``
command does NOT accept bind placeholders for either operand (channel or
payload), so every caller-controlled value would have to be inlined into the
SQL string. That is an injection footgun. We use a dedicated autocommit
connection that calls the ``pg_notify(text, text)`` *function* (cf.
:func:`emit_notify` — ``SELECT pg_notify(%s, %s)``) which DOES accept
parameters, giving us safe interpolation for the payload while keeping the
constant channel identifier explicit.
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

import psycopg
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.shared.config import settings
from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.event_bus.exceptions import MissingCorrelationIdError
from agentive_backend.shared.event_bus.metrics import (
    EVENT_BUS_NOTIFY_LATENCY,
    EVENT_BUS_PUBLISH_LATENCY,
)
from agentive_backend.shared.event_bus.naming import validate_event_type
from agentive_backend.shared.logging import get_logger

# Single hardcoded channel — keeping it constant avoids any need to interpolate
# user input into the LISTEN / NOTIFY identifier (which cannot be parameterized).
OUTBOX_CHANNEL = "agentive_outbox"

# Safety net : reject channel-like noise even if someone hardcodes a typo.
_VALID_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
assert _VALID_CHANNEL_RE.match(OUTBOX_CHANNEL), "OUTBOX_CHANNEL must be a safe identifier"

_log = get_logger(__name__)


def _psycopg_dsn() -> str:
    """Strip the SQLAlchemy ``+psycopg`` dialect marker for raw psycopg use."""
    raw = str(settings.database_url)
    return raw.replace("postgresql+psycopg://", "postgresql://", 1)


def _resolve_correlation_id(explicit: UUID | None) -> UUID:
    if explicit is not None:
        return explicit
    cid_str = get_correlation_id()
    if cid_str is None:
        raise MissingCorrelationIdError(
            "publish() called without a bound correlation_id. "
            "Pass correlation_id=UUID(...) explicitly when publishing outside an HTTP request."
        )
    # P20 — wrap the parse so callers always get a typed domain error
    # rather than an opaque ValueError from uuid.UUID().
    try:
        return UUID(cid_str)
    except ValueError as exc:
        raise MissingCorrelationIdError(
            f"correlation_id ContextVar holds a malformed UUID: {cid_str!r}"
        ) from exc


def _serialize_payload(payload: dict[str, Any] | BaseModel) -> dict[str, Any]:
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    # P26 — strict round-trip via JSON. Any non-JSON-native value (Decimal,
    # bytes, datetime, UUID, custom objects) raises TypeError immediately
    # rather than silently coercing to ``str(value)`` — coercion would
    # corrupt the payload at rest and downstream handlers would receive
    # surprise strings instead of structured data.
    json.dumps(payload)
    return payload


async def publish(
    event_type: str,
    payload: dict[str, Any] | BaseModel,
    *,
    session: AsyncSession,
    correlation_id: UUID | None = None,
) -> UUID:
    """INSERT an event into ``outbox_events`` within the caller's transaction.

    Caller SHOULD commit the session and then call :func:`emit_notify` for
    prompt delivery (P14). The worker's poll fallback (5 s default) covers
    missed NOTIFYs eventually, but skipping the explicit notify trades
    immediate dispatch for poll-interval latency. If the transaction rolls
    back, the row never appears and no NOTIFY is emitted — that's the
    atomicity guarantee.
    """
    validate_event_type(event_type)
    cid = _resolve_correlation_id(correlation_id)
    payload_dict = _serialize_payload(payload)
    event_id = uuid4()

    with EVENT_BUS_PUBLISH_LATENCY.labels(event_type=event_type).time():
        await session.execute(
            text(
                "INSERT INTO outbox_events (id, correlation_id, event_type, payload) "
                "VALUES (:id, :cid, :etype, CAST(:payload AS JSONB))"
            ),
            {
                "id": str(event_id),
                "cid": str(cid),
                "etype": event_type,
                "payload": json.dumps(payload_dict),
            },
        )

    _log.info(
        "event_bus.publish",
        event_id=str(event_id),
        event_type=event_type,
        correlation_id=str(cid),
    )
    return event_id


async def emit_notify(event_id: UUID, event_type: str) -> None:
    """Emit ``NOTIFY agentive_outbox`` on a fresh autocommit connection.

    Cannot reuse the SQLAlchemy session pool — NOTIFY must execute outside an
    open transaction (otherwise it queues until commit, which has already
    happened by the time the caller invokes this). Cannot reuse the worker's
    listener connection either — it is blocked inside ``notifies()``.

    Connection cost (~5-10 ms per call) is acceptable for the MVP target of
    < 100 events/s. Sprint 4+ scale-out: pooled autocommit connections.
    """
    message = f"{event_id}:{event_type}"
    # `NOTIFY <channel>, '<msg>'` (the SQL command) does NOT accept bind
    # placeholders for either operand. We use the `pg_notify(text, text)`
    # function, which does — the channel string is a constant validated at
    # module load, the message is parameterized to defeat injection.
    with EVENT_BUS_NOTIFY_LATENCY.labels(event_type=event_type).time():
        async with await psycopg.AsyncConnection.connect(_psycopg_dsn(), autocommit=True) as conn:
            await conn.execute("SELECT pg_notify(%s, %s)", (OUTBOX_CHANNEL, message))


async def notify_best_effort(event_id: UUID, event_type: str) -> None:
    """:func:`emit_notify` wrapped in the bus resilience policy (audit A-06).

    A NOTIFY failure after a committed outbox INSERT must never propagate:
    the row is durable and the worker's poll fallback (5s default) will
    pick it up — re-raising would push callers to retry, i.e. duplicate
    events. Before this helper the ``try/except + warning`` block was
    copy-pasted at 8 call sites across m2/m5/m7 ; the policy now has ONE
    owner.
    """
    try:
        await emit_notify(event_id, event_type)
    except Exception:
        _log.warning(
            "event_bus_notify_failed_will_be_polled",
            event_id=str(event_id),
            event_type=event_type,
        )


async def publish_and_commit(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any] | BaseModel,
    *,
    correlation_id: UUID | None = None,
) -> UUID:
    """Convenience wrapper — INSERT, commit, then NOTIFY.

    Use this when the caller owns the session exclusively and the publication
    is the only operation in the transaction. For multi-write transactions
    that need atomicity, call :func:`publish` then commit + :func:`emit_notify`
    explicitly.

    NOTIFY failures (P4) after a successful commit do **not** propagate — the
    row is durably committed and the worker's poll fallback (5s default) will
    pick it up. Re-raising would push callers to retry, which is the worst
    outcome (duplicate events). The failure is logged loudly so operators can
    diagnose persistent NOTIFY issues.
    """
    event_id = await publish(event_type, payload, session=session, correlation_id=correlation_id)
    await session.commit()
    await notify_best_effort(event_id, event_type)
    return event_id
