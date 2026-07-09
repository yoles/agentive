/**
 * Domain types for the Playground feature — Story 2.7.
 *
 * Mirror the backend Pydantic schemas in
 * `backend/src/agentive_backend/features/playground/schemas.py`.
 */

export type ToolInvocationStatus = "success" | "error" | "timeout";

export type ToolInvocationLog = {
  tool_id: string;
  tool_name: string;
  server_id: string;
  arguments_redacted: Record<string, unknown>;
  result_summary: string;
  duration_ms: number;
  status: ToolInvocationStatus;
};

export type RunPlaygroundRequest = {
  arguments: Record<string, unknown>;
  enabled_tool_ids?: string[] | null;
  timeout_seconds?: number;
};

export type RunPlaygroundResponse = {
  prompt_resolved: string;
  raw_output: string;
  parsed_output: Record<string, unknown> | null;
  tokens: { input_tokens: number; output_tokens: number };
  cost_estimate_usd: string | null; // Pydantic Decimal → string
  model_used: string;
  provider_used: string;
  tool_invocations: ToolInvocationLog[];
  duration_ms_total: number;
};
