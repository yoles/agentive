"""Pydantic v2 schemas — request/response models for ``/api/v1/memory/*``
(Story 3.1).

Same convention as ``features/tool_hub/schemas.py``: request models use
``ConfigDict(extra="forbid")`` (anti prompt-injection on free-shape body),
response models use ``extra="ignore"`` (defensive against ORM drift).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from agentive_backend.features.memory_manager.domain.value_objects import (
    DECAY_PARAM_NAMES,
    DECAY_PARAMS_BY_FUNCTION,
    SECONDS_MAX,
    SECONDS_MIN,
    DecayFunction,
    EmbeddingBackend,
)
from agentive_backend.shared.repositories.namespace_repo import NamespaceType

# `text-embedding-3-small` tops out at 8191 tokens. Past that,
# `OpenAIEmbeddings` silently splits the text and AVERAGES the resulting
# vectors, storing one diluted embedding that search will never retrieve.
# ~4 chars/token puts the practical ceiling here (code review Story 3.1, P2).
CONTENT_MAX_CHARS = 32_000

# `memory_chunks.ttl_seconds` is a 32-bit INTEGER, and `datetime + timedelta`
# overflows well before that. 10 years is far past any real retention need and
# keeps both safe (code review Story 3.1, P1). Aliased to the domain constant
# rather than repeating the literal: the HTTP bound and the domain bound have
# to be the same number, and two literals eventually drift (Story 3.4, P5).
TTL_MAX_SECONDS = SECONDS_MAX


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
    # `ge=SECONDS_MIN`, not `ge=0`: the domain rejects a zero-second TTL, so
    # accepting it here would turn a 422 into a 500 (code review Story 3.3, IG1).
    ttl: int | None = Field(default=None, ge=SECONDS_MIN, le=TTL_MAX_SECONDS)

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
    # Story 3.4 AC2 — opt OUT of the temporal-decay rerank. Defaults to
    # `True` because a namespace that configured a decay policy expects it
    # applied; on a namespace without one it is a no-op either way
    # (`DecayFunction.NONE` → factor 1.0 → `score == similarity`), and the
    # service skips ANN oversampling entirely in that case. Setting it to
    # `False` is how a caller asks for raw similarity ranking on a namespace
    # that *does* decay (debugging, or comparing the two orderings).
    # A body field, not a query param, for the same URL-hygiene reason as
    # `q`/`namespace` above.
    rerank: bool = Field(default=True)

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

    default_ttl_seconds: int | None = Field(default=None, ge=SECONDS_MIN, le=TTL_MAX_SECONDS)
    archive_after_seconds: int | None = Field(default=None, ge=SECONDS_MIN, le=TTL_MAX_SECONDS)

    @model_validator(mode="after")
    def _reject_empty_override(self) -> RetentionPolicyOverride:
        if self.default_ttl_seconds is None and self.archive_after_seconds is None:
            raise ValueError(
                "retention_policy must set at least one field; omit the "
                "field entirely (or send no retention_policy at all) to use "
                "the type default"
            )
        return self


class DecayPolicyOverride(BaseModel):
    """Optional per-namespace temporal decay configuration (Story 3.4 AC1).

    Mirror of :class:`RetentionPolicyOverride`, and like it a *replacement*
    rather than a merge. Omitting ``decay_policy`` entirely means "no
    decay" (``function: none``, factor 1.0, ``final_score == similarity``)
    — there is no per-type default to preserve here, deliberately: turning
    decay on by default would silently reorder every namespace created by
    Stories 3.1-3.3.

    The cross-field rules below duplicate :class:`DecayPolicy`'s own
    ``__post_init__`` on purpose, so an incoherent body is a **422 at the
    HTTP boundary** rather than a ``DomainValidationError`` escaping as a
    500 further in — exactly what ``RetentionPolicyOverride`` already does
    by re-declaring ``SECONDS_MIN``/``TTL_MAX_SECONDS`` bounds.
    """

    model_config = ConfigDict(extra="forbid")

    function: DecayFunction
    half_life_seconds: int | None = Field(default=None, ge=SECONDS_MIN, le=TTL_MAX_SECONDS)
    horizon_seconds: int | None = Field(default=None, ge=SECONDS_MIN, le=TTL_MAX_SECONDS)
    threshold_seconds: int | None = Field(default=None, ge=SECONDS_MIN, le=TTL_MAX_SECONDS)
    factor: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _enforce_function_parameters(self) -> DecayPolicyOverride:
        # The domain's table, not a copy of it. A local dict would be an
        # unguarded second source of truth: the domain's carries an
        # exhaustiveness `assert`, so a 5th `DecayFunction` member fails at
        # import; a copy would instead raise `KeyError` here, inside a
        # `model_validator`, i.e. a 500 on `POST /namespaces` where this class
        # exists precisely to produce a 422 (code review Story 3.4, P8).
        required = DECAY_PARAMS_BY_FUNCTION[self.function]
        for name in DECAY_PARAM_NAMES:
            value = getattr(self, name)
            if name in required and value is None:
                raise ValueError(f"{name} is required when function is {self.function.value!r}")
            if name not in required and value is not None:
                raise ValueError(
                    f"{name} is not a parameter of decay function "
                    f"{self.function.value!r}; remove it or change the function"
                )
        return self


class CreateNamespaceRequest(BaseModel):
    """Body of ``POST /api/v1/memory/namespaces`` (Story 3.2 AC1).

    ``embedding_backend`` (Story 3.6 AC2/AC3) picks which
    :class:`~agentive_backend.shared.llm.embedding_router.EmbeddingRouter`
    backend embeds this namespace's chunks — ``cloud`` (default,
    unchanged), ``local`` (FastEmbed), or ``voyage``. Set ONCE here: there
    is deliberately no way to change it later (see
    :class:`UpdateNamespaceRequest`'s docstring) — a namespace that already
    has chunks would have them go invisible to search if the backend
    changed underneath them (§ Symétrie write/read, this story's Dev
    Notes).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    type: NamespaceType
    department: str | None = Field(default=None, max_length=100)
    project: str | None = Field(default=None, max_length=100)
    retention_policy: RetentionPolicyOverride | None = None
    # Story 3.4 AC1 — omitted means "no decay" (see `DecayPolicyOverride`).
    decay_policy: DecayPolicyOverride | None = None
    embedding_backend: EmbeddingBackend = EmbeddingBackend.CLOUD

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


class UpdateNamespaceRequest(BaseModel):
    """Body of ``PATCH /api/v1/memory/namespaces/{name}`` (Story 3.4, code
    review BS1).

    Exists because `decay_policy` used to be settable at creation only, which
    made the whole feature unreachable on every namespace Stories 3.1-3.3 had
    already created — i.e. all of them. Enabling decay had no path short of
    hand-editing the JSONB, the very thing the read guards treat as suspect.

    `decay_policy` is required, and `null` is meaningful: it clears the policy
    back to "no decay". Leaving it optional would need a sentinel to tell
    "leave it alone" from "remove it", and with a single mutable field that
    ambiguity buys nothing. `retention_policy` is deliberately NOT mutable
    here: changing a TTL retroactively decides the fate of chunks already
    written, which is a Story 3.3 concern, not this one.

    ``embedding_backend`` (Story 3.6) is likewise NOT mutable here, on
    purpose — and unlike ``retention_policy``, not just "not yet": changing
    it on a namespace that already has chunks would make those chunks
    invisible to :meth:`~agentive_backend.features.memory_manager.service.MemoryManagerService.search`
    (§ Symétrie write/read, Dev Notes) — a bug, not a missing feature. With
    ``extra="forbid"`` a body attempting to set it already fails 422 with
    no code change needed here; a new namespace is the only path to a
    different backend.
    """

    model_config = ConfigDict(extra="forbid")

    decay_policy: DecayPolicyOverride | None


class NamespaceCreateView(BaseModel):
    """Response of ``POST /api/v1/memory/namespaces`` — 201 (Story 3.2 AC1)."""

    model_config = ConfigDict(extra="ignore")

    namespace_id: UUID
    name: str
    type: str
    department: str | None
    project: str | None
    retention_policy: dict[str, int | None]
    # Story 3.4 AC1 — the policy actually persisted, as `DecayPolicy`
    # serializes it: `{}` for "no decay", otherwise `function` plus that
    # function's own parameters only. Required, like its `retention_policy`
    # twin: a default would let a construction site that forgot the field
    # answer "no decay" instead of failing, which is the silent-drop mode
    # this story rejects everywhere else (code review Story 3.4, P11).
    decay_policy: dict[str, Any]
    embedding_backend: str
    created_at: datetime


class NamespaceUpdateView(NamespaceCreateView):
    """Response of ``PATCH /api/v1/memory/namespaces/{name}`` — 200.

    Same shape as the creation response, by subclassing rather than by
    copy: a caller that just reconfigured a namespace wants to read back
    exactly what creating it that way would have returned.
    """


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
    # Story 3.4 AC1/AC3 — same pair, same rationale, but tracked with its
    # OWN validity flag: a corrupt `retention_policy` must not brand a
    # healthy `decay_policy` invalid, nor the reverse. Required for the same
    # reason as on `NamespaceCreateView` (code review Story 3.4, P11).
    decay_policy: dict[str, Any]
    decay_policy_valid: bool = True
    embedding_backend: str
    chunk_count: int
    created_at: datetime


class MemorySearchResultView(BaseModel):
    """One result item of ``POST /api/v1/memory/search`` (Story 3.1 AC2,
    3.4 AC2).

    ``score`` is the FINAL score the results are ranked by:
    ``clamp(similarity, 0, 1) x decay_factor(age)`` (Story 3.4 AC1). The
    field keeps its name — it has been the ranking score since 3.1 — and
    its two terms are exposed alongside it:

    * ``similarity`` — ``clamp(1 - cosine_distance, 0, 1)``, what ``score``
      used to be before 3.4. Clamped because cosine distance ranges over
      ``[0, 2]``, so the raw value can be negative.
    * ``decay_factor`` — the namespace's :class:`DecayPolicy` evaluated at
      this chunk's age, always in ``[0, 1]``.

    On a namespace with no ``decay_policy`` (every namespace created before
    Story 3.4) ``decay_factor`` is ``1.0`` and ``score == similarity``, so
    the contract is unchanged in practice. Both terms are exposed because a
    score demoted by age is otherwise indistinguishable from a poor match.
    """

    model_config = ConfigDict(extra="ignore")

    chunk_id: UUID
    content: str
    score: float
    # Story 3.4 AC2 — the two terms of `score`, see the class docstring.
    similarity: float
    decay_factor: float
    namespace: str
    created_at: datetime
    # Story 3.3 AC2 — lets a caller distinguish a "live" result from one
    # only surfaced because `include_archived=true` was set.
    #
    # `archived_at` alone cannot make that distinction: `include_archived`
    # lifts BOTH retention filters, so a chunk whose TTL has elapsed but that
    # the daily worker has not archived yet (a window of up to `interval_s`)
    # comes back with `archived_at: null`, indistinguishable from a live hit.
    # `expires_at` closes it: past that instant the result is retained data,
    # not a search hit (code review Story 3.3, P10).
    archived_at: datetime | None = None
    expires_at: datetime | None = None


class MemoryChunkListItemView(BaseModel):
    """One item of ``GET /api/v1/memory/chunks`` — Story 3.6 T9.3 (AC5).

    Supports the admin "Config > Namespaces > detail" chunk table
    (``frontend/src/app/routes/config/namespaces/$name.tsx``) — the source
    a purge action (AC1's ``DELETE`` per selected row) reads ``chunk_id``
    from.
    """

    model_config = ConfigDict(extra="ignore")

    chunk_id: UUID
    namespace: str
    content: str
    created_at: datetime
    expires_at: datetime | None = None
    archived_at: datetime | None = None


__all__ = [
    "CONTENT_MAX_CHARS",
    "TTL_MAX_SECONDS",
    "CreateMemoryChunkRequest",
    "CreateNamespaceRequest",
    "DecayPolicyOverride",
    "MemoryChunkCreateView",
    "MemoryChunkListItemView",
    "MemorySearchResultView",
    "NamespaceCreateView",
    "NamespaceListItemView",
    "NamespaceUpdateView",
    "RetentionPolicyOverride",
    "SearchMemoryRequest",
    "UpdateNamespaceRequest",
    "ensure_embeddable_text",
]
