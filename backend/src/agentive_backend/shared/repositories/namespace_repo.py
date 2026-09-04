"""Public API surface for :class:`Namespace`. ALL DB access must go through this class."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select

from agentive_backend.infra.db.models import Namespace
from agentive_backend.shared.repositories.base import BaseRepo

# Mirror of the Postgres CHECK constraint on ``namespaces.type`` so callers
# get static-type leverage instead of relying on the DB to fail at runtime.
NamespaceType = Literal["client", "metier", "operationnelle", "contextuelle"]


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

    async def create(
        self,
        *,
        name: str,
        ns_type: NamespaceType,
        department: str | None = None,
        project: str | None = None,
        retention_policy: dict[str, Any] | None = None,
        embedding_backend: str = "cloud",
        tenant_id: UUID | None = None,
    ) -> Namespace:
        async with self.with_tenant(tenant_id) as session:
            ns = Namespace(
                name=name,
                type=ns_type,
                department=department,
                project=project,
                retention_policy=retention_policy or {},
                embedding_backend=embedding_backend,
                tenant_id=tenant_id,
            )
            session.add(ns)
            await session.flush()
            await session.refresh(ns)
            return ns
