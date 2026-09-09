/**
 * Domain types for the Memory Manager feature — Story 3.2, 3.6.
 *
 * Mirror the backend Pydantic schemas in
 * `backend/src/agentive_backend/features/memory_manager/schemas.py`.
 */

export type NamespaceType = "client" | "metier" | "operationnelle" | "contextuelle";

/** Story 3.6 AC2/AC3 — mirrors the domain `EmbeddingBackend` enum. */
export type EmbeddingBackend = "cloud" | "local" | "voyage";

export type RetentionPolicy = {
  default_ttl_seconds: number | null;
  archive_after_seconds: number | null;
};

/** Story 3.4 AC1 — free-shape (function-scoped params), same JSONB the
 * backend's `DecayPolicy.to_mapping()` serializes: `{}` means "no decay". */
export type DecayPolicy = Record<string, unknown>;

export type Namespace = {
  namespace_id: string;
  name: string;
  type: NamespaceType;
  department: string | null;
  project: string | null;
  retention_policy: RetentionPolicy;
  decay_policy: DecayPolicy;
  embedding_backend: EmbeddingBackend;
  created_at: string; // ISO 8601
};

export type NamespaceListItem = Namespace & {
  chunk_count: number;
  // `false` only for a hand-seeded/legacy row whose `retention_policy`
  // JSONB could not be parsed — `retention_policy` then falls back to an
  // empty policy, which would otherwise render exactly like a legitimate
  // unlimited `client` namespace (product decision, code review Story
  // 3.2, IG2).
  retention_policy_valid: boolean;
  // Same rationale, independent flag — a corrupt `retention_policy` must
  // not brand a healthy `decay_policy` invalid, nor the reverse (Story 3.4).
  decay_policy_valid: boolean;
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
  // Story 3.6 AC2/AC3 — omitted defaults to `"cloud"` server-side, and is
  // immutable after creation (no PATCH path — see the backend's
  // `UpdateNamespaceRequest` docstring).
  embedding_backend?: EmbeddingBackend;
};

/** One row of `GET /api/v1/memory/chunks` — Story 3.6 AC5 (admin chunk table). */
export type MemoryChunkListItem = {
  chunk_id: string;
  namespace: string;
  content: string;
  created_at: string; // ISO 8601
  expires_at: string | null;
  archived_at: string | null;
};
