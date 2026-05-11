/**
 * TanStack Query hooks for the Playground feature — Story 2.7.
 *
 * ``usePlaygroundRun`` is a `useMutation` (no cache : each click reruns).
 * ``usePlaygroundAssignedTools`` duplicates the Story 2.5 ``useAgentTools``
 * because `eslint-plugin-boundaries` forbids cross-feature imports
 * (playground → tool_hub). The duplication is intentional and small.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/shared/api/client";
import { runPlayground } from "./api";
import type { RunPlaygroundRequest, RunPlaygroundResponse } from "./types";

export function usePlaygroundRun(templateId: string) {
  return useMutation<RunPlaygroundResponse, Error, RunPlaygroundRequest>({
    mutationFn: (body) => runPlayground(templateId, body),
  });
}

/** Lean shape — only what the Playground form needs (anti-coupling). */
export type PlaygroundAssignedTool = {
  tool_id: string;
  name: string;
};

type AssignedToolsResponse = {
  template_id: string;
  assigned_tools: PlaygroundAssignedTool[];
};

export function usePlaygroundAssignedTools(templateId: string) {
  return useQuery<AssignedToolsResponse>({
    queryKey: ["playground-assigned-tools", templateId],
    queryFn: () =>
      apiFetch<AssignedToolsResponse>(
        `/api/v1/agents/templates/${encodeURIComponent(templateId)}/tools`,
      ),
    enabled: Boolean(templateId),
  });
}
