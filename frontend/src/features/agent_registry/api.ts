/**
 * API wrappers for `/api/v1/agents/*` — Story 2.1.
 *
 * Uses the shared `apiFetch` helper which handles RFC 7807 → ApiError mapping.
 */

import { apiFetch } from "@/shared/api/client";
import type {
  ArchetypeDetail,
  ArchetypeSummary,
  CreateTemplateRequest,
  CreateTemplateResponse,
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
