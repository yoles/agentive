"""Pydantic v2 schemas — request/response models for ``/api/v1/memory/*``
(Story 3.1).

Same convention as ``features/tool_hub/schemas.py``: request models use
``ConfigDict(extra="forbid")`` (anti prompt-injection on free-shape body),
response models use ``extra="ignore"`` (defensive against ORM drift).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

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

    @field_validator("q", "namespace", mode="after")
    @classmethod
    def _reject_unstorable_text(cls, value: str, info: ValidationInfo) -> str:
        return ensure_embeddable_text(value, field=info.field_name or "value")


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


__all__ = [
    "CONTENT_MAX_CHARS",
    "TTL_MAX_SECONDS",
    "CreateMemoryChunkRequest",
    "MemoryChunkCreateView",
    "MemorySearchResultView",
    "SearchMemoryRequest",
    "ensure_embeddable_text",
]
