/**
 * Public surface — Memory Manager feature (Story 3.2).
 *
 * Same-feature internal imports go through this barrel to satisfy
 * `boundaries/dependencies` v6 (post-rétro Epic 1, 2026-05-08).
 */

export { CreateNamespaceDialog } from "./CreateNamespaceDialog";
export { PurgeChunksDialog } from "./PurgeChunksDialog";
export { createNamespace, listChunks, listNamespaces, purgeChunk } from "./api";
export { departmentLabel, formatRetention, groupByDepartmentThenType } from "./format";
export { useCreateNamespace, useNamespaceChunks, useNamespaces, usePurgeChunk } from "./hooks";
export type { ListChunksParams } from "./api";
export type { NamespaceGroup } from "./format";
export type {
  CreateNamespaceRequest,
  DecayPolicy,
  EmbeddingBackend,
  MemoryChunkListItem,
  Namespace,
  NamespaceListItem,
  NamespaceType,
  RetentionPolicy,
} from "./types";
