"""Public API surface for :class:`AuditEvent` — INSERT-only, immutable.

The Postgres ``audit_events`` table grants ``INSERT`` only to the
``agentive_audit_admin`` role. ``agentive_app`` (the runtime role) has no
direct write access — production audit writes happen via a dedicated worker
that consumes the outbox and INSERTs into ``audit_events`` using the
``audit_admin`` connection.

Sprint 0 ships this repo as the **public surface** for that future worker.
The actual ``audit_admin`` connection wiring is deferred to Story 9.1
(Audit trail middleware). For now, :meth:`record` raises
``NotImplementedError`` to fail-fast if a caller invokes it before the
plumbing exists.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.infra.db.models import AuditEvent
from agentive_backend.shared.repositories.base import BaseRepo


class AuditEventRepo(BaseRepo):
    """Public API surface for AuditEvent. ALL DB access must go through this class.

    No ``update``, ``delete``, or ``get`` methods are exposed by design —
    the audit trail is immutable. Reads are reserved to the admin partition
    consumer (Story 9.1+).

    The constructor accepts an optional ``_allow_unsafe_writes`` flag that
    gates :meth:`_record_unsafe`. Production code MUST NOT pass
    ``_allow_unsafe_writes=True`` — the bypass exists only so integration
    tests provisioned with the ``agentive_test_seed`` (BYPASSRLS) or
    ``agentive_audit_admin`` factory can plant fixture rows.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        _allow_unsafe_writes: bool = False,
    ) -> None:
        super().__init__(session_factory)
        self._allow_unsafe_writes = _allow_unsafe_writes

    async def record(
        self,
        *,
        actor: str,
        action: str,
        correlation_id: UUID,
        target: str | None = None,
        payload_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
        tenant_id: UUID | None = None,
    ) -> AuditEvent:
        """Persist an audit event.

        **Sprint 0 status**: deferred — the runtime ``agentive_app`` role
        has no INSERT grant on ``audit_events``. A dedicated session
        factory bound to ``agentive_audit_admin`` is wired in Story 9.1
        when the audit middleware lands.
        """
        raise NotImplementedError(
            "AuditEventRepo.record() requires an audit_admin session factory; "
            "wiring deferred to Story 9.1 (Audit trail middleware)."
        )

    async def _record_unsafe(
        self,
        *,
        actor: str,
        action: str,
        correlation_id: UUID,
        target: str | None = None,
        payload_hash: str | None = None,
        metadata: dict[str, Any] | None = None,
        tenant_id: UUID | None = None,
    ) -> AuditEvent:
        """Internal helper — INSERTs directly via the configured session factory.

        Requires ``_allow_unsafe_writes=True`` at construction so a test
        fixture has explicitly acknowledged the bypass; otherwise raises
        :class:`RuntimeError`. Reserved for tests that provision an
        ``agentive_audit_admin`` or ``agentive_test_seed`` session.
        Never call from production code.
        """
        if not self._allow_unsafe_writes:
            raise RuntimeError(
                "_record_unsafe requires _allow_unsafe_writes=True at construction. "
                "Production code must use record() with an audit_admin session factory. "
                "Tests must opt in: AuditEventRepo(seed_session_factory, _allow_unsafe_writes=True)."
            )
        async with self.with_tenant(tenant_id) as session:
            event = AuditEvent(
                actor=actor,
                action=action,
                target=target,
                correlation_id=correlation_id,
                payload_hash=payload_hash,
                metadata_=metadata if metadata is not None else {},
                tenant_id=tenant_id,
            )
            session.add(event)
            await session.flush()
            await session.refresh(event)
            return event
