"""Integration test — lifespan sandbox detection wiring (Story 2.6 T3.4).

Validates that the FastAPI lifespan probe `detect_sandbox_backend()` and
stores the result on `app.state.mcp_sandbox_backend`, matching what
production lifespan does. Two scenarios :

1. bwrap probe succeeds → `app.state.mcp_sandbox_backend == "bwrap"`.
2. bwrap probe fails (monkeypatched) → `setrlimit` + warning logged.

We do NOT exercise the full production lifespan (which also boots the
LLM router, event-bus worker, etc.). Instead we build a minimal FastAPI
whose lifespan only calls `detect_sandbox_backend()` — exactly the
integration surface T3.4 requires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentive_backend.infra.mcp import sandbox as sandbox_module


def _build_sandbox_lifespan_app() -> FastAPI:
    """Minimal FastAPI app whose lifespan calls detect_sandbox_backend()."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.mcp_sandbox_backend = sandbox_module.detect_sandbox_backend()
        yield

    return FastAPI(lifespan=lifespan)


def test_lifespan_stores_bwrap_when_probe_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3.4 (1) — when the bwrap binary exists AND the kernel allows
    unprivileged user namespaces, lifespan stores backend='bwrap'."""
    # P-18 (CR 2026-05-11) — clear the @functools.cache so the monkey-
    # patched probe is actually consulted (previous test runs may have
    # cached a different result).
    sandbox_module._detect_sandbox_backend_uncached.cache_clear()
    monkeypatch.setattr(
        sandbox_module.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr(sandbox_module, "_probe_bwrap_actually_works", lambda: True)

    app = _build_sandbox_lifespan_app()
    # Before lifespan runs.
    assert getattr(app.state, "mcp_sandbox_backend", None) is None
    with TestClient(app):
        # After lifespan startup.
        assert app.state.mcp_sandbox_backend == "bwrap"


def test_lifespan_falls_back_to_setrlimit_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3.4 (2) — when bwrap binary exists but the kernel rejects
    unprivileged user namespaces (Docker default seccomp), lifespan stores
    backend='setrlimit' AND a warning is logged.
    """
    sandbox_module._detect_sandbox_backend_uncached.cache_clear()
    monkeypatch.setattr(
        sandbox_module.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr(sandbox_module, "_probe_bwrap_actually_works", lambda: False)

    # Capture warning logs via structlog's underlying logger.
    warning_seen: dict[str, bool] = {"flag": False}
    original_warning = sandbox_module._log.warning

    def _spy_warning(event: str, **kwargs: object) -> None:
        if "bwrap_unavailable" in event:
            warning_seen["flag"] = True
        return original_warning(event, **kwargs)

    monkeypatch.setattr(sandbox_module._log, "warning", _spy_warning)

    app = _build_sandbox_lifespan_app()
    with TestClient(app):
        assert app.state.mcp_sandbox_backend == "setrlimit"
    assert warning_seen["flag"], (
        "Expected mcp_sandbox.bwrap_unavailable_falling_back_to_setrlimit warning"
    )
