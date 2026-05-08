/**
 * TanStack Query hooks for the Agent Registry — Story 2.1.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createTemplate, getArchetypeDetail, listArchetypes } from "./api";
import type {
  ArchetypeDetail,
  ArchetypeSummary,
  CreateTemplateRequest,
  CreateTemplateResponse,
} from "./types";

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
      // Invalidate the (future) templates list once Story 2.2 ships it.
      void queryClient.invalidateQueries({ queryKey: ["agent-templates"] });
    },
  });
}
