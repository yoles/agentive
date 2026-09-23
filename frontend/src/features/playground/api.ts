/**
 * API wrapper for `/api/v1/playground/agents/{templateId}/run` — Story 2.7.
 *
 * Uses the shared `apiFetch` helper which handles RFC 7807 → ApiError mapping.
 */

import { apiFetch } from "@/shared/api/client";
import type { RunPlaygroundRequest, RunPlaygroundResponse } from "./types";

/** POST /api/v1/playground/agents/{templateId}/run — runs an agent in isolation. */
export async function runPlayground(
  templateId: string,
  body: RunPlaygroundRequest,
): Promise<RunPlaygroundResponse> {
  return apiFetch<RunPlaygroundResponse>(
    `/api/v1/playground/agents/${encodeURIComponent(templateId)}/run`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}
