"""Public API surface for :class:`ToolServer`, :class:`Tool`, and the
junction :class:`AgentTemplateTool` (Story 2.5).

ALL DB access must go through these classes. Features import
``ConflictError`` / ``NotFoundError`` from ``shared.exceptions`` — they
MUST NOT import ``sqlalchemy.exc`` directly (``import-linter`` Contract 3
forbids ``sqlalchemy`` imports from ``agentive_backend.features``).
"""

from __future__ import annotations

from collections.abc import Sequence
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

    async def require_by_id_in_session(self, session: AsyncSession, server_id: UUID) -> ToolServer:
        """In-session fetch by id or raise :class:`NotFoundError` (audit A-07)."""
        return self._require_found(
            await self.get_by_id_in_session(session, server_id),
            label="Tool server",
            entity_id=server_id,
            context_key="server_id",
        )

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

    async def require_by_id_in_session(self, session: AsyncSession, tool_id: UUID) -> Tool:
        """In-session fetch by id or raise :class:`NotFoundError` (audit A-07)."""
        return self._require_found(
            await self.get_by_id_in_session(session, tool_id),
            label="Tool",
            entity_id=tool_id,
            context_key="tool_id",
        )

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

    async def list_by_names(
        self,
        names: Sequence[str],
        *,
        tenant_id: UUID | None = None,
    ) -> dict[str, list[Tool]]:
        """Les outils portant ces noms, groupés par nom (Story 5.1 T2.4).

        Rend une LISTE par nom, jamais un outil : l'unicité déclarée est
        ``(server_id, name)``, donc deux serveurs MCP peuvent exposer un
        ``read_file`` chacun. Choisir « le premier » ferait dépendre
        l'assignation de l'ordre de retour de Postgres ; c'est à l'appelant
        de refuser l'ambiguïté, ce que fait le provisioning du Pôle Dev.

        Un nom absent est absent de la réponse — pas une entrée vide : le
        provisioning distingue « introuvable » de « ambigu » et ne rend pas
        le même message.
        """
        unique = sorted({name for name in names if name})
        if not unique:
            # `IN ()` — même raison que `list_by_ids` : pas d'aller-retour
            # pour une question vide.
            return {}
        async with self.with_tenant(tenant_id) as session:
            stmt = select(Tool).where(Tool.name.in_(unique)).order_by(asc(Tool.name), asc(Tool.id))
            result = await session.execute(stmt)
            grouped: dict[str, list[Tool]] = {}
            for tool in result.scalars().all():
                grouped.setdefault(tool.name, []).append(tool)
            return grouped

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

    async def list_resolved_for_template_in_session(
        self,
        session: AsyncSession,
        template_id: UUID,
    ) -> list[tuple[Tool, ToolServer]]:
        """Assigned tools joined with the server that can actually run them
        (Story 5.0 AC2).

        :meth:`list_by_template_in_session` returns the ``Tool`` rows, which
        carry the name, description and input schema — everything needed to
        OFFER a tool to a model, and nothing needed to CALL it. Calling needs
        the server's ``transport`` and ``connection_config``, which live one
        table away.

        A single join rather than a fetch-per-tool: a template with eight
        assigned tools would otherwise issue nine queries on the hot path of
        every run — the N+1 shape this repo has already had to close three
        times (Story 4.8 AC3, and its third site found in review).

        Returns ORM rows, not a domain object: ``shared`` cannot import
        ``infra`` (Contract 2), and the executor's ``ResolvedTool`` lives in
        ``infra/mcp``. The calling feature does that last projection.
        """
        stmt = (
            select(Tool, ToolServer)
            .join(AgentTemplateTool, AgentTemplateTool.tool_id == Tool.id)
            .join(ToolServer, ToolServer.id == Tool.server_id)
            .where(AgentTemplateTool.agent_template_id == template_id)
            .order_by(asc(Tool.name), asc(Tool.id))
        )
        result = await session.execute(stmt)
        return [(tool, server) for tool, server in result.all()]

    async def list_resolved_for_templates(
        self,
        template_ids: Sequence[UUID],
        *,
        tenant_id: UUID | None = None,
    ) -> dict[UUID, list[tuple[Tool, ToolServer]]]:
        """Same join as :meth:`list_resolved_for_template_in_session`, for
        MANY templates at once (review P2).

        The workflow engine needs this shape: a DAG resolves its tools once
        per run, for every node, and calling the single-template method in a
        loop would reintroduce exactly the N+1 that method's own docstring
        says it exists to avoid — one query per node, on the hot path of
        every run and every resume.

        Opens its own transaction rather than taking a session, because the
        engine's caller has none to lend: ``_load_templates`` is the
        precedent, and it batches for the same reason.

        Returns a dict keyed by ``agent_template_id``. A template with no
        assigned tool is ABSENT rather than mapped to an empty list — the
        caller distinguishes "no tools" from "unknown template" by asking the
        template map, not this one. Empty ``template_ids`` short-circuits
        without a round trip: a DAG of nodes that all lack tools must not pay
        for a query returning nothing.
        """
        if not template_ids:
            return {}
        stmt = (
            select(AgentTemplateTool.agent_template_id, Tool, ToolServer)
            .join(Tool, Tool.id == AgentTemplateTool.tool_id)
            .join(ToolServer, ToolServer.id == Tool.server_id)
            .where(AgentTemplateTool.agent_template_id.in_(list(template_ids)))
            .order_by(asc(Tool.name), asc(Tool.id))
        )
        async with self.with_tenant(tenant_id) as session:
            result = await session.execute(stmt)
            rows = result.all()
        grouped: dict[UUID, list[tuple[Tool, ToolServer]]] = {}
        for template_id, tool, server in rows:
            grouped.setdefault(template_id, []).append((tool, server))
        return grouped

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
