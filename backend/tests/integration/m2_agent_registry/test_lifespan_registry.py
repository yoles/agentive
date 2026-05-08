"""Integration test — lifespan registry boot (Story 2.1 T4.2).

Story 2.1 P-04 — the original version of this file invoked
``load_registry()`` directly, never instantiating a FastAPI app or
running its lifespan, so the "lifespan" claim in the spec was a misnomer.
The rewrite uses ``TestClient`` as a context manager which fires the
ASGI lifespan startup before any request — the same code path used in
production by uvicorn.

We do NOT exercise the production ``app.lifespan.lifespan`` (which also
boots the LLM router, event-bus worker, etc., requiring DB+LLM env). We
build a minimal ``FastAPI(lifespan=...)`` whose lifespan only calls
``load_registry`` — exactly the integration surface T4.2 requires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentive_backend.features.m2_agent_registry import load_registry
from agentive_backend.features.m2_agent_registry.archetypes import EXPECTED_ARCHETYPE_IDS


def _build_app_with_registry_lifespan(schema_path: Path | None = None) -> FastAPI:
    """Minimal FastAPI app whose lifespan loads the archetype registry."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.archetype_registry = load_registry(schema_path=schema_path)
        yield

    return FastAPI(lifespan=lifespan)


def test_lifespan_populates_archetype_registry_with_8_canonical_ids() -> None:
    """T4.2 — `app.state.archetype_registry` is populated after lifespan startup."""
    app = _build_app_with_registry_lifespan()

    # Before entering the TestClient context, the lifespan has NOT run yet.
    assert (
        not hasattr(app.state, "archetype_registry")
        or getattr(app.state, "archetype_registry", None) is None
        or len(getattr(app.state, "archetype_registry", {})) == 0
    )

    with TestClient(app) as client:
        # Inside the context, the ASGI lifespan startup has run — exactly
        # like uvicorn would do in production before the first request.
        assert hasattr(app.state, "archetype_registry")
        assert set(app.state.archetype_registry.keys()) == EXPECTED_ARCHETYPE_IDS
        assert len(app.state.archetype_registry) == 8

        # Sanity: at least one round-trip works (the lifespan didn't crash).
        # (No real route registered — we just confirm the app responds 404
        # rather than exploding on lifespan errors.)
        response = client.get("/")
        assert response.status_code == 404


def test_lifespan_with_malformed_yaml_fails_fast(tmp_path: Path) -> None:
    """T4.2 — Malformed YAML at lifespan level raises RuntimeError on app boot.

    `TestClient(app).__enter__()` triggers the ASGI lifespan startup. If
    `load_registry` raises, the lifespan startup fails and `__enter__`
    propagates the exception rather than silently booting an empty app.
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text("archetypes: [not closed", encoding="utf-8")
    app = _build_app_with_registry_lifespan(schema_path=bad)

    with pytest.raises(RuntimeError, match=r"YAML parse error"), TestClient(app):
        pass  # Should never reach here — lifespan should fail.


def test_lifespan_with_partial_yaml_fails_fast(tmp_path: Path) -> None:
    """T4.2 — Partial archetype set (< 8) at lifespan level raises RuntimeError."""
    bad = tmp_path / "partial.yaml"
    bad.write_text(
        """archetypes:
  - id: producteur
    display_name: Producteur
    icon_name: wrench
    description: dummy
    default_role: producer
    prompt_base: Tu es un producteur.
    input_contract: {core: {}, extras: {}}
    output_contract: {core: {}, extras: {}}
""",
        encoding="utf-8",
    )
    app = _build_app_with_registry_lifespan(schema_path=bad)

    with pytest.raises(RuntimeError, match=r"archetype id set mismatch"), TestClient(app):
        pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Defense-in-depth: the immutable contract holds across lifespan reloads
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_lifespan_reload_yields_equivalent_registry_snapshots() -> None:
    """Booting two apps loads two independent registries with identical data."""
    app_a = _build_app_with_registry_lifespan()
    app_b = _build_app_with_registry_lifespan()

    with TestClient(app_a), TestClient(app_b):
        keys_a = set(app_a.state.archetype_registry.keys())
        keys_b = set(app_b.state.archetype_registry.keys())
        assert keys_a == keys_b == EXPECTED_ARCHETYPE_IDS
        # Identical content but different object instances (per-app load).
        for archetype_id in EXPECTED_ARCHETYPE_IDS:
            assert (
                app_a.state.archetype_registry[archetype_id].model_dump()
                == app_b.state.archetype_registry[archetype_id].model_dump()
            )
