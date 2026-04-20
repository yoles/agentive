"""Startup / shutdown hooks for the FastAPI app."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agentive_backend.shared.logging import configure_logging, get_logger

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan : logging config, warmup, graceful shutdown."""
    configure_logging()
    log.info("agentive_startup", version=app.version)
    try:
        yield
    finally:
        log.info("agentive_shutdown")
