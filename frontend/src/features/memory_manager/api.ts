/**
 * API wrappers for `/api/v1/memory/namespaces` and `/api/v1/memory/chunks`
 * — Story 3.2, 3.6.
 *
 * Uses the shared `apiFetch` helper which handles RFC 7807 → ApiError mapping.
 */

import { apiFetch } from "@/shared/api/client";
import type {
  CreateNamespaceRequest,
  MemoryChunkListItem,
  Namespace,
  NamespaceListItem,
} from "./types";

/** GET /api/v1/memory/namespaces — admin listing, all departments. */
export async function listNamespaces(): Promise<NamespaceListItem[]> {
  return apiFetch<NamespaceListItem[]>("/api/v1/memory/namespaces");
}

/** POST /api/v1/memory/namespaces — create (201). */
export async function createNamespace(body: CreateNamespaceRequest): Promise<Namespace> {
  return apiFetch<Namespace>("/api/v1/memory/namespaces", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type ListChunksParams = {
  namespace: string;
  includeArchived?: boolean;
  contentContains?: string;
  createdAfter?: string; // ISO 8601
  createdBefore?: string; // ISO 8601
  limit?: number;
  offset?: number;
};

/** GET /api/v1/memory/chunks — admin chunk listing for a namespace (Story 3.6 AC5). */
export async function listChunks(params: ListChunksParams): Promise<MemoryChunkListItem[]> {
  const search = new URLSearchParams({ namespace: params.namespace });
  if (params.includeArchived) search.set("include_archived", "true");
  if (params.contentContains) search.set("content_contains", params.contentContains);
  if (params.createdAfter) search.set("created_after", params.createdAfter);
  if (params.createdBefore) search.set("created_before", params.createdBefore);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.offset !== undefined) search.set("offset", String(params.offset));
  return apiFetch<MemoryChunkListItem[]>(`/api/v1/memory/chunks?${search.toString()}`);
}

/** DELETE /api/v1/memory/chunks/{chunk_id} — manual purge (Story 3.6 AC1).
 *
 * 204 on success. Idempotent per the backend contract — a second purge of
 * the same chunk is a 404, never a 204 again; callers loop this per
 * selected chunk with `Promise.allSettled` (never `Promise.all`), so one
 * chunk purged concurrently elsewhere does not fail the whole selection. */
export async function purgeChunk(chunkId: string): Promise<void> {
  await apiFetch<undefined>(`/api/v1/memory/chunks/${chunkId}`, { method: "DELETE" });
}
