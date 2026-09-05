"""Pydantic v2 schemas — request/response models for ``/api/v1/memory/*``
(Story 3.1).

Same convention as ``features/tool_hub/schemas.py``: request models use
``ConfigDict(extra="forbid")`` (anti prompt-injection on free-shape body),
response models use ``extra="ignore"`` (defensive against ORM drift).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from agentive_backend.shared.repositories.namespace_repo import NamespaceType

# `text-embedding-3-small` tops out at 8191 tokens. Past that,
# `OpenAIEmbeddings` silently splits the text and AVERAGES the resulting
# vectors, storing one diluted embedding that search will never retrieve.
# ~4 chars/token puts the practical ceiling here (code review Story 3.1, P2).
CONTENT_MAX_CHARS = 32_000

# `memory_chunks.ttl_seconds` is a 32-bit INTEGER, and `datetime + timedelta`
# overflows well before that. 10 years is far past any real retention need and
# keeps both safe (code review Story 3.1, P1).
TTL_MAX_SECONDS = 315_360_000


def ensure_embeddable_text(value: str, *, field: str) -> str:
    """Strip ``value`` and reject what the storage/embedding layers cannot take.

    Three rejections, each of which used to surface as a raw 500 instead of a
    422 (code review Story 3.1, P3/P8):

    * blank or whitespace-only (Pydantic's ``min_length`` does not strip),
    * NUL byte, which Postgres refuses in a ``text`` column,
    * lone surrogate, which breaks ``str.encode("utf-8")`` in the embedder.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not be blank or whitespace-only")
    if "\x00" in stripped:
        raise ValueError(f"{field} must not contain NUL (0x00) characters")
    try:
        stripped.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 (lone surrogates rejected)") from exc
    return stripped


class CreateMemoryChunkRequest(BaseModel):
    """Body of ``POST /api/v1/memory/chunks`` (Story 3.1 AC1).

    ``namespace`` is the namespace **name** (not a UUID) — resolved via
    ``NamespaceRepo.require_by_name``, 404 if unknown (namespace creation
    is Story 3.2, out of scope here).
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=CONTENT_MAX_CHARS)
    namespace: str = Field(min_length=1, max_length=255)
    ttl: int | None = Field(default=None, ge=0, le=TTL_MAX_SECONDS)

    @field_validator("content", "namespace", mode="after")
    @classmethod
    def _reject_unstorable_text(cls, value: str, info: ValidationInfo) -> str:
        return ensure_embeddable_text(value, field=info.field_name or "value")


class MemoryChunkCreateView(BaseModel):
    """Response of ``POST /api/v1/memory/chunks`` — 201 (Story 3.1 AC1)."""

    model_config = ConfigDict(extra="ignore")

    chunk_id: UUID
    namespace: str
    content: str
    # The EFFECTIVE TTL (per-chunk override, else the namespace's
    # `default_ttl_seconds`), not just the raw override, which could read
    # `null` while `expires_at` was set from a namespace default, misleading
    # a caller into concluding "never expires" (code review Story 3.1, BS3).
    ttl_seconds: int | None
    expires_at: datetime | None
    embedding_model: str
    created_at: datetime


class SearchMemoryRequest(BaseModel):
    """Body of ``POST /api/v1/memory/search`` (Story 3.1 AC2).

    AC2 originally prescribed ``GET /memory/search?q=...``. A GET query
    string lands in clear text in access logs, the reverse proxy and the
    APM, none of which this codebase controls or scrubs, and the
    ``Embedder`` protocol already documents (NFR9) that request text is
    never redacted upstream. Moving ``q`` (and, for the same reason,
    ``namespace``) into a POST body keeps the search text out of the URL,
    hence out of anything that logs URLs (code review Story 3.1, BS4).
    """

    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=CONTENT_MAX_CHARS)
    namespace: str = Field(min_length=1, max_length=255)
    top_k: int = Field(default=5, ge=1, le=50)
    # Story 3.3 AC2 — lifts BOTH the `archived_at` and `expires_at` filters
    # for audit recovery (see `ChunkEmbeddingRepo.search_ann` docstring).
    # A body field, not a query param, for the same URL-hygiene reason as
    # `q`/`namespace` above.
    include_archived: bool = Field(default=False)

    @field_validator("q", "namespace", mode="after")
    @classmethod
    def _reject_unstorable_text(cls, value: str, info: ValidationInfo) -> str:
        return ensure_embeddable_text(value, field=info.field_name or "value")


class RetentionPolicyOverride(BaseModel):
    """Optional override of a namespace's default retention (Story 3.2 AC1).

    Replaces the type default entirely when provided (no field-by-field
    merge): a field left unset here means "unlimited" for that field, not
    "keep the type default". An override with every field unset would
    therefore silently produce an unlimited-retention namespace, defeating
    AC1's per-type default (code review Story 3.2, BS1). Rejected instead:
    at least one field must be explicit.
    """

    model_config = ConfigDict(extra="forbid")

    default_ttl_seconds: int | None = Field(default=None, ge=0, le=TTL_MAX_SECONDS)
    archive_after_seconds: int | None = Field(default=None, ge=0, le=TTL_MAX_SECONDS)

    @model_validator(mode="after")
    def _reject_empty_override(self) -> RetentionPolicyOverride:
        if self.default_ttl_seconds is None and self.archive_after_seconds is None:
            raise ValueError(
                "retention_policy must set at least one field; omit the "
                "field entirely (or send no retention_policy at all) to use "
                "the type default"
            )
        return self


class CreateNamespaceRequest(BaseModel):
    """Body of ``POST /api/v1/memory/namespaces`` (Story 3.2 AC1).

    ``embedding_backend`` is NOT exposed here — always hardcoded to
    ``"cloud"`` server-side (same anti-scope as Story 3.1 T1.6, no other
    backend is wired before Story 3.6).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    type: NamespaceType
    department: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)
    retention_policy: RetentionPolicyOverride | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _reject_unstorable_text(cls, value: str) -> str:
        return ensure_embeddable_text(value, field="name")

    @field_validator("department", "project", mode="after")
    @classmethod
    def _reject_unstorable_optional_text(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        """Same guard as ``name``, minus the ``required`` cardinality.

        Previously only ``name`` went through ``ensure_embeddable_text``,
        so a NUL byte or lone surrogate in ``department``/``project`` sailed
        through Pydantic and 500'd at ``session.flush()`` instead of 422
        (code review Story 3.2, P4).
        """
        if value is None:
            return None
        return ensure_embeddable_text(value, field=info.field_name or "value")


class NamespaceCreateView(BaseModel):
    """Response of ``POST /api/v1/memory/namespaces`` — 201 (Story 3.2 AC1)."""

    model_config = ConfigDict(extra="ignore")

    namespace_id: UUID
    name: str
    type: str
    department: str | None
    project: str | None
    retention_policy: dict[str, int | None]
    embedding_backend: str
    created_at: datetime


class NamespaceListItemView(BaseModel):
    """One item of ``GET /api/v1/memory/namespaces`` — 200 (Story 3.2 AC3)."""

    model_config = ConfigDict(extra="ignore")

    namespace_id: UUID
    name: str
    type: str
    department: str | None
    project: str | None
    retention_policy: dict[str, int | None]
    # `False` only when the DB row's `retention_policy` JSONB could not be
    # parsed (hand-seeded/legacy data) — `retention_policy` then falls back
    # to an empty policy, which reads exactly like a legitimate unlimited
    # `client` namespace unless this field says otherwise (product
    # decision, code review Story 3.2, IG2).
    retention_policy_valid: bool = True
    embedding_backend: str
    chunk_count: int
    created_at: datetime


class MemorySearchResultView(BaseModel):
    """One result item of ``POST /api/v1/memory/search`` (Story 3.1 AC2).

    ``score = 1 - cosine_distance`` — raw similarity, no temporal decay
    (``final_score = similarity x decay(age)`` is Story 3.4, out of scope
    here).
    """

    model_config = ConfigDict(extra="ignore")

    chunk_id: UUID
    content: str
    score: float
    namespace: str
    created_at: datetime
    # Story 3.3 AC2 — lets a caller distinguish a "live" result from one
    # only surfaced because `include_archived=true` was set.
    archived_at: datetime | None = None


__all__ = [
    "CONTENT_MAX_CHARS",
    "TTL_MAX_SECONDS",
    "CreateMemoryChunkRequest",
    "CreateNamespaceRequest",
    "MemoryChunkCreateView",
    "MemorySearchResultView",
    "NamespaceCreateView",
    "NamespaceListItemView",
    "RetentionPolicyOverride",
    "SearchMemoryRequest",
    "ensure_embeddable_text",
]
