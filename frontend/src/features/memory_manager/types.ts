/**
 * Domain types for the Memory Manager feature — Story 3.2.
 *
 * Mirror the backend Pydantic schemas in
 * `backend/src/agentive_backend/features/memory_manager/schemas.py`.
 */

export type NamespaceType = "client" | "metier" | "operationnelle" | "contextuelle";

export type RetentionPolicy = {
  default_ttl_seconds: number | null;
  archive_after_seconds: number | null;
};

export type Namespace = {
  namespace_id: string;
  name: string;
  type: NamespaceType;
  department: string | null;
  project: string | null;
  retention_policy: RetentionPolicy;
  embedding_backend: string;
  created_at: string; // ISO 8601
};

export type NamespaceListItem = Namespace & {
  chunk_count: number;
};

export type CreateNamespaceRequest = {
  name: string;
  type: NamespaceType;
  department?: string | null;
  project?: string | null;
  retention_policy?: {
    default_ttl_seconds?: number | null;
    archive_after_seconds?: number | null;
  } | null;
};
