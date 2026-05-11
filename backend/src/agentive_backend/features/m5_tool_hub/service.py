"""Service layer — Tool Hub MCP orchestration (Story 2.5).

The service sits between the FastAPI router and the repositories. It :

1. Validates the requested transport via Pydantic ``Transport`` Literal
   (router-level).
2. Calls :func:`agentive_backend.infra.mcp.client.discover_tools` with a
   strict 10s timeout (Story 2.5 décision #4).
3. Composes the row INSERTs (1 server + N tools) AND the audit-event
   publishes (1 server.connected + N tool.discovered) in a SINGLE
   transaction (Story 2.1 P-02 atomicity pattern). The audit events
   bypass to ``AuditEventRepo.record()`` — TODO Story 9.1 cleanup on a
   single line so ``git grep "audit-event bypass cleanup"`` finds it.
4. After the transaction commits, best-effort ``emit_notify`` to wake the
   outbox worker. NOTIFY failure is non-fatal (poll fallback Story 1.4).

Domain errors raised :
- :class:`ConflictError` — server with this name already exists (décision #6).
- :class:`DependencyError` — MCP discovery timed out (décision #4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from agentive_backend.features.m5_tool_hub.schemas import (
    ToolServerDetailView,
    ToolServerView,
    ToolView,
)
from agentive_backend.infra.mcp.client import (
    MCPDiscoveryTimeoutError,
    discover_tools,
)
from agentive_backend.shared.contracts.events import (
    ToolDiscoveredEvent,
    ToolServerConnectedEvent,
)
from agentive_backend.shared.event_bus import emit_notify, publish
from agentive_backend.shared.exceptions import DependencyError, NotFoundError
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.repositories import (
        ToolRepo,
        ToolServerRepo,
    )

_log = get_logger(__name__)

# P-03 (CR 2026-05-10) — secret redaction for API responses.
#
# ``connection_config`` is stored verbatim (Sprint 1 = clear text on disk,
# Fernet defer Story 9.2 / D55) but the API response MUST NOT echo
# credentials back to the caller, since access middleware logs response
# bodies. Strategy: only the structural fields (``command``, ``args``,
# ``url``) are exposed ; values that hold secrets (``env``, ``headers``)
# are replaced by their key list (``["KEY1", "KEY2"]``) so the caller can
# see the SHAPE without reading the secret values.
_REDACTED_VALUE_KEY_LIST = "<redacted-keys>"


def _redact_connection_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``config`` with secret-bearing values masked.

    Secret-bearing keys (env, headers, Authorization, token, password,
    api_key, secret) are replaced by the SHAPE of their value (list of
    sub-keys for dicts ; ``"<redacted>"`` for strings) so consumers can
    inspect the wiring without leaking credentials.
    """
    if not isinstance(config, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, value in config.items():
        lkey = key.lower()
        # Whole-dict redaction for env-like / headers-like containers.
        if lkey in {"env", "headers"}:
            if isinstance(value, dict):
                safe[key] = {"_redacted_keys": sorted(str(k) for k in value)}
            else:
                safe[key] = _REDACTED_VALUE_KEY_LIST
        # Direct secret-name match — mask the value.
        elif lkey in {"authorization", "token", "password", "api_key", "secret", "auth"}:
            safe[key] = "<redacted>"
        else:
            safe[key] = value
    return safe


class ToolHubService:
    """Orchestrate MCP server registration + tool discovery (Story 2.5)."""

    def __init__(
        self,
        *,
        server_repo: ToolServerRepo,
        tool_repo: ToolRepo,
    ) -> None:
        self._server_repo = server_repo
        self._tool_repo = tool_repo

    async def connect_server(
        self,
        *,
        name: str,
        transport: str,  # 'stdio' | 'sse' (validated upstream by Pydantic Literal)
        connection_config: dict[str, Any],
        tenant_id: UUID | None = None,
    ) -> ToolServerDetailView:
        """Register a new MCP server + discover its tools atomically.

        Story 2.1 P-02 — the row INSERTs and outbox INSERTs share a single
        transaction. ``with_tenant`` commits at ``__aexit__`` ; any
        exception triggers a rollback so a partial state is impossible.

        Raises
        ------
        ConflictError
            ``(name, tenant_id)`` already exists.
        DependencyError
            MCP discovery timed out (default 10s).
        """
        # Step 1 (out of transaction) — duplicate check via a fresh session.
        # We do this BEFORE the discovery so we don't waste 10s of subprocess
        # spawn / SSE handshake for a name that's already taken.
        async with self._server_repo.with_tenant(tenant_id) as session:
            existing = await self._server_repo.get_by_name_in_session(
                session, name=name, tenant_id=tenant_id
            )
            if existing is not None:
                # ConflictError raised explicitly here (vs IntegrityError on
                # INSERT) so we fail fast with a clear message before the
                # MCP discovery costs a full timeout window.
                from agentive_backend.shared.exceptions import ConflictError

                raise ConflictError(
                    detail=f"Tool server '{name}' already registered",
                    context={"name": name},
                )

        # Step 2 (out of transaction) — MCP discovery. Network/subprocess
        # I/O outside the DB transaction so we don't hold a connection
        # open for 10s.
        try:
            discovered = await discover_tools(
                transport=transport,  # type: ignore[arg-type]  # Literal narrowed upstream
                connection_config=connection_config,
                timeout=10.0,
            )
        except MCPDiscoveryTimeoutError as exc:
            raise DependencyError(
                detail="MCP server discovery timeout",
                context={"name": name, "transport": transport, "timeout_seconds": exc.timeout},
            ) from exc
        except ValueError as exc:
            # `connection_config` shape issue (e.g. stdio without 'command')
            # — surface as 503 DependencyError (the user provided invalid
            # config that the MCP layer rejected).
            raise DependencyError(
                detail=f"MCP connection_config invalid: {exc}",
                context={"name": name, "transport": transport},
            ) from exc
        except (FileNotFoundError, PermissionError, OSError) as exc:
            # P-08 (CR 2026-05-10) — subprocess spawn failures (stdio
            # ``command`` not on PATH, executable bit missing, broken pipe
            # post-fork, ENOENT) bubble up as raw OS errors otherwise. Wrap
            # them as DependencyError 503 with the exception type so
            # callers see "MCP command not found / not executable" instead
            # of a generic 500.
            raise DependencyError(
                detail=f"MCP transport error: {type(exc).__name__}: {exc}",
                context={"name": name, "transport": transport, "error_type": type(exc).__name__},
            ) from exc
        except Exception as exc:  # P-08 defensive catch-all
            # The MCP SDK can raise its own client errors (mcp.ClientError,
            # JSONDecodeError, anyio.EndOfStream, httpx exceptions for SSE,
            # etc.). Any of those bubbling to the FastAPI global handler
            # becomes a 500 with no useful detail. Translate to 503 so the
            # caller knows the failure is in the MCP plumbing, not our
            # service. P-03 redaction note: do NOT include connection_config
            # in the error context (may contain secrets).
            _log.warning(
                "mcp_discovery_unexpected_error",
                name=name,
                transport=transport,
                error_type=type(exc).__name__,
                error_message=str(exc)[:200],
            )
            raise DependencyError(
                detail=f"MCP discovery failed: {type(exc).__name__}",
                context={"name": name, "transport": transport, "error_type": type(exc).__name__},
            ) from exc

        # Step 3 — INSERT server + N tools + N+1 audit events in ONE transaction.
        async with self._server_repo.with_tenant(tenant_id) as session:
            # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
            server = await self._server_repo.create_in_session(
                session,
                name=name,
                transport=transport,
                connection_config=connection_config,
                tenant_id=tenant_id,
            )

            inserted_tools: list[Any] = []  # list[Tool] — kept loose to dodge ORM import here
            for info in discovered:
                tool = await self._tool_repo.create_in_session(
                    session,
                    server_id=server.id,
                    name=info.name,
                    description=info.description,
                    input_schema=info.input_schema,
                    output_schema=info.output_schema,
                    tenant_id=tenant_id,
                )
                inserted_tools.append(tool)

            # Publish 1 + N audit events in the same transaction.
            server_event = ToolServerConnectedEvent(
                server_id=server.id,
                name=server.name,
                transport=server.transport,
                tools_count=len(inserted_tools),
                actor="system",
                tenant_id=tenant_id,
            )
            server_event_id = await publish(
                ToolServerConnectedEvent.event_type, server_event, session=session
            )

            for tool in inserted_tools:
                tool_event = ToolDiscoveredEvent(
                    tool_id=tool.id,
                    server_id=server.id,
                    tool_name=tool.name,
                    actor="system",
                    tenant_id=tenant_id,
                )
                await publish(ToolDiscoveredEvent.event_type, tool_event, session=session)
            # commit happens at __aexit__ if no exception is raised.

        # Step 4 — Post-commit best-effort NOTIFY (Story 1.4 outbox pattern).
        try:
            await emit_notify(server_event_id, ToolServerConnectedEvent.event_type)
        except Exception:
            _log.warning(
                "event_bus_notify_failed_will_be_polled",
                event_id=str(server_event_id),
                event_type=ToolServerConnectedEvent.event_type,
            )

        _log.info(
            "tool_server_connected",
            server_id=str(server.id),
            name=server.name,
            transport=server.transport,
            tools_count=len(inserted_tools),
            actor="system",
            tenant_id=None,
        )

        # Build the detail response.
        return ToolServerDetailView(
            server_id=server.id,
            name=server.name,
            transport=server.transport,
            status=server.status,
            connection_config=_redact_connection_config(server.connection_config),
            discovered_at=server.discovered_at,
            tools_count=len(inserted_tools),
            tools=[
                ToolView(
                    tool_id=tool.id,
                    name=tool.name,
                    description=tool.description,
                    input_schema=dict(tool.input_schema or {}),
                    output_schema=tool.output_schema,
                )
                for tool in inserted_tools
            ],
        )

    async def list_servers(
        self,
        *,
        tenant_id: UUID | None = None,
    ) -> list[ToolServerView]:
        """List all registered MCP servers with their tools count (AC4)."""
        rows = await self._server_repo.list_with_tools_count(tenant_id=tenant_id)
        return [
            ToolServerView(
                server_id=server.id,
                name=server.name,
                transport=server.transport,
                status=server.status,
                tools_count=count,
                discovered_at=server.discovered_at,
            )
            for server, count in rows
        ]

    async def get_server_detail(
        self,
        server_id: UUID,
        *,
        tenant_id: UUID | None = None,
    ) -> ToolServerDetailView:
        """Get one server with its full tools list (AC4).

        Raises
        ------
        NotFoundError
            ``server_id`` does not exist.
        """
        async with self._server_repo.with_tenant(tenant_id) as session:
            server = await self._server_repo.get_by_id_in_session(session, server_id)
            if server is None:
                raise NotFoundError(
                    detail=f"Tool server '{server_id}' not found",
                    context={"server_id": str(server_id)},
                )
            tools = await self._tool_repo.list_by_server_in_session(session, server_id)

        return ToolServerDetailView(
            server_id=server.id,
            name=server.name,
            transport=server.transport,
            status=server.status,
            connection_config=_redact_connection_config(server.connection_config),
            discovered_at=server.discovered_at,
            tools_count=len(tools),
            tools=[
                ToolView(
                    tool_id=tool.id,
                    name=tool.name,
                    description=tool.description,
                    input_schema=dict(tool.input_schema or {}),
                    output_schema=tool.output_schema,
                )
                for tool in tools
            ],
        )


__all__ = ["ToolHubService"]
