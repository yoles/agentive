"""Playground — Agent test isolation (Story 2.7, FR48).

Strict isolation contract (AC2) :
- This module MUST NOT import ``shared.event_bus.publish`` (or
  ``publish_and_commit``) except for the single audit event
  ``playground.run.completed`` (AC5) — P-29 fix-batch 2026-08-31, the
  docstring previously named a function this module never imports.
- This module MUST NOT import any ``MemoryChunkRepo`` (memory writes
  forbidden in Playground).
- This module MUST NOT import any ``AgentInstanceRepo.create_*`` method
  (no agent_instances row created — snapshot lives in memory only).

The PlaygroundService bypasses ``ToolHubService.invoke_tool`` and calls
``infra.mcp.client.call_tool`` directly — this is intentional to skip
the ``tool_hub.tool.invoked`` audit publish (Playground bypass).
"""

from __future__ import annotations

from agentive_backend.features.playground.router import router
from agentive_backend.features.playground.service import PlaygroundService

__all__ = ["PlaygroundService", "router"]
