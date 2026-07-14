"""Repository pattern base class.

The :class:`BaseRepo` exposes a single canonical entry point — the
:meth:`with_tenant` async context manager — that every concrete repository
**must** use to open transactions. The context manager performs two duties:

1. Yields an :class:`~sqlalchemy.ext.asyncio.AsyncSession` from the
   repository's session factory.
2. If a non-``None`` ``tenant_id`` is provided, executes
   ``SET LOCAL app.tenant_id = <uuid>`` as the first statement of the
   transaction so the Postgres RLS policy ``tenant_isolation`` (declared in
   ``backend/alembic/versions/20260419_000000_initial.py:457-468``) takes
   effect for the remainder of the transaction.

The single-tenant MVP passes ``tenant_id=None`` everywhere — the policy
clause ``tenant_id IS NULL OR tenant_id = current_setting(...)`` matches all
global rows. When Sprint 4+ Growth multi-tenant arrives, callers start
passing tenant UUIDs without any repository-side change.

``SET LOCAL`` is transaction-scoped and reset on commit/rollback — the
binding never leaks to the next transaction in the connection pool.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TypeVar
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.exceptions import NotFoundError

_E = TypeVar("_E")


class BaseRepo:
    """Public API surface for repository transactions. ALL DB access must go through subclasses of this class.

    Sub-classes inherit :meth:`with_tenant` and must use it for every DB
    operation. They never expose :class:`AsyncSession` in their public
    signatures.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _require_found(
        entity: _E | None,
        *,
        label: str,
        entity_id: object,
        context_key: str,
    ) -> _E:
        """Return ``entity`` unchanged, or raise :class:`NotFoundError` if it is ``None``.

        The single source of the lookup-or-404 pattern (audit A-07). Every
        concrete repo's ``require_by_id`` / ``require_by_id_in_session`` routes
        through this helper so the RFC 7807 message and ``context`` shape are
        identical across the codebase (``"<Label> '<id>' not found"`` +
        ``{context_key: str(entity_id)}``), instead of being re-spelled at each
        of the ~12 former call sites in the service layer.
        """
        if entity is None:
            raise NotFoundError(
                detail=f"{label} '{entity_id}' not found",
                context={context_key: str(entity_id)},
            )
        return entity

    @asynccontextmanager
    async def with_tenant(self, tenant_id: UUID | None) -> AsyncIterator[AsyncSession]:
        """Open a transaction-scoped session bound to ``tenant_id``.

        Args:
            tenant_id: UUID of the tenant to bind, or ``None`` for the
                single-tenant MVP path (the RLS policy matches global rows
                with ``tenant_id IS NULL``).

        Yields:
            The :class:`AsyncSession` ready for use. The transaction is
            committed on successful exit, rolled back if the body raises.

        Note:
            ``SET LOCAL`` is transaction-scoped — committing inside the
            ``async with`` body resets the binding. Don't call
            ``session.commit()`` manually unless you understand the impact.
        """
        async with self._session_factory() as session:
            try:
                if tenant_id is not None:
                    # Postgres SET LOCAL doesn't accept bind placeholders for
                    # its value (see Story 1.4 Debug Log #1 for the analogous
                    # NOTIFY case). Use set_config(setting, value, is_local)
                    # which is parameter-safe and equivalent to SET LOCAL.
                    await session.execute(
                        text("SELECT set_config('app.tenant_id', :tid, true)"),
                        {"tid": str(tenant_id)},
                    )
                yield session
            except BaseException:
                # Body raised (incl. ``asyncio.CancelledError``) — best-effort
                # rollback before re-raising. Catching ``BaseException`` is
                # required so task cancellation still releases the transaction
                # promptly; if the rollback itself errors the outer
                # ``async with`` will dispose the session in ``__aexit__``.
                with suppress(Exception):
                    await session.rollback()
                raise
            else:
                # Body completed without raising → commit. If commit raises
                # (constraint violation, deferred FK, lost connection) we
                # propagate directly: SQLAlchemy already invalidated the
                # transaction on commit failure, so a redundant rollback
                # would only emit "transaction already deassociated" noise.
                await session.commit()
