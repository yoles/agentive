/**
 * TanStack Query hooks for the Agent Registry — Story 2.1 + 2.2.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createTemplate,
  getArchetypeDetail,
  getInstance,
  getTemplate,
  instantiateTemplate,
  listArchetypes,
  listInstancesByRun,
  updateTemplate,
} from "./api";
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

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Story 2.4 — Agent instance hooks (no UI consumer Sprint 1, ready for Story 8.x)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

/** `useInstantiateTemplate` — Story 2.4 POST mutation.
 *
 * Invalidates BOTH (a) the per-template instances queryKey AND (b) the
 * per-workflow-run instances queryKey when the response carries a
 * workflow_run_id. P-04 (CR 2026-05-10) — without (b), the Story 8.x trace
 * explorer fetched via `useInstancesByRun` saw a stale cache after a new
 * instance was created in the run.
 */
export function useInstantiateTemplate(templateId: string) {
  const queryClient = useQueryClient();
  return useMutation<InstantiateTemplateResponse, unknown, InstantiateTemplateRequest>({
    mutationFn: (body) => instantiateTemplate(templateId, body ?? {}),
    onSuccess: (response) => {
      void queryClient.invalidateQueries({
        queryKey: ["agent-template", templateId, "instances"],
      });
      if (response.workflow_run_id != null) {
        void queryClient.invalidateQueries({
          queryKey: ["workflow-run", response.workflow_run_id, "instances"],
        });
      }
    },
  });
}

/** `useInstance` — Story 2.4 detail fetch.
 *
 * P-04 (CR 2026-05-10) — `staleTime: Infinity` car le snapshot est
 * immutable par design (FR12, AC2). Pas de PUT/PATCH JAMAIS sur
 * `/agents/instances/{id}` (anti-scope spec). Refresh = invalidation
 * manuelle uniquement (ex : after `useInstantiateTemplate` succès).
 */
export function useInstance(instanceId: string | null | undefined) {
  return useQuery<AgentInstance>({
    queryKey: ["agent-instance", instanceId],
    queryFn: () => getInstance(instanceId as string),
    enabled: instanceId != null && instanceId !== "" && UUID_RE.test(instanceId),
    staleTime: Number.POSITIVE_INFINITY,
  });
}

/** `useInstancesByRun` — Story 2.4 list fetch (per workflow_run).
 *
 * `staleTime: 30_000` (pas Infinity) car la LISTE peut s'allonger pendant
 * la durée de vie d'un run (workflow_engine Story 4.x ajoutera des
 * instances au fur et à mesure). L'invalidation explicite via
 * `useInstantiateTemplate.onSuccess` couvre les ajouts qu'on initie nous-
 * mêmes ; le staleTime court couvre les ajouts initiés par d'autres
 * onglets / le worker.
 */
export function useInstancesByRun(runId: string | null | undefined) {
  return useQuery<AgentInstance[]>({
    queryKey: ["workflow-run", runId, "instances"],
    queryFn: () => listInstancesByRun(runId as string),
    enabled: runId != null && runId !== "" && UUID_RE.test(runId),
    staleTime: 30_000,
  });
}
