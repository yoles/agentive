/**
 * TanStack Query hooks for the Memory Manager namespaces + chunks —
 * Story 3.2, 3.6.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createNamespace, listChunks, listNamespaces, purgeChunk } from "./api";
import type { ListChunksParams } from "./api";
import type { CreateNamespaceRequest, MemoryChunkListItem, Namespace, NamespaceListItem } from "./types";

/** `useNamespaces` — admin listing of all namespaces + live chunk counts. */
export function useNamespaces() {
  return useQuery<NamespaceListItem[]>({
    queryKey: ["memory-namespaces"],
    queryFn: listNamespaces,
    staleTime: 30_000,
  });
}

/** `useCreateNamespace` — invalidates the listing on success. */
export function useCreateNamespace() {
  const queryClient = useQueryClient();
  return useMutation<Namespace, unknown, CreateNamespaceRequest>({
    mutationFn: createNamespace,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory-namespaces"] });
    },
  });
}

type ChunkFilters = Omit<ListChunksParams, "namespace">;

/** `useNamespaceChunks` — Story 3.6 AC5 admin chunk table.
 *
 * `namespace` is a separate key segment from `filters` so a purge mutation
 * can invalidate every filter combination for that namespace with a single
 * prefix match (`["memory-chunks", namespace]`), instead of having to
 * reproduce the exact filters object the currently-mounted table used. */
export function useNamespaceChunks(namespace: string, filters: ChunkFilters = {}) {
  return useQuery<MemoryChunkListItem[]>({
    queryKey: ["memory-chunks", namespace, filters],
    queryFn: () => listChunks({ namespace, ...filters }),
    staleTime: 10_000,
  });
}

/** `usePurgeChunk` — Story 3.6 AC1. Invalidates both this namespace's chunk
 * table (any filter combination) AND the namespace listing (`chunk_count`
 * changes too). */
export function usePurgeChunk(namespace: string) {
  const queryClient = useQueryClient();
  return useMutation<void, unknown, string>({
    mutationFn: purgeChunk,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory-chunks", namespace] });
      void queryClient.invalidateQueries({ queryKey: ["memory-namespaces"] });
    },
  });
}
