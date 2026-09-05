/**
 * TanStack Query hooks for the Memory Manager namespaces — Story 3.2.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createNamespace, listNamespaces } from "./api";
import type { CreateNamespaceRequest, Namespace, NamespaceListItem } from "./types";

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
