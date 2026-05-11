"""Public API surface for :class:`ToolServer`, :class:`Tool`, and the
junction :class:`AgentTemplateTool` (Story 2.5).

ALL DB access must go through these classes. Features import
``ConflictError`` / ``NotFoundError`` from ``shared.exceptions`` — they
MUST NOT import ``sqlalchemy.exc`` directly (``import-linter`` Contract 3
forbids ``sqlalchemy`` imports from ``agentive_backend.features``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import asc, delete, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import (
    AgentTemplateTool,
    Tool,
    ToolServer,
)
from agentive_backend.shared.exceptions import ConflictError
from agentive_backend.shared.repositories.base import BaseRepo


class ToolServerRepo(BaseRepo):
    """Public API surface for ToolServer (Story 2.5)."""

    async def get_by_id(
        self, server_id: UUID, *, tenant_id: UUID | None = None
    ) -> ToolServer | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(ToolServer, server_id)

    async def get_by_id_in_session(
        self,
        session: AsyncSession,
        server_id: UUID,
    ) -> ToolServer | None:
        return await session.get(ToolServer, server_id)

    async def get_by_name_in_session(
        self,
        session: AsyncSession,
        name: str,
        *,
        tenant_id: UUID | None = None,
    ) -> ToolServer | None:
        """SELECT by `(name, tenant_id)` — used for the duplicate check
        (décision #6 Story 2.5 : POST same name → 409 ConflictError, no
        UPSERT silencieux).

        P-11 (CR 2026-05-10) — Postgres ``IS`` predicate accepts only
        ``NULL``/``TRUE``/``FALSE``/``UNKNOWN``, so ``tenant_id.is_(<uuid>)``
        emits invalid SQL. Branch by None vs UUID so Story 12 multi-tenant
        doesn't crash with a SyntaxError on the day a real tenant_id is
        passed.
        """
        tenant_predicate = (
            ToolServer.tenant_id.is_(None)
            if tenant_id is None
            else ToolServer.tenant_id == tenant_id
        )
        stmt = select(ToolServer).where(ToolServer.name == name, tenant_predicate)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        name: str,
        transport: str,
        connection_config: dict[str, Any],
        tenant_id: UUID | None = None,
    ) -> ToolServer:
        """INSERT inside the caller's transaction (atomicity P-02 Story 2.1).

        Raises:
            ConflictError: ``(name, tenant_id)`` already exists. The repo
                translates ``IntegrityError`` so feature code stays free of
                ``sqlalchemy`` imports.
        """
        server = ToolServer(
            name=name,
            transport=transport,
            connection_config=connection_config,
            tenant_id=tenant_id,
        )
        session.add(server)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                detail=f"Tool server '{name}' already registered",
                context={"name": name},
            ) from exc
        await session.refresh(server)
        return server

    async def list_with_tools_count(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> list[tuple[ToolServer, int]]:
        """LEFT JOIN tools + GROUP BY → list[(server, count)] ordered by
        ``discovered_at DESC, id DESC`` (most recent first — UX naturelle,
        secondary sort P-15 for deterministic ordering on ties).
        Single SQL query (no N+1) — verified by P-21 test.
        """
        async with self.with_tenant(tenant_id) as session:
            stmt = (
                select(ToolServer, func.count(Tool.id).label("tools_count"))
                .outerjoin(Tool, Tool.server_id == ToolServer.id)
                .group_by(ToolServer.id)
                .order_by(desc(ToolServer.discovered_at), desc(ToolServer.id))
            )
            result = await session.execute(stmt)
            return [(row[0], int(row[1])) for row in result.all()]


class ToolRepo(BaseRepo):
    """Public API surface for Tool (Story 2.5)."""

    async def get_by_id(self, tool_id: UUID, *, tenant_id: UUID | None = None) -> Tool | None:
        async with self.with_tenant(tenant_id) as session:
            return await session.get(Tool, tool_id)

    async def get_by_id_in_session(
        self,
        session: AsyncSession,
        tool_id: UUID,
    ) -> Tool | None:
        return await session.get(Tool, tool_id)

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        server_id: UUID,
        name: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        tenant_id: UUID | None = None,
    ) -> Tool:
        """INSERT a tool. ``input_schema`` defaults to ``{}`` if None.

        Raises:
            ConflictError: ``(server_id, name)`` already exists (the
                ``uq_tool_per_server`` constraint).
        """
        tool = Tool(
            server_id=server_id,
            name=name,
            description=description,
            input_schema=input_schema if input_schema is not None else {},
            output_schema=output_schema,
            tenant_id=tenant_id,
        )
        session.add(tool)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                detail=f"Tool '{name}' already exists for server {server_id}",
                context={"server_id": str(server_id), "name": name},
            ) from exc
        await session.refresh(tool)
        return tool

    async def list_by_server_in_session(
        self,
        session: AsyncSession,
        server_id: UUID,
    ) -> list[Tool]:
        stmt = select(Tool).where(Tool.server_id == server_id).order_by(asc(Tool.name))
        result = await session.execute(stmt)
        return list(result.scalars().all())


class AgentTemplateToolRepo(BaseRepo):
    """Junction repo — manages assignments between agent_templates and
    tools (Story 2.5)."""

    async def list_by_template_in_session(
        self,
        session: AsyncSession,
        template_id: UUID,
    ) -> list[tuple[Tool, datetime]]:
        """Return the Tools currently assigned to ``template_id`` paired with
        their ``assigned_at`` timestamp from the junction row, ordered by
        ``Tool.name`` then ``Tool.id`` for deterministic UX (P-01 + P-15).
        """
        stmt = (
            select(Tool, AgentTemplateTool.assigned_at)
            .join(AgentTemplateTool, AgentTemplateTool.tool_id == Tool.id)
            .where(AgentTemplateTool.agent_template_id == template_id)
            .order_by(asc(Tool.name), asc(Tool.id))
        )
        result = await session.execute(stmt)
        return [(tool, assigned_at) for tool, assigned_at in result.all()]

    async def replace_in_session(
        self,
        session: AsyncSession,
        *,
        template_id: UUID,
        new_tool_ids: list[UUID],
        actor: str = "system",
        tenant_id: UUID | None = None,
    ) -> tuple[list[UUID], list[UUID]]:
        """REPLACE semantics (Story 2.5 décision #9) : the new list REPLACES
        the current assignments for ``template_id``.

        Returns
        -------
        tuple[added: list[UUID], removed: list[UUID]]
            The diffs — used by the service layer to emit one audit event
            per added/removed assignment.

        Atomic — all DML happens inside the caller's transaction.
        """
        # Fetch current assignments (just the tool_ids — we don't need the rows).
        current_stmt = select(AgentTemplateTool.tool_id).where(
            AgentTemplateTool.agent_template_id == template_id
        )
        result = await session.execute(current_stmt)
        current: set[UUID] = {row[0] for row in result.all()}
        new_set = set(new_tool_ids)

        added = sorted(new_set - current)
        removed = sorted(current - new_set)

        # DELETE the removed.
        if removed:
            del_stmt = delete(AgentTemplateTool).where(
                AgentTemplateTool.agent_template_id == template_id,
                AgentTemplateTool.tool_id.in_(removed),
            )
            await session.execute(del_stmt)

        # INSERT the added.
        for tool_id in added:
            session.add(
                AgentTemplateTool(
                    agent_template_id=template_id,
                    tool_id=tool_id,
                    assigned_by_actor=actor,
                    tenant_id=tenant_id,
                )
            )
        if added:
            await session.flush()

        return list(added), list(removed)

    async def unassign_in_session(
        self,
        session: AsyncSession,
        *,
        template_id: UUID,
        tool_id: UUID,
    ) -> bool:
        """DELETE one assignment. Returns ``True`` if a row was actually
        deleted, ``False`` if the assignment did not exist (used by the
        service layer to raise 404 NotFoundError — Story 2.5 décision #10
        idempotency stricte).
        """
        stmt = delete(AgentTemplateTool).where(
            AgentTemplateTool.agent_template_id == template_id,
            AgentTemplateTool.tool_id == tool_id,
        )
        result = await session.execute(stmt)
        rowcount = getattr(result, "rowcount", None) or 0
        return int(rowcount) > 0
