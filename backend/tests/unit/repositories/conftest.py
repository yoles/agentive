"""Shared async-mock helpers for repository unit tests.

The integration suite under ``tests/integration/repositories/`` is what
actually exercises Postgres + RLS + the migration-defined schema. The unit
tests in this package cover the *call graph* — that each repo method opens
a session via :meth:`BaseRepo.with_tenant`, performs the expected
``execute``/``add``/``flush`` calls, and propagates the right return type.

The mock helper here is deliberately small: most repo methods do nothing
beyond instantiating a model and dispatching to the session. A heavier
mock would just invert the test pyramid and fight the integration suite
for ownership of the real assertions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock


def make_session_factory_mock() -> tuple[Callable[..., Any], AsyncMock]:
    """Return a pair (factory, session_mock) usable as ``BaseRepo``'s factory.

    Calling ``factory()`` returns an async context manager whose
    ``__aenter__`` yields ``session_mock``. ``session_mock.execute`` is an
    ``AsyncMock`` that returns a ``MagicMock`` — tests can override its
    ``scalar_one_or_none``/``scalars().all()`` return values per case.
    """
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    session.get = AsyncMock(return_value=None)
    session.execute = AsyncMock(return_value=MagicMock())

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=cm)
    return factory, session
