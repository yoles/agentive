/**
 * API wrappers for `/api/v1/tools/*` and `/api/v1/agents/templates/{id}/tools` — Story 2.5.
 *
 * Uses the shared `apiFetch` helper which handles RFC 7807 → ApiError mapping.
 */

import { apiFetch } from "@/shared/api/client";
import type {
  AgentToolsResponse,
  CreateToolServerRequest,
  ReplaceAgentToolsRequest,
  ToolServer,
  ToolServerDetail,
} from "./types";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Tool servers (m5)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** GET /api/v1/tools/servers — lean list. */
export async function listToolServers(): Promise<ToolServer[]> {
  return apiFetch<ToolServer[]>("/api/v1/tools/servers");
}

/** GET /api/v1/tools/servers/{id} — detail with tools. */
export async function getToolServer(serverId: string): Promise<ToolServerDetail> {
  return apiFetch<ToolServerDetail>(
    `/api/v1/tools/servers/${encodeURIComponent(serverId)}`,
  );
}

/** POST /api/v1/tools/servers — register + discover tools (201). */
export async function createToolServer(
  body: CreateToolServerRequest,
): Promise<ToolServerDetail> {
  return apiFetch<ToolServerDetail>("/api/v1/tools/servers", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Agent template ↔ tools assignment (m2 endpoints, owned by tool_hub feature)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** GET /api/v1/agents/templates/{id}/tools — list assigned tools. */
export async function listAgentTools(templateId: string): Promise<AgentToolsResponse> {
  return apiFetch<AgentToolsResponse>(
    `/api/v1/agents/templates/${encodeURIComponent(templateId)}/tools`,
  );
}

/** POST /api/v1/agents/templates/{id}/tools — REPLACE assignments. */
export async function replaceAgentTools(
  templateId: string,
  body: ReplaceAgentToolsRequest,
): Promise<AgentToolsResponse> {
  return apiFetch<AgentToolsResponse>(
    `/api/v1/agents/templates/${encodeURIComponent(templateId)}/tools`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** DELETE /api/v1/agents/templates/{templateId}/tools/{toolId} — unassign 1 tool (204). */
export async function deleteAgentTool(templateId: string, toolId: string): Promise<void> {
  await apiFetch<void>(
    `/api/v1/agents/templates/${encodeURIComponent(templateId)}/tools/${encodeURIComponent(toolId)}`,
    {
      method: "DELETE",
    },
  );
}
