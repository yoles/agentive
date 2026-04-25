"""Fixtures partagées pour les tests Story 1.2 (spike M3 LangGraph).

Réutilise la fixture ``postgres_container`` (testcontainers) définie dans
``tests/conftest.py`` pour stand-up un Postgres + pgvector éphémère, puis
expose un ``checkpoint_dsn`` (URL ``postgresql://``) compatible
:class:`AsyncPostgresSaver` (LangGraph 1.1.8).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest


@pytest.fixture
def checkpoint_dsn(postgres_container: object) -> Iterator[str]:
    """Expose un DSN ``postgresql://...`` (sans ``+psycopg``) pour LangGraph.

    Surcharge ``SPIKE_DATABASE_URL`` pendant la durée du test pour que
    :func:`spike.m3_langgraph._checkpoint_dsn` retourne le DSN testcontainer.
    """
    url = postgres_container.get_connection_url()  # type: ignore[attr-defined]
    # testcontainers retourne `postgresql+psycopg2://` (driver SQLAlchemy v1) —
    # on convertit en scheme canonique attendu par LangGraph / psycopg3.
    canonical = (
        str(url)
        .replace("postgresql+psycopg2://", "postgresql://", 1)
        .replace("postgresql+psycopg://", "postgresql://", 1)
    )
    previous = os.environ.get("SPIKE_DATABASE_URL")
    os.environ["SPIKE_DATABASE_URL"] = canonical
    try:
        yield canonical
    finally:
        if previous is None:
            os.environ.pop("SPIKE_DATABASE_URL", None)
        else:
            os.environ["SPIKE_DATABASE_URL"] = previous


@pytest.fixture
async def checkpointer(checkpoint_dsn: str) -> AsyncIterator[object]:
    """Instance :class:`AsyncPostgresSaver` prête (tables setup) pour un test."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn) as saver:
        await saver.setup()
        yield saver


@pytest.fixture
def force_mock_llm(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Garantit qu'aucun appel réseau LLM ne part en CI (force MockLLM).

    Le ``settings`` Pydantic est instancié une fois au boot (``lru_cache`` sur
    ``get_settings``), donc retirer ``ANTHROPIC_API_KEY`` de ``os.environ`` est
    inopérant — il faut patcher l'attribut directement sur l'objet ``settings``
    déjà importé par :mod:`spike.m3_langgraph`. Pytest ``monkeypatch`` restaure
    automatiquement la valeur initiale en teardown.
    """
    from agentive_backend.shared.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", None)
    # Aussi vider l'env pour les subprocess que les tests pourraient spawner.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    yield
