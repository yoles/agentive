"""Public API surface for :class:`Namespace`. ALL DB access must go through this class."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import asc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import Namespace
from agentive_backend.shared.exceptions import ConflictError
from agentive_backend.shared.repositories.base import BaseRepo

# Mirror of the Postgres CHECK constraint on ``namespaces.type`` so callers
# get static-type leverage instead of relying on the DB to fail at runtime.
NamespaceType = Literal["client", "metier", "operationnelle", "contextuelle"]

# `NamespaceRepo.list_all`'s default `limit` — see its docstring (code
# review Story 3.2, BS3).
NAMESPACE_LISTING_SAFETY_CAP = 10_000


class NamespaceRepo(BaseRepo):
    """Public API surface for Namespace. ALL DB access must go through this class.

    The ``type`` column is constrained by a CHECK to one of the four MVP
    namespace types (``client``, ``metier``, ``operationnelle``,
    ``contextuelle``). The :data:`NamespaceType` ``Literal`` mirrors that
    constraint so the type checker rejects typos.
    """

    async def get_by_id(
        self, namespace_id: UUID, *, tenant_id: UUID | None = None
    ) -> Namespace | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(Namespace, namespace_id)

    async def get_by_name(self, name: str, *, tenant_id: UUID | None = None) -> Namespace | None:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(Namespace).where(Namespace.name == name)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def require_by_name(self, name: str, *, tenant_id: UUID | None = None) -> Namespace:
        """Fetch by name or raise :class:`NotFoundError` (lookup-or-404, audit A-07).

        Story 3.1 AC1/AC2 — namespace creation is Story 3.2 (no implicit
        creation here); an unknown ``name`` is a 404, not an auto-provision.
        """
        return self._require_found(
            await self.get_by_name(name, tenant_id=tenant_id),
            label="Namespace",
            entity_id=name,
            context_key="namespace",
        )

    async def list_by_type(
        self,
        ns_type: NamespaceType,
        *,
        tenant_id: UUID | None = None,
        limit: int = 100,
    ) -> list[Namespace]:
        async with self.with_tenant(tenant_id) as session:
            stmt = select(Namespace).where(Namespace.type == ns_type).limit(limit)
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def list_all(
        self, *, tenant_id: UUID | None = None, limit: int = NAMESPACE_LISTING_SAFETY_CAP
    ) -> list[Namespace]:
        """All namespaces, no ``type``/``department`` filter (Story 3.2 AC3).

        Admin listing for ``Config > Namespaces`` — John (owner) needs to see
        every namespace to administer the memory system, unlike the
        department-scoped read/write path in ``MemoryManagerService``.

        ``limit`` used to default to 500 with no pagination and no signal
        when the cap was hit, silently contradicting AC3's "retourne tous
        les namespaces" (code review Story 3.2, BS3). Unlike
        ``memory_chunks``, namespaces are only ever created by an admin
        through ``POST /memory/namespaces`` — there is no user-facing or
        automated path that grows this table, so a generous cap is a safety
        net against a runaway caller, not a real ceiling. The caller
        (``MemoryManagerService.list_namespaces``) logs a warning if this
        cap is ever actually hit, so a truncation is never silent.

        Ordered (``created_at``, ``id``) so row order is stable across
        refetches — Postgres makes no ordering guarantee on an unordered
        ``SELECT`` (code review Story 3.2, P6).
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(Namespace)
                .order_by(asc(Namespace.created_at), asc(Namespace.id))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def set_decay_policy_in_session(
        self,
        session: AsyncSession,
        *,
        name: str,
        decay_policy: dict[str, Any],
    ) -> Namespace | None:
        """UPDATE ``decay_policy`` inside the caller's transaction.

        Returns ``None`` when no namespace carries that ``name``, leaving the
        404 to the service (same lookup-or-404 split as
        :meth:`require_by_name`).

        Session-scoped like :meth:`create_in_session` so the service can
        publish ``NamespaceDecayPolicyUpdatedEvent`` in the same transaction
        as the write: a crash between the two would otherwise leave a
        namespace silently reordering its search results with nothing in the
        outbox to say when that started (Story 3.4, code review BS1).
        """
        stmt = select(Namespace).where(Namespace.name == name)
        namespace = (await session.execute(stmt)).scalar_one_or_none()
        if namespace is None:
            return None
        namespace.decay_policy = decay_policy
        await session.flush()
        await session.refresh(namespace)
        return namespace

    async def create(
        self,
        *,
        name: str,
        ns_type: NamespaceType,
        department: str | None = None,
        project: str | None = None,
        retention_policy: dict[str, Any] | None = None,
        decay_policy: dict[str, Any] | None = None,
        embedding_backend: str = "cloud",
        tenant_id: UUID | None = None,
    ) -> Namespace:
        """Convenience wrapper — self-managed transaction.

        Use :meth:`create_in_session` from inside an existing transaction
        when you need to compose the INSERT with another write (e.g.
        publishing an outbox event atomically — Story 3.2 AC1).

        Raises:
            ConflictError: If ``name`` already exists.
        """
        async with self.with_tenant(tenant_id) as session:
            return await self.create_in_session(
                session,
                name=name,
                ns_type=ns_type,
                department=department,
                project=project,
                retention_policy=retention_policy,
                decay_policy=decay_policy,
                embedding_backend=embedding_backend,
                tenant_id=tenant_id,
            )

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        name: str,
        ns_type: NamespaceType,
        department: str | None = None,
        project: str | None = None,
        retention_policy: dict[str, Any] | None = None,
        decay_policy: dict[str, Any] | None = None,
        embedding_backend: str = "cloud",
        tenant_id: UUID | None = None,
    ) -> Namespace:
        """INSERT inside the caller's transaction — caller owns commit.

        Story 3.2 T5.1 — used by ``MemoryManagerService.create_namespace`` to
        publish ``NamespaceCreatedEvent`` in the same transaction as the row
        INSERT (atomicity with the outbox pattern, same mirror as
        ``AgentTemplateRepo.create_in_session``).

        Raises:
            ConflictError: If ``name`` already exists. The repo translates
                ``IntegrityError`` (``namespaces.name`` UNIQUE constraint) so
                feature code stays free of ``sqlalchemy`` imports
                (``import-linter`` Contract 3).
        """
        ns = Namespace(
            name=name,
            type=ns_type,
            department=department,
            project=project,
            retention_policy=retention_policy or {},
            # Story 3.4 T6.1 — `{}` is `DecayFunction.NONE`, i.e. a constant
            # factor of 1.0, i.e. the pre-3.4 ordering. Callers opt in.
            decay_policy=decay_policy or {},
            embedding_backend=embedding_backend,
            tenant_id=tenant_id,
        )
        session.add(ns)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                detail=f"Namespace '{name}' already exists",
                context={"name": name},
            ) from exc
        await session.refresh(ns)
        return ns
