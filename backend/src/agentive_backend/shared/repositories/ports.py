"""Persistence ports — the abstractions the application layer depends on (DIP).

Audit A-11 / §2.5a: the feature services used to depend on the *concrete*
repository classes (``AgentTemplateRepo``, ``ToolServerRepo``, …), so the
application knew the shape of one precise persistence implementation. These
:class:`typing.Protocol` ports invert that: a service depends on the port, and
the concrete repos in this package satisfy it **structurally — no change to the
repos**.

Why the ports live in ``shared.repositories`` (not the feature/domain layer)
---------------------------------------------------------------------------
The port signatures must name :class:`~sqlalchemy.ext.asyncio.AsyncSession`
(the ``*_in_session`` atomicity contract, P-02) and the ORM row types. A
feature-layer module naming those would violate ``import-linter`` Contract 3
(no direct ``sqlalchemy`` in ``features``) and Contract 2 (``features`` may not
reach ``infra.db``). ``shared.repositories`` is the single package already
whitelisted for both. A feature importing a port from here is the normal
``features → shared`` edge; the ``AsyncSession``/ORM references stay behind the
``shared`` boundary. Services import these under ``TYPE_CHECKING`` (annotations
are strings via ``from __future__ import annotations``), so no runtime edge is
created either.

Granularity: one port per persistence concept (per repo), covering exactly the
methods the services call. The concrete repos expose a superset — the port is
the *consumer's* view.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from agentive_backend.infra.db.models import (
    AgentInstance,
    AgentTemplate,
    Prompt,
    Tool,
    ToolServer,
    WorkflowRun,
)


class AgentTemplateRepository(Protocol):
    """Persistence port for ``agent_templates`` (Agent Registry, Tool Hub, Playground)."""

    def with_tenant(self, tenant_id: UUID | None) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def require_by_id(
        self, template_id: UUID, *, tenant_id: UUID | None = None
    ) -> AgentTemplate: ...

    async def get_by_id_in_session(
        self, session: AsyncSession, template_id: UUID
    ) -> AgentTemplate | None: ...

    async def require_by_id_in_session(
        self, session: AsyncSession, template_id: UUID
    ) -> AgentTemplate: ...

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        name: str,
        archetype: str,
        config: dict[str, Any],
        version: int = 1,
        tenant_id: UUID | None = None,
    ) -> AgentTemplate: ...

    async def update_in_session(
        self,
        session: AsyncSession,
        *,
        template: AgentTemplate,
        config: dict[str, Any],
        new_version: int,
    ) -> AgentTemplate: ...

    async def update_config_in_session(
        self,
        session: AsyncSession,
        *,
        template: AgentTemplate,
        config: dict[str, Any],
    ) -> AgentTemplate: ...


class PromptRepository(Protocol):
    """Persistence port for ``prompts`` (append-only versioning)."""

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        agent_template_id: UUID,
        version: int,
        content: str,
        tenant_id: UUID | None = None,
    ) -> Prompt: ...


class AgentInstanceRepository(Protocol):
    """Persistence port for ``agent_instances`` (immutable snapshots)."""

    def with_tenant(self, tenant_id: UUID | None) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def require_by_id(
        self, instance_id: UUID, *, tenant_id: UUID | None = None
    ) -> AgentInstance: ...

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        template_id: UUID,
        template_version: int,
        snapshot: dict[str, Any],
        workflow_run_id: UUID | None = None,
        tenant_id: UUID | None = None,
    ) -> AgentInstance: ...

    async def list_by_workflow_run_in_session(
        self, session: AsyncSession, workflow_run_id: UUID
    ) -> list[AgentInstance]: ...


class WorkflowRunRepository(Protocol):
    """Persistence port for ``workflow_runs`` (FK validation only, here)."""

    async def require_by_id_in_session(
        self, session: AsyncSession, run_id: UUID
    ) -> WorkflowRun: ...


class AgentTemplateToolRepository(Protocol):
    """Persistence port for the ``agent_template_tools`` junction."""

    def with_tenant(self, tenant_id: UUID | None) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def list_by_template_in_session(
        self, session: AsyncSession, template_id: UUID
    ) -> list[tuple[Tool, datetime]]: ...

    async def replace_in_session(
        self,
        session: AsyncSession,
        *,
        template_id: UUID,
        new_tool_ids: list[UUID],
        actor: str = "system",
        tenant_id: UUID | None = None,
    ) -> tuple[list[UUID], list[UUID]]: ...

    async def unassign_in_session(
        self,
        session: AsyncSession,
        *,
        template_id: UUID,
        tool_id: UUID,
    ) -> bool: ...


class ToolServerRepository(Protocol):
    """Persistence port for ``tool_servers`` (Tool Hub)."""

    def with_tenant(self, tenant_id: UUID | None) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def get_by_id_in_session(
        self, session: AsyncSession, server_id: UUID
    ) -> ToolServer | None: ...

    async def require_by_id_in_session(
        self, session: AsyncSession, server_id: UUID
    ) -> ToolServer: ...

    async def get_by_name_in_session(
        self, session: AsyncSession, name: str, *, tenant_id: UUID | None = None
    ) -> ToolServer | None: ...

    async def create_in_session(
        self,
        session: AsyncSession,
        *,
        name: str,
        transport: str,
        connection_config: dict[str, Any],
        tenant_id: UUID | None = None,
    ) -> ToolServer: ...

    async def list_with_tools_count(
        self, *, tenant_id: UUID | None = None
    ) -> list[tuple[ToolServer, int]]: ...


class ToolRepository(Protocol):
    """Persistence port for ``tools``."""

    async def get_by_id_in_session(self, session: AsyncSession, tool_id: UUID) -> Tool | None: ...

    async def require_by_id_in_session(self, session: AsyncSession, tool_id: UUID) -> Tool: ...

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
    ) -> Tool: ...

    async def list_by_server_in_session(
        self, session: AsyncSession, server_id: UUID
    ) -> list[Tool]: ...


__all__ = [
    "AgentInstanceRepository",
    "AgentTemplateRepository",
    "AgentTemplateToolRepository",
    "PromptRepository",
    "ToolRepository",
    "ToolServerRepository",
    "WorkflowRunRepository",
]
