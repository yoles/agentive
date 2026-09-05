/**
 * API wrappers for `/api/v1/memory/namespaces` — Story 3.2.
 *
 * Uses the shared `apiFetch` helper which handles RFC 7807 → ApiError mapping.
 */

import { apiFetch } from "@/shared/api/client";
import type { CreateNamespaceRequest, Namespace, NamespaceListItem } from "./types";

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
