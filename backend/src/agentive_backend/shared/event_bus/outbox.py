"""OutboxWorker — consumer + replay over a dedicated psycopg connection.

Lifecycle
---------
``start()`` performs a synchronous *replay phase* (process any rows with
``processed_at IS NULL`` left over from previous runs), then spawns the
*listen task* that consumes ``NOTIFY agentive_outbox`` messages. The listener
also runs a periodic *poll fallback* (re-scan every ``poll_interval_s``) so
events whose NOTIFY was lost (publisher crash post-INSERT pre-NOTIFY, listener
reconnect window) are still drained.

``stop()`` cancels the listener task, ``UNLISTEN``-s, and closes the dedicated
connection. Idempotent.

Concurrency invariants
----------------------
- Exactly one worker per process. Multi-worker scenarios are out of scope for
  Sprint 0 (cf. ``event-bus-migration-trigger.md``). ``start()`` enforces this
  by raising :class:`OutboxWorkerNotRunningError` on a second call.
- The listener connection is dedicated and never shared with the SQLAlchemy
  pool. Sharing breaks ``execute()`` because ``notifies()`` parks the connection
  in wait-for-notify state (psycopg issue #340).
- Marking ``processed_at`` uses the guard ``WHERE id = :id AND processed_at IS NULL``
  so concurrent NOTIFY + poll races never overwrite an existing timestamp.

Resilience
----------
- **Listener reconnect** (P1): the listen loop catches ``psycopg.OperationalError``
  (DB restart, network blip, idle timeout) and reconnects with exponential
  backoff (1s → 5s → 30s, capped). Documented in the spec Dev Notes Edge case #3.
- **Handler timeout** (P8): each handler invocation is wrapped in
  ``asyncio.wait_for(timeout=_HANDLER_TIMEOUT_S)`` so a hung handler can't
  freeze the worker or block ``stop()``.
- **Poison-event cap** (P7): per-event-id in-memory attempt counter; after
  ``_MAX_HANDLER_ATTEMPTS`` failures the event is force-marked ``processed_at``
  with a ``poison`` log to break the retry storm. The full DLQ design lands
  Story 7.6 (monitoring qualité).
- **Correlation reset** (P2): ``_dispatch`` saves the ContextVar token and
  resets it in ``finally`` so handler correlation_ids never bleed into
  subsequent dispatches or sibling tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.correlation import _correlation_id_var
from agentive_backend.shared.event_bus.exceptions import OutboxWorkerNotRunningError
from agentive_backend.shared.event_bus.metrics import (
    EVENT_BUS_HANDLER_DURATION,
    EVENT_BUS_HANDLER_FAILURES_TOTAL,
    EVENT_BUS_OUTBOX_BACKLOG,
)
from agentive_backend.shared.event_bus.publisher import OUTBOX_CHANNEL, _psycopg_dsn
from agentive_backend.shared.event_bus.subscriber import _match_subscriptions
from agentive_backend.shared.event_bus.types import Event
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

# Replay batch size — keeps each transaction short to avoid long locks on the
# outbox table during a large backlog drain.
_REPLAY_BATCH_SIZE = 100

# Backlog gauge refresh cadence.
_BACKLOG_REFRESH_S = 30.0

# Per-handler timeout (P8) — a hung handler must not freeze the worker.
# 30 s is generous enough for any reasonable I/O-bound handler ; CPU-bound
# work over that should be moved to a queue (cf. M11 scheduler).
_HANDLER_TIMEOUT_S = 30.0

# Listener reconnect backoff (P1) — exponential, capped.
_RECONNECT_BACKOFF_S: tuple[float, ...] = (1.0, 5.0, 15.0, 30.0)

# Poison-event cap (P7) — after this many handler failures for the same
# event_id, the row is force-marked processed to break the retry storm.
# Full DLQ comes Story 7.6 ; this is a circuit breaker for Sprint 0.
_MAX_HANDLER_ATTEMPTS = 5


class OutboxWorker:
    """Consume outbox events via LISTEN/NOTIFY + periodic poll fallback."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        channel: str = OUTBOX_CHANNEL,
        poll_interval_s: float = 5.0,
        handler_timeout_s: float = _HANDLER_TIMEOUT_S,
        max_handler_attempts: int = _MAX_HANDLER_ATTEMPTS,
    ) -> None:
        self._session_factory = session_factory
        self._channel = channel
        self._poll_interval = poll_interval_s
        self._handler_timeout = handler_timeout_s
        self._max_attempts = max_handler_attempts
        self._listen_task: asyncio.Task[None] | None = None
        self._backlog_task: asyncio.Task[None] | None = None
        self._listen_conn: psycopg.AsyncConnection | None = None
        self._stopping = asyncio.Event()
        self._started = False
        # In-memory poison-event counter (P7). Cleared on stop().
        self._handler_attempts: dict[UUID, int] = {}

    # ─────────────────────────── lifecycle ───────────────────────────

    async def start(self) -> None:
        """Run replay then start the listen + backlog gauge tasks.

        Not idempotent (P6) — calling twice raises so a misuse can't leak the
        prior listener connection or spawn duplicate dispatch tasks.
        """
        if self._started:
            raise OutboxWorkerNotRunningError(
                "OutboxWorker.start() called twice; call stop() first if restarting."
            )
        self._started = True

        try:
            await self._replay_unprocessed()
            self._listen_conn = await self._open_listen_connection()
        except Exception:
            # Roll back the started flag so the caller can retry.
            self._started = False
            raise

        _log.info("event_bus.outbox_worker_started", channel=self._channel)
        self._stopping.clear()
        self._listen_task = asyncio.create_task(self._listen_loop(), name="event-bus-listen")
        self._backlog_task = asyncio.create_task(self._backlog_loop(), name="event-bus-backlog")

    async def stop(self) -> None:
        """Cancel tasks, UNLISTEN, close dedicated connection. Idempotent."""
        self._stopping.set()

        for task in (self._listen_task, self._backlog_task):
            if task is None or task.done():
                continue
            task.cancel()
            # P17 — narrower suppress: cancellation is expected; surface any
            # OTHER exception via log.exception so programmer errors during
            # shutdown aren't swallowed silently.
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.exception("event_bus.task_shutdown_error", task=task.get_name())

        if self._listen_conn is not None:
            with contextlib.suppress(Exception):
                await self._listen_conn.execute(f"UNLISTEN {self._channel}")
            with contextlib.suppress(Exception):
                await self._listen_conn.close()
            self._listen_conn = None

        self._listen_task = None
        self._backlog_task = None
        self._handler_attempts.clear()
        self._started = False
        _log.info("event_bus.outbox_worker_stopped", channel=self._channel)

    # ─────────────────────────── reconnect ───────────────────────────

    async def _open_listen_connection(self) -> psycopg.AsyncConnection:
        """Open the dedicated autocommit connection + LISTEN on our channel."""
        conn = await psycopg.AsyncConnection.connect(_psycopg_dsn(), autocommit=True)
        try:
            await conn.execute(f"LISTEN {self._channel}")
        except Exception:
            await conn.close()
            raise
        return conn

    async def _reconnect_with_backoff(self) -> None:
        """Reopen the listener with exponential backoff, honouring the stop flag."""
        for attempt, delay in enumerate(_RECONNECT_BACKOFF_S, start=1):
            if self._stopping.is_set():
                return
            _log.warning(
                "event_bus.listen_reconnect_attempt",
                attempt=attempt,
                delay_s=delay,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            if self._stopping.is_set():
                return
            try:
                self._listen_conn = await self._open_listen_connection()
                _log.info("event_bus.listen_reconnected", attempt=attempt)
                return
            except Exception:
                _log.exception("event_bus.listen_reconnect_failed", attempt=attempt)
        # Out of attempts — surface to operator via log but keep the worker
        # alive (poll fallback inside _listen_loop will keep draining).
        _log.error(
            "event_bus.listen_reconnect_exhausted",
            attempts=len(_RECONNECT_BACKOFF_S),
        )

    # ─────────────────────────── loops ───────────────────────────────

    async def _listen_loop(self) -> None:
        """Consume NOTIFY messages with reconnect-on-error + poll fallback (P1)."""
        try:
            while not self._stopping.is_set():
                if self._listen_conn is None:
                    await self._reconnect_with_backoff()
                    if self._listen_conn is None:
                        # Reconnect exhausted — fall back to pure polling and
                        # retry reconnect on the next loop turn.
                        await self._replay_unprocessed()
                        continue

                try:
                    # `timeout` makes notifies() yield None when no NOTIFY arrives,
                    # giving us a chance to drain events whose NOTIFY was lost.
                    gen = self._listen_conn.notifies(timeout=self._poll_interval)
                    try:
                        async for notify in gen:
                            if self._stopping.is_set():
                                break
                            await self._process_notify(notify)
                    finally:
                        # Closing the generator releases the wait-for-notify state.
                        await gen.aclose()
                except psycopg.OperationalError, psycopg.InterfaceError:
                    _log.exception("event_bus.listen_connection_lost")
                    with contextlib.suppress(Exception):
                        await self._listen_conn.close()
                    self._listen_conn = None
                    # Reconnect will happen at the top of the next loop turn.
                    continue
                except Exception:
                    _log.exception("event_bus.listen_iteration_failed")
                    # Don't kill the worker on a transient exception — wait one
                    # poll interval then retry.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)

                # Fallback poll on every loop turn — covers (a) timeout reached
                # with no NOTIFY, (b) publisher crashed post-INSERT pre-NOTIFY.
                if not self._stopping.is_set():
                    await self._replay_unprocessed()
        except asyncio.CancelledError:
            raise

    async def _backlog_loop(self) -> None:
        """Sample the outbox backlog gauge every ``_BACKLOG_REFRESH_S`` seconds."""
        try:
            while not self._stopping.is_set():
                try:
                    await self._refresh_backlog_gauge()
                except Exception:
                    _log.exception("event_bus.backlog_gauge_refresh_failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=_BACKLOG_REFRESH_S)
        except asyncio.CancelledError:
            raise

    # ─────────────────────────── core handlers ───────────────────────

    async def _process_notify(self, notify: Any) -> None:
        """Decode `<id>:<event_type>` and dispatch a single event."""
        payload = getattr(notify, "payload", None)
        if not payload or ":" not in payload:
            _log.warning("event_bus.notify_malformed", raw=str(payload))
            return
        event_id_str, _ = payload.split(":", 1)
        try:
            event_id = UUID(event_id_str)
        except ValueError:
            _log.warning("event_bus.notify_invalid_uuid", raw=event_id_str)
            return
        await self._process_one(event_id)

    async def _process_one(self, event_id: UUID) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT id, correlation_id, event_type, payload, "
                    "       created_at, tenant_id "
                    "FROM outbox_events WHERE id = :id AND processed_at IS NULL"
                ),
                {"id": str(event_id)},
            )
            row = result.mappings().first()
            if row is None:
                # Either already processed (race with poll) or rolled back.
                return
            event = _row_to_event(row)
            ok = await self._dispatch(event)
            if ok:
                await self._mark_processed(session, event.id)
                self._handler_attempts.pop(event.id, None)
            elif self._record_failure_and_check_poison(event.id):
                # P7 — poison cap reached. Force-mark to break the retry storm.
                await self._force_mark_poison(session, event.id, event.event_type)
            await session.commit()

    async def _replay_unprocessed(self) -> None:
        """Drain the unprocessed backlog in batches."""
        processed_count = 0
        while not self._stopping.is_set():
            async with self._session_factory() as session:
                result = await session.execute(
                    text(
                        "SELECT id, correlation_id, event_type, payload, "
                        "       created_at, tenant_id "
                        "FROM outbox_events WHERE processed_at IS NULL "
                        "ORDER BY created_at ASC LIMIT :limit"
                    ),
                    {"limit": _REPLAY_BATCH_SIZE},
                )
                rows = result.mappings().all()
                if not rows:
                    break
                for row in rows:
                    event = _row_to_event(row)
                    ok = await self._dispatch(event)
                    if ok:
                        await self._mark_processed(session, event.id)
                        self._handler_attempts.pop(event.id, None)
                        processed_count += 1
                    elif self._record_failure_and_check_poison(event.id):
                        await self._force_mark_poison(session, event.id, event.event_type)
                await session.commit()
            if len(rows) < _REPLAY_BATCH_SIZE:
                break
        if processed_count:
            _log.info("event_bus.replay_completed", count=processed_count)

    async def _dispatch(self, event: Event) -> bool:
        """Invoke all matching handlers; return True iff every call succeeded.

        Each handler invocation is wrapped in :func:`asyncio.wait_for` (P8) to
        bound dispatch latency. The correlation_id ContextVar is bound for the
        duration of the dispatch and reset on exit (P2) so it doesn't bleed
        into the next event or sibling tasks.
        """
        token = _correlation_id_var.set(str(event.correlation_id))
        try:
            subs = _match_subscriptions(event.event_type)
            all_ok = True
            for sub in subs:
                handler = sub.handler
                if handler is None:
                    continue
                handler_module = getattr(handler, "__module__", "unknown")
                try:
                    with EVENT_BUS_HANDLER_DURATION.labels(
                        event_type=event.event_type, handler_module=handler_module
                    ).time():
                        await asyncio.wait_for(handler(event), timeout=self._handler_timeout)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    all_ok = False
                    EVENT_BUS_HANDLER_FAILURES_TOTAL.labels(
                        event_type=event.event_type,
                        handler_module=handler_module,
                        error_type=type(exc).__name__,
                    ).inc()
                    _log.exception(
                        "event_bus.handler_failed",
                        event_id=str(event.id),
                        event_type=event.event_type,
                        handler_module=handler_module,
                    )
            _log.info(
                "event_bus.event_dispatched",
                event_id=str(event.id),
                event_type=event.event_type,
                handler_count=len(subs),
                ok=all_ok,
            )
            return all_ok
        finally:
            _correlation_id_var.reset(token)

    def _record_failure_and_check_poison(self, event_id: UUID) -> bool:
        """Increment the per-event attempt counter; return True if cap reached.

        The counter lives in process memory (cleared on ``stop()``). On
        process restart the counter resets — at worst this means a poison
        event triggers the same N handler failures one more time before being
        force-marked. Acceptable for Sprint 0 (cf docstring P7).
        """
        attempts = self._handler_attempts.get(event_id, 0) + 1
        self._handler_attempts[event_id] = attempts
        if attempts >= self._max_attempts:
            _log.error(
                "event_bus.poison_event_capped",
                event_id=str(event_id),
                attempts=attempts,
                max_attempts=self._max_attempts,
            )
            return True
        return False

    async def _mark_processed(self, session: AsyncSession, event_id: UUID) -> None:
        result = await session.execute(
            text(
                "UPDATE outbox_events SET processed_at = NOW() "
                "WHERE id = :id AND processed_at IS NULL"
            ),
            {"id": str(event_id)},
        )
        # P23 — surface concurrent dispatch races. Idempotent UPDATE: 0
        # rowcount means another worker / poll already processed this event.
        # We log a warning so duplicate handler invocations aren't invisible.
        # `rowcount` is exposed by SQLAlchemy CursorResult (DML execution).
        rowcount = getattr(result, "rowcount", None)
        if rowcount == 0:
            _log.warning(
                "event_bus.duplicate_dispatch_detected",
                event_id=str(event_id),
                note="processed_at was already set; handler may have run twice. "
                "Handlers MUST be idempotent (cf AC3).",
            )

    async def _force_mark_poison(
        self, session: AsyncSession, event_id: UUID, event_type: str
    ) -> None:
        """Force ``processed_at`` for a poison event; log critically (P7)."""
        await self._mark_processed(session, event_id)
        self._handler_attempts.pop(event_id, None)
        _log.critical(
            "event_bus.poison_event_force_marked",
            event_id=str(event_id),
            event_type=event_type,
            note="Handler failures exhausted retry cap; row marked processed to break loop. "
            "Investigate the handler and replay manually if needed.",
        )

    async def _refresh_backlog_gauge(self) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM outbox_events WHERE processed_at IS NULL")
            )
            count = result.scalar_one()
            EVENT_BUS_OUTBOX_BACKLOG.set(float(count))


def _row_to_event(row: Any) -> Event:
    """Convert a SQLAlchemy ``RowMapping`` into our immutable :class:`Event`.

    P10 — defensive: psycopg deserializes JSONB to ``dict`` in the common
    case, but a legacy column type or driver upgrade could surface a list /
    scalar / non-mapping value. We coerce to ``dict`` only when safe and
    raise a typed error otherwise so the dispatch loop's ``except Exception``
    catches it instead of leaving the row stuck.
    """
    payload = row["payload"]
    if not isinstance(payload, dict):
        try:
            payload = dict(payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"outbox_events.payload not coercible to dict: {type(payload).__name__}"
            ) from exc

    created_at = row["created_at"]
    if not isinstance(created_at, datetime):
        try:
            created_at = datetime.fromisoformat(str(created_at))
        except ValueError as exc:
            raise ValueError(f"outbox_events.created_at unreadable: {created_at!r}") from exc

    return Event(
        id=row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"])),
        correlation_id=row["correlation_id"]
        if isinstance(row["correlation_id"], UUID)
        else UUID(str(row["correlation_id"])),
        event_type=row["event_type"],
        payload=payload,
        created_at=created_at,
        tenant_id=row["tenant_id"],
    )
