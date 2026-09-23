/**
 * Domain types for the Tool Hub feature — Story 2.5.
 *
 * Mirror the backend Pydantic schemas in
 * `backend/src/agentive_backend/features/tool_hub/schemas.py` and
 * `backend/src/agentive_backend/features/agent_registry/schemas.py`
 * (assignment shapes).
 */

export type Transport = "stdio" | "sse";
export type ToolServerStatus = "active" | "inactive";

export type ToolInfo = {
  tool_id: string;
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown> | null;
};

export type ToolServer = {
  server_id: string;
  name: string;
  transport: Transport;
  status: ToolServerStatus;
  tools_count: number;
  discovered_at: string; // ISO 8601
};

export type ToolServerDetail = {
  server_id: string;
  name: string;
  transport: Transport;
  status: ToolServerStatus;
  connection_config: Record<string, unknown>;
  discovered_at: string;
  tools_count: number;
  tools: ToolInfo[];
};

export type CreateToolServerRequest = {
  name: string;
  transport: Transport;
  connection_config: Record<string, unknown>;
};

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Agent template ↔ tools junction (Story 2.5)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type AssignedTool = {
  tool_id: string;
  name: string;
  description: string;
  server_id: string;
  input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown> | null;
  assigned_at: string; // ISO 8601
};

export type AgentToolsResponse = {
  template_id: string;
  assigned_tools: AssignedTool[];
};

export type ReplaceAgentToolsRequest = {
  tool_ids: string[];
};
