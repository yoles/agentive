"""Service layer — Tool Hub MCP orchestration (Stories 2.5 + 2.6).

The service sits between the FastAPI router and the repositories. It :

1. Validates the requested transport via Pydantic ``Transport`` Literal
   (router-level).
2. **Story 2.5** — Calls :func:`...client.discover_tools` (10s timeout)
   and composes the row INSERTs + audit events in a SINGLE transaction.
3. **Story 2.6** — Calls :func:`...client.call_tool` (configurable
   timeout) inside the sandbox (bwrap or setrlimit fallback) and audits
   the invocation with ``m5.tool.invoked``.
4. After the transaction commits, best-effort ``emit_notify`` to wake the
   outbox worker. NOTIFY failure is non-fatal (poll fallback Story 1.4).

Domain errors raised :
- :class:`ConflictError` — server with this name already exists (décision #6).
- :class:`DependencyError` — MCP discovery timed out, sandbox crash, or
  tool execution timeout (P-08 Story 2.5 translation).
- :class:`NotFoundError` — server / tool not found OR tool returned
  ``isError=True`` (Story 2.6 AC6).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from agentive_backend.features.m5_tool_hub.schemas import (
    ToolServerDetailView,
    ToolServerView,
    ToolView,
)
from agentive_backend.infra.mcp.client import (
    MCPDiscoveryTimeoutError,
    MCPExecutionError,
    MCPExecutionTimeoutError,
    MCPToolError,
    call_tool,
    discover_tools,
)
from agentive_backend.infra.mcp.sandbox import SandboxBackend
from agentive_backend.shared.contracts.events import (
    ToolDiscoveredEvent,
    ToolInvokedEvent,
    ToolServerConnectedEvent,
)
from agentive_backend.shared.event_bus import emit_notify, publish
from agentive_backend.shared.exceptions import DependencyError, NotFoundError
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.shared.repositories import (
        AgentTemplateRepo,
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


# P-05 (CR 2026-05-11) — tool-arguments redaction.
#
# Distinct from ``_redact_connection_config`` (which understands the
# known structural shape ``{env, headers, ...}`` of a connection_config
# dict), ``_redact_arguments`` walks arbitrary user-supplied dicts/lists
# recursively and masks values whose KEY matches a secret-name pattern
# (case-insensitive substring). This is the helper Story 2.6 P-05 calls
# out for the audit payload : tool callers can supply any shape, secret-
# bearing keys can appear at any depth, under any of many synonyms.
_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "_key",  # covers openai_key, private_key, access_key, signing_key, etc.
    "key_",  # covers key_id, key_secret, etc.
    "auth",  # matches authorization, auth_header, bearer_auth, etc.
    "bearer",
    "credential",  # credentials, aws_credential, etc.
    "passphrase",
    "client_secret",
)


def _key_is_secret(key: str) -> bool:
    """True if ``key`` (case-insensitive) contains any secret-name pattern."""
    lkey = key.lower()
    return any(pat in lkey for pat in _SECRET_KEY_PATTERNS)


def _redact_arguments(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact secret-bearing values in arbitrary tool arguments.

    Walks dicts + lists ; masks values whose KEY (in a parent dict)
    matches a secret-name pattern. Non-dict / non-list values are returned
    as-is. Depth-bounded at 8 levels (defensive against pathological
    nested input).
    """
    if depth > 8:
        return "<redacted-deep>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if _key_is_secret(key_str):
                result[key_str] = "<redacted>"
            else:
                result[key_str] = _redact_arguments(v, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_redact_arguments(item, depth=depth + 1) for item in value]
    return value


