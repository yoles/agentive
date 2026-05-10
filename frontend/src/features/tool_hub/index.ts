/**
 * Public surface — Tool Hub feature (Story 2.5).
 *
 * Same-feature internal imports go through this barrel to satisfy
 * `boundaries/dependencies` v6 (post-rétro Epic 1, 2026-05-08).
 */

export { AgentToolsPanel } from "./AgentToolsPanel";
export { AddToolServerDialog } from "./AddToolServerDialog";
export {
  createToolServer,
  deleteAgentTool,
  getToolServer,
  listAgentTools,
  listToolServers,
  replaceAgentTools,
} from "./api";
export {
  useAgentTools,
  useCreateToolServer,
  useDeleteAgentTool,
  useReplaceAgentTools,
  useToolServer,
  useToolServers,
} from "./hooks";
export type {
  AgentToolsResponse,
  AssignedTool,
  CreateToolServerRequest,
  ReplaceAgentToolsRequest,
  ToolInfo,
  ToolServer,
  ToolServerDetail,
  ToolServerStatus,
  Transport,
} from "./types";
