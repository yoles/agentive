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
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.exceptions import DependencyError, NotFoundError

_E = TypeVar("_E")

# Postgres ``lock_not_available`` — raised when a statement's wait for a row
# lock exceeds ``lock_timeout`` (Story 4.14 AC2).
#
# Deliberately NOT extended to ``57014 query_canceled``: that is
# ``statement_timeout``, which this repo never sets (grepped — no
# ``statement_timeout`` anywhere in ``backend/src``, ``infra/`` or the compose
# files) and which can fire for a slow query that was never waiting on a lock
# at all. Translating it would report "the lock timed out, retry me" for an
# unrelated fault. Known deployment constraint, documented in
# ``docs/runbooks/repositories-usage.md``: if an operator sets a role-wide or
# pooler-side ``statement_timeout`` BELOW the lock timeout, it pre-empts this
# GUC and the caller sees the untyped error instead of the typed 503.
LOCK_NOT_AVAILABLE_SQLSTATE = "55P03"


def is_lock_timeout(exc: OperationalError) -> bool:
    """True only for a ``lock_timeout`` expiry, never any other
    ``OperationalError`` (connection loss, admin shutdown, ...).

    The caller turns this into a specific, typed 503, so a blanket
    ``except OperationalError`` would misreport a connection drop as "the
    lock timed out, retry me" instead of surfacing the real fault.

    ``exc.orig.sqlstate`` is psycopg3's spelling (this repo pins
    ``psycopg[binary]>=3.2.10``). ``getattr`` with a ``None`` default means a
    driver exposing neither yields ``False`` — the conservative answer, since
    being wrong that way surfaces the real error rather than hiding it behind
    a 503.
    """
    return getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE_SQLSTATE


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
    async def with_tenant(
        self, tenant_id: UUID | None, *, lock_timeout_ms: int | None = None
    ) -> AsyncIterator[AsyncSession]:
        """Open a transaction-scoped session bound to ``tenant_id``.

        Args:
            tenant_id: UUID of the tenant to bind, or ``None`` for the
                single-tenant MVP path (the RLS policy matches global rows
                with ``tenant_id IS NULL``).
            lock_timeout_ms: Story 4.14 AC2 — when given, bounds how long
                THIS transaction alone will wait on a row lock (``SET LOCAL
                lock_timeout``) before Postgres raises ``lock_not_available``
                (SQLSTATE ``55P03``). Must be strictly positive: Postgres
                reads ``lock_timeout = 0`` as *disabled*, so a zero here
                would silently restore the unbounded wait the argument
                exists to remove — a ``ValueError`` is raised instead of
                forwarding it. ``None`` (the
                default) leaves the session-wide/role-wide setting
                untouched — deliberately opt-in per call site rather than a
                blanket change here, since a repository-wide default would
                be a behaviour change for every query every feature issues,
                not a decision one caller gets to make for everyone.
                Callers that need it: a transaction that holds ``FOR SHARE``
                for its own duration, where an unbounded wait on a
                downstream conflict (e.g. a unique-index collision) would
                also hold that lock unbounded (``create_workflow``, Story
                4.8 AC1+AC2).

        Yields:
            The :class:`AsyncSession` ready for use. The transaction is
            committed on successful exit, rolled back if the body raises.

        Raises:
            ValueError: ``lock_timeout_ms`` is not strictly positive.
            DependencyError: a statement in the body hit the ``lock_timeout``
                this call asked for. Translated HERE, at the boundary, and
                not at one repo method, because ``SET LOCAL`` is
                **transaction-scoped**: the bound applies to every statement
                in the body, so every statement can raise it. A per-method
                handler covers the one statement its author had in mind and
                lets the others escape as raw ``sqlalchemy`` exceptions —
                which is exactly what happened to ``create_workflow``'s
                ``SELECT ... FOR SHARE`` (review 4.14, finding 1). Callers
                that want a more specific message still catch it closer to
                the statement and re-raise; this is the floor, not a ceiling.

        Note:
            ``SET LOCAL`` is transaction-scoped — committing inside the
            ``async with`` body resets the binding. Don't call
            ``session.commit()`` manually unless you understand the impact.
        """
        if lock_timeout_ms is not None and lock_timeout_ms <= 0:
            raise ValueError(
                f"lock_timeout_ms must be strictly positive, got {lock_timeout_ms}. "
                "Postgres reads `lock_timeout = 0` as DISABLED, so forwarding it "
                "would silently restore an unbounded wait; a negative value raises "
                "SQLSTATE 22023 at transaction open."
            )
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
                if lock_timeout_ms is not None:
                    # Same parameter-safe mechanism as `app.tenant_id` above
                    # — `lock_timeout` is a standard Postgres GUC, settable
                    # through `set_config` like any other. The explicit `ms`
                    # suffix removes any ambiguity about the unit the string
                    # is interpreted in.
                    await session.execute(
                        text("SELECT set_config('lock_timeout', :timeout, true)"),
                        {"timeout": f"{lock_timeout_ms}ms"},
                    )
                yield session
            except OperationalError as exc:
                # The `lock_timeout` this call asked for expired on SOME
                # statement of the body — which one is not knowable here and
                # does not matter to the caller. Roll back first (same
                # best-effort discipline as the `BaseException` arm below),
                # then hand back the typed 503 instead of a raw driver
                # exception. Guarded on `lock_timeout_ms is not None` so a
                # transaction that never asked for a bound keeps propagating
                # `OperationalError` untouched: a 55P03 there would come from
                # a role-wide setting this code did not choose.
                with suppress(Exception):
                    await session.rollback()
                if lock_timeout_ms is None or not is_lock_timeout(exc):
                    raise
                raise DependencyError(
                    detail="Timed out waiting for a database lock",
                    context={"lock_timeout_ms": lock_timeout_ms},
                ) from exc
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
