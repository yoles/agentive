/**
 * TanStack Query hooks for the Tool Hub — Story 2.5.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createToolServer,
  deleteAgentTool,
  getToolServer,
  listAgentTools,
  listToolServers,
  replaceAgentTools,
} from "./api";
import type {
  AgentToolsResponse,
  CreateToolServerRequest,
  ReplaceAgentToolsRequest,
  ToolServer,
  ToolServerDetail,
} from "./types";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `useToolServers` — list of all registered MCP servers (lean). */
export function useToolServers() {
  return useQuery<ToolServer[]>({
    queryKey: ["tool-servers"],
    queryFn: listToolServers,
    staleTime: 30_000,
  });
}

/** `useToolServer` — one server with its full tools list. */
export function useToolServer(serverId: string | null | undefined) {
  return useQuery<ToolServerDetail>({
    queryKey: ["tool-server", serverId],
    queryFn: () => getToolServer(serverId as string),
    enabled: serverId != null && serverId !== "" && UUID_RE.test(serverId),
    staleTime: 30_000,
  });
}

/** `useCreateToolServer` — POST + discovery mutation. Invalidates the
 * servers list on success so the new entry appears immediately. */
export function useCreateToolServer() {
  const queryClient = useQueryClient();
  return useMutation<ToolServerDetail, unknown, CreateToolServerRequest>({
    mutationFn: createToolServer,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tool-servers"] });
    },
  });
}

/** `useAgentTools` — list tools assigned to an agent template. */
export function useAgentTools(templateId: string | null | undefined) {
  return useQuery<AgentToolsResponse>({
    queryKey: ["agent-template", templateId, "tools"],
    queryFn: () => listAgentTools(templateId as string),
    enabled: templateId != null && templateId !== "" && UUID_RE.test(templateId),
    staleTime: 30_000,
  });
}

/** `useReplaceAgentTools` — REPLACE the assignments for one template.
 * Invalidates the per-template tools queryKey. */
export function useReplaceAgentTools(templateId: string) {
  const queryClient = useQueryClient();
  return useMutation<AgentToolsResponse, unknown, ReplaceAgentToolsRequest>({
    mutationFn: (body) => replaceAgentTools(templateId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["agent-template", templateId, "tools"],
      });
    },
  });
}

/** `useDeleteAgentTool` — granular unassignment of 1 tool. */
export function useDeleteAgentTool(templateId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: (toolId) => deleteAgentTool(templateId, toolId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["agent-template", templateId, "tools"],
      });
    },
  });
}
