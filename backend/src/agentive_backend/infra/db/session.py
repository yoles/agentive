"""SQLAlchemy async session factory.

This module is the ONLY place outside of ``shared.repositories`` where
``AsyncSession`` may be instantiated. All feature code uses repositories.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agentive_backend.shared.config import settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Lazy-initialize the global AsyncEngine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            str(settings.database_url),
            pool_pre_ping=True,
            echo=settings.is_development and settings.log_level == "DEBUG",
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Lazy-initialize the global session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — yields an AsyncSession, closes it after use."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