class ToolHubService:
    """Orchestrate MCP server registration + tool discovery (Story 2.5)."""

    def __init__(
        self,
        *,
        server_repo: ToolServerRepo,
        tool_repo: ToolRepo,
        template_repo: AgentTemplateRepo | None = None,
    ) -> None:
        # ``template_repo`` is optional Sprint 1 — only ``invoke_tool``
        # uses it (P-11 CR 2026-05-11 : verify ``agent_template_id`` FK
        # before audit). Connect/list/get paths don't need it ; passing
        # None at construction time keeps existing call-sites working.
        self._server_repo = server_repo
        self._tool_repo = tool_repo
        self._template_repo = template_repo

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

    async def invoke_tool(
        self,
        *,
        server_id: UUID,
        tool_id: UUID,
        arguments: dict[str, Any],
        agent_template_id: UUID | None = None,
        timeout: float = 30.0,
        sandbox_backend: SandboxBackend | None = None,
        tenant_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Invoke a tool at runtime — Story 2.6 AC1+AC5.

        Sprint 1 — ephemeral session per call (no MCP pool — D61 defer
        Sprint 2). Audit event ``m5.tool.invoked`` is published in a
        single transaction with the response timing (status ∈ success /
        timeout / error). If the audit publish fails after a successful
        call, the call result is still returned to the caller (the audit
        gap window is documented in D68 defer Story 9.1).

        Parameters
        ----------
        server_id, tool_id
            Both must reference rows that exist in ``tool_servers`` /
            ``tools`` (404 ``NotFoundError`` otherwise).
        arguments
            Tool-specific input dict. Persisted in the audit event as
            ``args_redacted`` (P-03 redaction reuse).
        agent_template_id
            Optional FK to the agent that invoked the tool — used by
            Trace Explorer (Story 8.x) and audit (Story 9.1).
        timeout
            Wall-clock cap. Default 30s — tunable per call (workflow
            engine Story 4.x will tune per step).
        sandbox_backend
            Force a backend (test override). ``None`` = auto-detect.
        tenant_id
            Multi-tenant defer Story 12 — always ``None`` Sprint 1.

        Raises
        ------
        NotFoundError
            Server or tool does not exist, OR tool returned ``isError=True``.
        DependencyError
            Sandbox crash / timeout / SDK protocol error.
        """
        # 1. Resolve server + tool rows (NotFoundError if missing).
        async with self._server_repo.with_tenant(tenant_id) as session:
            server = await self._server_repo.get_by_id_in_session(session, server_id)
            if server is None:
                raise NotFoundError(
                    detail=f"Tool server '{server_id}' not found",
                    context={"server_id": str(server_id)},
                )
            tool = await self._tool_repo.get_by_id_in_session(session, tool_id)
            if tool is None or tool.server_id != server_id:
                raise NotFoundError(
                    detail=f"Tool '{tool_id}' not found on server '{server_id}'",
                    context={"server_id": str(server_id), "tool_id": str(tool_id)},
                )
            tool_name = tool.name
            transport = server.transport
            connection_config = dict(server.connection_config or {})

        # P-11 (CR 2026-05-11) — verify ``agent_template_id`` exists (when
        # supplied) before consuming the audit attribution. Without this
        # check, an authenticated caller could attribute invocations to
        # any template UUID, polluting the audit trail. Allowlist runtime
        # ("tool in agent_template's assigned_tools") remains Story 4.x.
        if agent_template_id is not None and self._template_repo is not None:
            async with self._template_repo.with_tenant(tenant_id) as session:
                template = await self._template_repo.get_by_id_in_session(
                    session, agent_template_id
                )
            if template is None:
                raise NotFoundError(
                    detail=f"Agent template '{agent_template_id}' not found",
                    context={
                        "agent_template_id": str(agent_template_id),
                    },
                )

        # 2. Resolve the actual sandbox backend ONCE (P-03 CR 2026-05-11)
        #    — never default to literal "bwrap" since detect_sandbox_backend
        #    may pick "setrlimit" in containers where bwrap is non-functional.
        #    The resolved value is forwarded to call_tool AND used in the
        #    audit event + response, so all three see the same truth.
        from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

        effective_backend: SandboxBackend = sandbox_backend or detect_sandbox_backend()
        start = time.monotonic()
        status: Literal["success", "timeout", "error"] = "error"
        result: dict[str, Any] = {}
        try:
            result = await call_tool(
                transport=transport,  # type: ignore[arg-type]  # Literal narrowed by DB CHECK
                connection_config=connection_config,
                tool_name=tool_name,
                arguments=arguments,
                timeout=timeout,
                backend=effective_backend,
            )
            status = "success"
        except MCPExecutionTimeoutError as exc:
            status = "timeout"
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._publish_invoked(
                session_factory_owner=self._server_repo,
                tool_id=tool_id,
                server_id=server_id,
                agent_template_id=agent_template_id,
                tool_name=tool_name,
                arguments=arguments,
                duration_ms=duration_ms,
                status=status,
                sandbox_backend=effective_backend,
                tenant_id=tenant_id,
            )
            raise DependencyError(
                detail=f"MCP tool '{tool_name}' execution timeout",
                context={
                    "server_id": str(server_id),
                    "tool_id": str(tool_id),
                    "timeout_seconds": exc.timeout,
                },
            ) from exc
        except MCPToolError as exc:
            status = "error"
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._publish_invoked(
                session_factory_owner=self._server_repo,
                tool_id=tool_id,
                server_id=server_id,
                agent_template_id=agent_template_id,
                tool_name=tool_name,
                arguments=arguments,
                duration_ms=duration_ms,
                status=status,
                sandbox_backend=effective_backend,
                tenant_id=tenant_id,
            )
            raise NotFoundError(
                detail=f"MCP tool '{tool_name}' returned error: {exc.detail}",
                context={
                    "server_id": str(server_id),
                    "tool_id": str(tool_id),
                    "tool_name": tool_name,
                },
            ) from exc
        except MCPExecutionError as exc:
            status = "error"
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._publish_invoked(
                session_factory_owner=self._server_repo,
                tool_id=tool_id,
                server_id=server_id,
                agent_template_id=agent_template_id,
                tool_name=tool_name,
                arguments=arguments,
                duration_ms=duration_ms,
                status=status,
                sandbox_backend=effective_backend,
                tenant_id=tenant_id,
            )
            raise DependencyError(
                detail=f"MCP tool '{tool_name}' subprocess error (rc={exc.returncode})",
                context={
                    "server_id": str(server_id),
                    "tool_id": str(tool_id),
                    "returncode": exc.returncode,
                },
            ) from exc

        duration_ms = int((time.monotonic() - start) * 1000)

        # 3. Audit event m5.tool.invoked (success path).
        #
        # P-08 (CR 2026-05-11) — wrap audit publish in try/except : the tool
        # has ALREADY executed (with side effects), so an audit-publish DB
        # failure must NOT shadow the successful result. Returning the tool
        # result here is better than 500-ing the caller (which would retry
        # and multiply side effects). The audit gap is documented in D68 ;
        # background recovery jobs can replay missing audits.
        try:
            await self._publish_invoked(
                session_factory_owner=self._server_repo,
                tool_id=tool_id,
                server_id=server_id,
                agent_template_id=agent_template_id,
                tool_name=tool_name,
                arguments=arguments,
                duration_ms=duration_ms,
                status=status,
                sandbox_backend=effective_backend,
                tenant_id=tenant_id,
            )
        except Exception as audit_exc:
            _log.warning(
                "tool_invoked_audit_publish_failed",
                tool_id=str(tool_id),
                server_id=str(server_id),
                tool_name=tool_name,
                duration_ms=duration_ms,
                error_type=type(audit_exc).__name__,
                error_message=str(audit_exc)[:200],
            )

        _log.info(
            "tool_invoked",
            tool_id=str(tool_id),
            server_id=str(server_id),
            tool_name=tool_name,
            duration_ms=duration_ms,
            status=status,
            sandbox_backend=effective_backend,
        )

        return result

    async def _publish_invoked(
        self,
        *,
        session_factory_owner: Any,
        tool_id: UUID,
        server_id: UUID,
        agent_template_id: UUID | None,
        tool_name: str,
        arguments: dict[str, Any],
        duration_ms: int,
        status: Literal["success", "timeout", "error"],
        sandbox_backend: SandboxBackend,
        tenant_id: UUID | None,
    ) -> None:
        """Publish ``m5.tool.invoked`` in a fresh single-tx session.

        Separated from ``invoke_tool`` so the success / timeout / error
        branches all use the same publish path with their own ``status``
        + ``duration_ms``. Best-effort post-commit ``emit_notify`` follows
        the Story 2.5 P-05 pattern (real event_type, not a fabricated label).
        """
        # P-05 (CR 2026-05-11) — use the dedicated arguments helper, which
        # walks dicts/lists recursively and masks by case-insensitive
        # secret-name patterns (bearer, openai_key, private_key, nested, …).
        # ``_redact_connection_config`` understood only the env/headers
        # shape, missing the long tail of secret-key synonyms callers use
        # in tool arguments.
        args_redacted = _redact_arguments(arguments)
        async with session_factory_owner.with_tenant(tenant_id) as session:
            event = ToolInvokedEvent(
                tool_id=tool_id,
                server_id=server_id,
                agent_template_id=agent_template_id,
                tool_name=tool_name,
                args_redacted=args_redacted,
                duration_ms=duration_ms,
                status=status,
                sandbox_backend=sandbox_backend,
                actor="system",
                tenant_id=tenant_id,
            )
            # TODO Story 9.1 — migrate to AuditEventRepo.record() — audit-event bypass cleanup (Epic 1 retro 2026-05-08).
            event_id = await publish(ToolInvokedEvent.event_type, event, session=session)
        try:
            await emit_notify(event_id, ToolInvokedEvent.event_type)
        except Exception:
            _log.warning(
                "event_bus_notify_failed_will_be_polled",
                event_id=str(event_id),
                event_type=ToolInvokedEvent.event_type,
            )


__all__ = ["ToolHubService"]
