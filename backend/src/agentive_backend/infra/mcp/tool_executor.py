"""Execute a model-requested tool against MCP (Story 5.0 AC2/AC3).

The injected half of :mod:`agentive_backend.shared.llm.tool_loop`. The loop
lives in ``shared`` and cannot reach MCP (``.import-linter`` Contract 2:
``shared`` never imports ``infra``); this module is the ``infra`` side it
receives by injection.

**Why here and not in a feature.** Both ``playground`` and ``workflow_engine``
need it, and Contract 1 forbids them to import each other. Putting it in
either would force the other to duplicate it — which is exactly the debt this
codebase already carries on ``PlaygroundService``'s constants ("Keep in sync
manually", no parity test). ``infra`` is the lowest layer both can reach.

**It takes tools already resolved, and does no database access.** The caller
loads its assignments the way it already does, hands over the resolved set,
and this module stays a pure MCP adapter — testable without a repo, a session
or a migration.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from agentive_backend.infra.mcp.client import (
    MCPExecutionTimeoutError,
    call_tool,
)
from agentive_backend.shared.config import settings
from agentive_backend.shared.llm.security import wrap_external_input
from agentive_backend.shared.llm.types import ToolCall, ToolDefinition
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

#: Cap on the tool result text handed back to the model. A tool is an
#: unbounded source of text (a `grep` over a monorepo, a CI log), and the
#: mechanism meant to let an agent read things must not become the most
#: direct way to blow up a prompt — the same lesson Story 4.7 P-9 learned on
#: handoff summaries, applied before it bites rather than after.
MAX_TOOL_RESULT_CHARS = 8_000


@dataclass(frozen=True)
class ResolvedTool:
    """An assigned tool, joined with what it takes to actually call it."""

    tool_id: UUID
    server_id: UUID
    name: str
    description: str
    input_schema: dict[str, Any]
    transport: Literal["stdio", "sse"]
    connection_config: dict[str, Any]


@dataclass
class ToolInvocation:
    """What one execution did — the raw material for the Playground's
    ``tool_invocations`` (AC6) and for per-node metrics."""

    tool_id: UUID
    server_id: UUID
    tool_name: str
    arguments: dict[str, Any]
    result_summary: str
    duration_ms: int
    status: Literal["success", "error", "timeout"]


def to_tool_definitions(resolved: Mapping[str, ResolvedTool]) -> list[ToolDefinition]:
    """Project resolved tools into the provider-agnostic contract the loop
    offers to the model."""
    return [
        ToolDefinition(
            name=t.name,
            description=t.description,
            input_schema=t.input_schema,
        )
        for t in resolved.values()
    ]


@dataclass
class McpToolExecutor:
    """Callable matching ``shared.llm.tool_loop.ToolExecutor``.

    **Never raises for an ordinary tool failure.** A timeout, an MCP
    ``isError`` result or an unknown tool name is information the model can
    act on — it can try another tool, or say it could not find out. Raising
    would kill a run and throw away everything already billed, which is the
    opposite of what the surrounding error handling is for. Only a
    programming error escapes.
    """

    resolved: Mapping[str, ResolvedTool]
    invocations: list[ToolInvocation] = field(default_factory=list)

    async def __call__(self, call: ToolCall) -> str:
        started = time.perf_counter()
        tool = self.resolved.get(call.name)

        if tool is None:
            # The model asked for something it was not offered. Told plainly
            # rather than swallowed: an agent that hallucinates a tool name
            # can correct itself if it is told, and can do nothing at all if
            # the result is an empty string.
            _log.warning(
                "mcp.tool_executor_unknown_tool",
                requested=call.name,
                offered=sorted(self.resolved),
            )
            return self._wrap(f"ERROR: no tool named {call.name!r} is available to you.")

        status: Literal["success", "error", "timeout"]
        try:
            raw = await call_tool(
                transport=tool.transport,
                connection_config=tool.connection_config,
                tool_name=tool.name,
                arguments=call.arguments,
                # AC4, troisième plafond. Passé EXPLICITEMENT plutôt que
                # laissé au défaut de `call_tool` : le défaut de ce module-là
                # est dimensionné pour un appel isolé (Dry Run, Playground
                # manuel), pas pour une boucle qui peut en enchaîner 24.
                timeout=settings.tool_call_timeout_s,
            )
            text = self._render(raw)
            # MCP reports a tool-level failure in the payload, not by raising.
            # Treating that as success would hand the model an error message
            # formatted as a result.
            status = "error" if raw.get("isError") else "success"
        except MCPExecutionTimeoutError as exc:
            status = "timeout"
            text = f"ERROR: tool {tool.name!r} timed out: {exc}"
            _log.warning("mcp.tool_executor_timeout", tool=tool.name)
        except Exception as exc:
            status = "error"
            text = f"ERROR: tool {tool.name!r} failed: {type(exc).__name__}"
            # Logged with the traceback, but NOT surfaced to the model: an
            # exception message can carry a DSN, a path or a token, and this
            # string goes straight into a prompt (NFR9).
            _log.exception("mcp.tool_executor_failed", tool=tool.name)

        duration_ms = int((time.perf_counter() - started) * 1000)
        self.invocations.append(
            ToolInvocation(
                tool_id=tool.tool_id,
                server_id=tool.server_id,
                tool_name=tool.name,
                arguments=call.arguments,
                result_summary=text[:500],
                duration_ms=duration_ms,
                status=status,
            )
        )
        return self._wrap(text)

    @staticmethod
    def _render(raw: dict[str, Any]) -> str:
        """Flatten an MCP ``CallToolResult`` into text the model can read.

        Prefers the ``content`` blocks the protocol defines; falls back to
        JSON rather than to ``str(dict)``, which would hand the model Python
        repr syntax it has no reason to parse.
        """
        content = raw.get("content")
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if any(parts):
                return "\n".join(p for p in parts if p)
        try:
            return json.dumps(raw, ensure_ascii=False, default=str)
        except TypeError, ValueError:  # pragma: no cover — defensive
            return str(raw)

    @staticmethod
    def _wrap(text: str) -> str:
        """Truncate, then wrap — and this is the ONLY exit of this class.

        ``wrap_external_input`` is applied HERE, one layer below the loop, so
        that no caller can forget it. That is not paranoia: this is the fourth
        prompt-building surface in the repo, and the omission on the third
        (``PlaygroundService``) survived two full epics before a review found
        it (Story 9.7). Placing the guard at the only exit makes a fifth
        omission structurally impossible rather than merely discouraged.

        Truncation happens BEFORE wrapping so the cap applies to what the
        model actually reads — wrapping first would let escaping push the
        result past the ceiling it is supposed to respect (review 4.15,
        finding P-01, same ordering mistake).
        """
        if len(text) > MAX_TOOL_RESULT_CHARS:
            text = text[:MAX_TOOL_RESULT_CHARS] + "\n[… tronqué]"
        return wrap_external_input(text, "tool_output")
