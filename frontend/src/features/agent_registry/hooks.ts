/**
 * TanStack Query hooks for the Agent Registry — Story 2.1 + 2.2.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createTemplate,
  getArchetypeDetail,
  getTemplate,
  listArchetypes,
  updateTemplate,
} from "./api";
import type {
  ArchetypeDetail,
  ArchetypeSummary,
  CreateTemplateRequest,
  CreateTemplateResponse,
  TemplateDetail,
  UpdateTemplateRequest,
  UpdateTemplateResponse,
} from "./types";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * `useArchetypes` — list of 8 universal archetypes.
 * `staleTime: Infinity` because the registry is loaded once at backend
 * lifespan and does not change at runtime.
 */
export function useArchetypes() {
  return useQuery<ArchetypeSummary[]>({
    queryKey: ["archetypes"],
    queryFn: listArchetypes,
    staleTime: Number.POSITIVE_INFINITY,
  });
}

/** `useArchetypeDetail` — lazy detail fetch on archetype selection.
 *
 * Story 2.1 P-09 — `enabled` also rejects empty strings so a stray
 * `setArchetypeId("")` never fires a `GET /agents/archetypes/` (which
 * would 404 and surface a spurious error toast).
 */
export function useArchetypeDetail(id: string | null) {
  return useQuery<ArchetypeDetail>({
    queryKey: ["archetypes", id],
    queryFn: () => getArchetypeDetail(id as string),
    enabled: id != null && id !== "",
    staleTime: Number.POSITIVE_INFINITY,
  });
}

/** `useCreateTemplate` — POST mutation, invalidates the (future) templates list. */
export function useCreateTemplate() {
  const queryClient = useQueryClient();
  return useMutation<CreateTemplateResponse, unknown, CreateTemplateRequest>({
    mutationFn: createTemplate,
    onSuccess: () => {
      // Invalidate the (future) templates list once Story 2.4/2.7 ships it.
      void queryClient.invalidateQueries({ queryKey: ["agent-templates"] });
    },
  });
}

/** `useTemplate` — Story 2.2 detail fetch.
 *
 * Story 2.2 D4 fix — `enabled` rejects non-UUID `id` values so a malformed
 * route param (e.g. someone typing `/config/agents/abc`) does not fire a
 * spurious 422 on every render. The same regex lives in the route guard.
 */
export function useTemplate(id: string | null | undefined) {
  return useQuery<TemplateDetail>({
    queryKey: ["agent-template", id],
    queryFn: () => getTemplate(id as string),
    enabled: id != null && id !== "" && UUID_RE.test(id),
    staleTime: 30_000,
  });
}

/** `useUpdateTemplate` — Story 2.2 PUT mutation.
 *
 * Invalidates BOTH the detail (`["agent-template", id]`) and the future
 * templates list (`["agent-templates"]`) so the next navigation re-fetches
 * fresh data. Toast feedback + focus management live in the page component
 * to keep the hook reusable.
 */
export function useUpdateTemplate(id: string) {
  const queryClient = useQueryClient();
  return useMutation<UpdateTemplateResponse, unknown, UpdateTemplateRequest>({
    mutationFn: (body) => updateTemplate(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["agent-template", id] });
      void queryClient.invalidateQueries({ queryKey: ["agent-templates"] });
    },
  });
}
