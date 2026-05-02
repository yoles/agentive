"""Startup / shutdown hooks for the FastAPI app."""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import FastAPI

from agentive_backend.infra.db.session import get_session_factory
from agentive_backend.infra.llm import AnthropicProvider, OpenAIProvider
from agentive_backend.shared.config import settings
from agentive_backend.shared.contracts.events import (
    SystemShutdownEvent,
    SystemStartedEvent,
)
from agentive_backend.shared.correlation import _correlation_id_var, new_correlation_id
from agentive_backend.shared.event_bus import OutboxWorker, publish_and_commit
from agentive_backend.shared.llm import Completion, FallbackCallback, FallbackContext, LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.logging import configure_logging, get_logger

log = get_logger(__name__)


def _build_llm_router(*, on_fallback: FallbackCallback | None = None) -> LLMRouter:
    """Build the singleton :class:`LLMRouter` for the process.

    Decision matrix
    ---------------
    * Both keys absent + ``environment == "production"`` → :class:`RuntimeError`.
      A prod deployment without LLM access is almost certainly a misconfig.
    * Both keys absent + dev/test → wire a :class:`MockProvider` so the
      app boots locally without secrets.
    * Anthropic only / OpenAI only → wire the configured provider as the
      sole entry of the chain. Fallback gracefully degrades to "no
      fallback" rather than failing the boot.
    * Both keys present → canonical Sprint 0 chain ``["anthropic", "openai"]``.

    The ``on_fallback`` kwarg accepts the bus-publishing callback wired
    by :func:`lifespan` once the session factory is available — passing
    it here avoids the previous private-attribute mutation
    (``llm_router._on_fallback = ...``) and the race window between
    ``app.state.llm_router = ...`` and that mutation (review fix-batch P3).
    """
    anthropic_key = settings.anthropic_api_key
    openai_key = settings.openai_api_key

    has_anthropic = anthropic_key is not None and anthropic_key.get_secret_value()
    has_openai = openai_key is not None and openai_key.get_secret_value()

    if not has_anthropic and not has_openai:
        if settings.is_production:
            raise RuntimeError(
                "Refusing to boot in production without any LLM provider "
                "configured. Set ANTHROPIC_API_KEY and/or OPENAI_API_KEY."
            )
        log.warning(
            "llm_router.built_with_mock",
            reason="no_api_keys_configured",
            environment=settings.environment,
        )
        # P20 — synthesize an UNBOUNDED mock that always returns the same
        # placeholder response. The previous list of 1024 pre-built
        # Completions caused the dev provider to silently start raising
        # ``ValueError`` (= fatal) after ~1024 dev calls.
        placeholder = Completion(
            text="[mock] no provider configured",
            model="mock-model",
            provider="mock",
            input_tokens=0,
            output_tokens=10,
            finish_reason="stop",
            latency_ms=1.0,
            cost_estimate_usd=Decimal("0"),
        )
        mock = MockProvider("mock", [], infinite_default=placeholder)
        return LLMRouter(
            providers={"mock": mock},
            default_chain=["mock"],
            on_fallback=on_fallback,
        )

    providers: dict[str, Any] = {}
    chain: list[str] = []
    if has_anthropic:
        assert anthropic_key is not None
        providers["anthropic"] = AnthropicProvider(api_key=anthropic_key)
        chain.append("anthropic")
    if has_openai:
        assert openai_key is not None
        providers["openai"] = OpenAIProvider(api_key=openai_key)
        chain.append("openai")

    log.info(
        "llm_router.built",
        providers=list(providers),
        default_chain=chain,
        environment=settings.environment,
    )
    return LLMRouter(
        providers=providers,
        default_chain=chain,
        on_fallback=on_fallback,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan : logging config, event-bus worker, lifecycle events."""
    configure_logging()
    log.info("agentive_startup", version=app.version)
    started_monotonic = time.monotonic()

    # Build the session factory FIRST so the LLM fallback callback can
    # close over it. The callback is wired into the LLMRouter at
    # construction time (no post-construction private-attribute mutation).
    session_factory = get_session_factory()

    async def _publish_fallback(ctx: FallbackContext) -> None:
        """Publish ``m3.llm.fallback_triggered`` on the event bus.

        Captures the per-call ``correlation_id`` from the FallbackContext
        (router populates it from the structlog ContextVar at fallback
        time) and binds it on this publish — without binding,
        ``publish_and_commit`` would raise ``MissingCorrelationIdError``
        in non-HTTP code paths (review fix-batch P8).
        """
        # Bind a correlation_id on this background task. If the router
        # captured an unbound ContextVar earlier the value is the literal
        # ``"unbound"`` — use a fresh UUID instead so the audit chain is
        # still meaningful.
        try:
            from uuid import UUID

            cid: str | UUID = ctx.correlation_id
            if cid == "unbound":
                cid = new_correlation_id()
            else:
                # Best-effort UUID parse — fall back to the raw string if
                # somebody pushes a non-UUID id (e.g. external trace id).
                with contextlib.suppress(ValueError):
                    cid = UUID(str(cid))

            token = _correlation_id_var.set(str(cid))
            try:
                async with session_factory() as session:
                    await publish_and_commit(
                        session,
                        "m3.llm.fallback_triggered",
                        {
                            "failed_provider": ctx.failed_provider,
                            "next_provider": ctx.next_provider,
                            "error_class": ctx.error_class,
                            "error_type": ctx.error_type,
                            "model_attempted": ctx.model_attempted,
                            "model_fallback": ctx.model_fallback,
                            "correlation_id": str(cid),
                        },
                    )
            finally:
                _correlation_id_var.reset(token)
        except Exception:
            # P39 — keep the warning informative: include the
            # failed_provider so operators can correlate with router
            # metrics, and use exception-info logging.
            log.exception(
                "llm_fallback_event_publish_failed",
                failed_provider=ctx.failed_provider,
                next_provider=ctx.next_provider,
            )

    # Build the LLM router with the callback wired at construction —
    # eliminates the race window where requests landing between
    # ``app.state.llm_router = ...`` and ``llm_router._on_fallback = ...``
    # would silently no-op the fallback event.
    llm_router = _build_llm_router(on_fallback=_publish_fallback)
    app.state.llm_router = llm_router

    # Background tasks need an explicit correlation_id — there is no HTTP
    # request to inherit from, so the middleware never runs at startup.
    # We bind via a token that is reset before yielding so the value never
    # leaks into the first HTTP request handler (P2).
    startup_token = _correlation_id_var.set(new_correlation_id())

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


__all__ = ["_build_llm_router", "lifespan"]
