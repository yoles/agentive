/**
 * Public surface — Memory Manager feature (Story 3.2).
 *
 * Same-feature internal imports go through this barrel to satisfy
 * `boundaries/dependencies` v6 (post-rétro Epic 1, 2026-05-08).
 */

export { CreateNamespaceDialog } from "./CreateNamespaceDialog";
export { createNamespace, listNamespaces } from "./api";
export { departmentLabel, formatRetention, groupByDepartmentThenType } from "./format";
export { useCreateNamespace, useNamespaces } from "./hooks";
export type { NamespaceGroup } from "./format";
export type {
  CreateNamespaceRequest,
  Namespace,
  NamespaceListItem,
  NamespaceType,
  RetentionPolicy,
} from "./types";
