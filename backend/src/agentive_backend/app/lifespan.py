"""Startup / shutdown hooks for the FastAPI app."""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agentive_backend.infra.db.session import get_session_factory
from agentive_backend.shared.contracts.events import (
    SystemShutdownEvent,
    SystemStartedEvent,
)
from agentive_backend.shared.correlation import _correlation_id_var, new_correlation_id
from agentive_backend.shared.event_bus import OutboxWorker, publish_and_commit
from agentive_backend.shared.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan : logging config, event-bus worker, lifecycle events."""
    configure_logging()
    log.info("agentive_startup", version=app.version)
    started_monotonic = time.monotonic()

    # Background tasks need an explicit correlation_id — there is no HTTP
    # request to inherit from, so the middleware never runs at startup.
    # We bind via a token that is reset before yielding so the value never
    # leaks into the first HTTP request handler (P2).
    startup_token = _correlation_id_var.set(new_correlation_id())

    session_factory = get_session_factory()
    worker = OutboxWorker(session_factory=session_factory)
    try:
        await worker.start()
    except Exception:
        log.exception("agentive_outbox_worker_start_failed")
        _correlation_id_var.reset(startup_token)
        raise
    app.state.outbox_worker = worker

    # Best-effort startup event — a transient DB hiccup must not prevent the
    # app from serving traffic (the worker will replay any orphaned writes
    # next time the publish path succeeds).
    try:
        async with session_factory() as session:
            await publish_and_commit(
                session,
                SystemStartedEvent.event_type,
                SystemStartedEvent(version=app.version),
            )
    except Exception:
        log.exception("agentive_startup_event_publish_failed")

    # Reset the ContextVar BEFORE yielding so the first HTTP request handler
    # gets a fresh state and the middleware's own set_correlation_id wins.
    _correlation_id_var.reset(startup_token)

    try:
        yield
    finally:
        # New correlation_id scoped to the shutdown sequence so its logs
        # cluster — reset on exit so we don't leak into post-yield Python
        # cleanup (uvicorn worker, etc.).
        shutdown_token = _correlation_id_var.set(new_correlation_id())
        try:
            uptime_seconds = time.monotonic() - started_monotonic
            try:
                async with session_factory() as session:
                    await publish_and_commit(
                        session,
                        SystemShutdownEvent.event_type,
                        SystemShutdownEvent(uptime_seconds=uptime_seconds),
                    )
            except Exception:
                log.exception("agentive_shutdown_event_publish_failed")
            with contextlib.suppress(Exception):
                await worker.stop()
            log.info("agentive_shutdown", uptime_seconds=uptime_seconds)
        finally:
            _correlation_id_var.reset(shutdown_token)
