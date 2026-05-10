/**
 * API wrappers for `/api/v1/agents/*` — Story 2.1 + 2.2.
 *
 * Uses the shared `apiFetch` helper which handles RFC 7807 → ApiError mapping.
 */

import { apiFetch } from "@/shared/api/client";
import type {
  AgentInstance,
  ArchetypeDetail,
  ArchetypeSummary,
  CreateTemplateRequest,
  CreateTemplateResponse,
  InstantiateTemplateRequest,
  InstantiateTemplateResponse,
  TemplateDetail,
  UpdateTemplateRequest,
  UpdateTemplateResponse,
} from "./types";

/** GET /api/v1/agents/archetypes — lean list (no prompt_base / contracts). */
export async function listArchetypes(): Promise<ArchetypeSummary[]> {
  return apiFetch<ArchetypeSummary[]>("/api/v1/agents/archetypes");
}

/** GET /api/v1/agents/archetypes/{id} — detail with prompt_base + contracts. */
export async function getArchetypeDetail(id: string): Promise<ArchetypeDetail> {
  return apiFetch<ArchetypeDetail>(`/api/v1/agents/archetypes/${encodeURIComponent(id)}`);
}

/** POST /api/v1/agents/templates — 201 Created. */
export async function createTemplate(
  body: CreateTemplateRequest,
): Promise<CreateTemplateResponse> {
  return apiFetch<CreateTemplateResponse>("/api/v1/agents/templates", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** GET /api/v1/agents/templates/{id} — Story 2.2 detail. */
export async function getTemplate(id: string): Promise<TemplateDetail> {
  return apiFetch<TemplateDetail>(`/api/v1/agents/templates/${encodeURIComponent(id)}`);
}

/** PUT /api/v1/agents/templates/{id} — Story 2.2 PATCH-like update. */
export async function updateTemplate(
  id: string,
  body: UpdateTemplateRequest,
): Promise<UpdateTemplateResponse> {
  return apiFetch<UpdateTemplateResponse>(
    `/api/v1/agents/templates/${encodeURIComponent(id)}`,
    {
      method: "PUT",
      body: JSON.stringify(body),
    },
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Story 2.4 — Agent instance endpoints
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** POST /api/v1/agents/templates/{id}/instances — 201 Created (Story 2.4). */
export async function instantiateTemplate(
  templateId: string,
  body: InstantiateTemplateRequest = {},
): Promise<InstantiateTemplateResponse> {
  return apiFetch<InstantiateTemplateResponse>(
    `/api/v1/agents/templates/${encodeURIComponent(templateId)}/instances`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** GET /api/v1/agents/instances/{id} — Story 2.4 detail. */
export async function getInstance(instanceId: string): Promise<AgentInstance> {
  return apiFetch<AgentInstance>(`/api/v1/agents/instances/${encodeURIComponent(instanceId)}`);
}

/** GET /api/v1/workflows/runs/{run_id}/instances — Story 2.4. */
export async function listInstancesByRun(runId: string): Promise<AgentInstance[]> {
  return apiFetch<AgentInstance[]>(
    `/api/v1/workflows/runs/${encodeURIComponent(runId)}/instances`,
  );
}
