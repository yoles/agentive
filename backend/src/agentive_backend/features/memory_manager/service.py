"""Service layer — Memory Manager storage + vector search (Story 3.1).

The service sits between the FastAPI router and the repositories:

1. Resolves the namespace by name (404 if unknown — namespace creation is
   Story 3.2, out of scope here).
2. **AC1** — computes ``expires_at`` via the domain :class:`RetentionPolicy`
   (service triggers, domain decides — audit §4.5), computes the embedding
   FIRST (code review Story 3.1, IG1: a failed embed call must never leave
   a durable chunk row with no vector), then writes the chunk row and
   upserts the embedding, compensating with a hard delete of the chunk if
   the embedding write itself fails. The response's ``ttl_seconds`` is the
   *effective* TTL (override, else the namespace's ``default_ttl_seconds``),
   not the raw override alone, so it never contradicts ``expires_at`` (code
   review Story 3.1, BS3).
3. **AC2** — computes the query embedding, delegates the ANN search to
   :meth:`ChunkEmbeddingRepo.search_ann`, then reranks its rows by
   ``similarity x decay_factor(age)`` (Story 3.4). The ANN pass is
   *oversampled* whenever that rerank can actually change the ordering —
   see :func:`~.reranker.fetch_k_for`.
4. **AC3** — isolation is satisfied by construction: every repo call goes
   through ``with_tenant(tenant_id)``, and Sprint 1 always passes
   ``tenant_id=None`` (mono-tenant MVP) so the ``tenant_isolation`` RLS
   policy matches all global rows. No department/project authorization
   check exists yet — that's Story 3.2's RLS département, which will plug
   in here once ``tenant_id``/department scoping is real.

Story 3.6 wires a real :class:`~agentive_backend.shared.llm.embedding_router.EmbeddingRouter`
in place of the single ``cloud``-only ``Embedder`` — every ``embed()`` call
below now resolves the ACTING namespace's ``embedding_backend`` first. See
§ Symétrie write/read in this story's Dev Notes for why ``create_chunk``
and ``search`` MUST derive the backend the same way, from the same
freshly-read namespace.
"""

from __future__ import annotations

import time
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from agentive_backend.features.memory_manager.domain.value_objects import (
    DecayPolicy,
    EmbeddingBackend,
    RetentionPolicy,
)
from agentive_backend.features.memory_manager.domain.value_objects import (
    NamespaceType as DomainNamespaceType,
)

# Aliased: `search()` takes a `rerank: bool` flag, which would shadow the
# function inside the method body.
from agentive_backend.features.memory_manager.reranker import fetch_k_for
from agentive_backend.features.memory_manager.reranker import rerank as rerank_rows
from agentive_backend.features.memory_manager.schemas import (
    MemoryChunkCreateView,
    MemoryChunkListItemView,
    MemorySearchResultView,
    NamespaceCreateView,
    NamespaceListItemView,
    NamespaceUpdateView,
)
from agentive_backend.shared.contracts.events import (
    MemoryChunkArchivedEvent,
    NamespaceAccessDeniedEvent,
    NamespaceCreatedEvent,
    NamespaceDecayPolicyUpdatedEvent,
)
from agentive_backend.shared.event_bus import notify_best_effort, publish
from agentive_backend.shared.exceptions import (
    DependencyError,
    ForbiddenError,
    InternalError,
    NotFoundError,
    ValidationError,
)
from agentive_backend.shared.logging import get_logger
from agentive_backend.shared.repositories.chunk_embedding_repo import (
    EF_SEARCH_DEFAULT,
    EF_SEARCH_MAX,
)
from agentive_backend.shared.repositories.namespace_repo import (
    NAMESPACE_LISTING_SAFETY_CAP,
    NamespaceType,
)

if TYPE_CHECKING:
    from agentive_backend.infra.db.models import Namespace
    from agentive_backend.shared.llm.embedding_router import EmbeddingRouter
    from agentive_backend.shared.repositories import (
        ChunkEmbeddingRepo,
        MemoryChunkRepo,
        NamespaceRepo,
    )

_log = get_logger(__name__)

# Story 3.4 T5.2 — `list_namespaces` now parses `retention_policy` and
# `decay_policy` under separate guards, so the retention guard can no
# longer lean on `NamespaceListItemView`'s own field validation to reject a
# malformed value. This adapter applies exactly that field's type
# (`dict[str, int | None]`) at the same point, preserving the P1 fix:
# `from_mapping` lets a float TTL through, and only `int` validation
# catches it (code review Story 3.2, P1).
_RETENTION_ADAPTER: TypeAdapter[dict[str, int | None]] = TypeAdapter(dict[str, int | None])


def _normalize_department(value: str) -> str:
    """Case- and whitespace-insensitive key for department comparison

    (product decision, code review Story 3.2, IG1). Only used to compare —
    the stored/echoed value is never altered.
    """
    return value.strip().casefold()


def _first_vector(vectors: list[list[float]], *, namespace_name: str, model: str) -> list[float]:
    """Return ``vectors[0]``, or raise 503 rather than ``IndexError``.

    The :class:`~agentive_backend.shared.llm.embedder.Embedder` protocol
    documents ordering but never cardinality, so an adapter returning ``[]``
    used to surface as an unclassified 500 (code review Story 3.1, P5).
    """
    if not vectors:
        raise DependencyError(
            detail="Embedding backend returned no vector for the input text.",
            context={"namespace": namespace_name, "model": model},
        )
    return vectors[0]


class MemoryManagerService:
    """Orchestrate memory chunk storage + vector search (Story 3.1)."""

    def __init__(
        self,
        *,
        memory_chunk_repo: MemoryChunkRepo,
        chunk_embedding_repo: ChunkEmbeddingRepo,
        namespace_repo: NamespaceRepo,
        embedding_router: EmbeddingRouter,
    ) -> None:
        self._memory_chunk_repo = memory_chunk_repo
        self._chunk_embedding_repo = chunk_embedding_repo
        self._namespace_repo = namespace_repo
        self._embedding_router = embedding_router

    async def create_chunk(
        self,
        *,
        namespace_name: str,
        content: str,
        ttl_seconds: int | None,
        tenant_id: UUID | None = None,
        acting_department: str | None = None,
    ) -> MemoryChunkCreateView:
        """Write a chunk + its embedding — Story 3.1 AC1.

        Raises
        ------
        NotFoundError
            ``namespace_name`` does not exist.
        ForbiddenError
            ``acting_department`` (Story 3.2 AC2) differs from the
            namespace's department.
        """
        start = time.monotonic()
        namespace = await self._namespace_repo.require_by_name(namespace_name, tenant_id=tenant_id)
        await self._check_department_access(
            namespace,
            acting_department=acting_department,
            operation="create_chunk",
            tenant_id=tenant_id,
        )

        # T3.2 — `now` captured once, before the insert, fed to both the
        # domain rule and the repo write. The row's real `created_at`
        # (server_default=func.now()) may differ by a few ms — negligible
        # at TTL scale (seconds/days).
        now = datetime.now(UTC)
        # `namespaces.retention_policy` is free-shape JSONB with no CHECK
        # constraint, so a hand-seeded row can carry a string TTL, a negative
        # value, or not be an object at all. Each used to escape as a raw 500
        # (code review Story 3.1, P4). Surface a proper RFC 7807 instead.
        try:
            policy = RetentionPolicy.from_mapping(namespace.retention_policy)
            expires_at = policy.expires_at(created_at=now, ttl_override_seconds=ttl_seconds)
            # BS3 : the response's `ttl_seconds` must agree with `expires_at`.
            # `ttl_seconds` alone used to echo back the raw client override
            # (often `None`) even when a namespace `default_ttl_seconds`
            # made the chunk expire anyway, so a caller reading
            # `ttl_seconds: null` wrongly concluded "never expires". This is
            # exactly the value `policy.expires_at` used internally to
            # compute `expires_at`, reused here rather than re-derived from
            # a datetime subtraction (which would drift by the same few-ms
            # clock skew noted above for `now` vs `chunk.created_at`).
            effective_ttl_seconds = (
                ttl_seconds if ttl_seconds is not None else policy.default_ttl_seconds
            )
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            _log.error(
                "memory_manager.retention_policy_invalid",
                namespace=namespace_name,
                error=str(exc),
            )
            raise InternalError(
                detail=f"Namespace {namespace_name!r} has a malformed retention_policy.",
                context={"namespace": namespace_name},
            ) from exc

        # Compute the embedding BEFORE writing anything (code review Story
        # 3.1, IG1). `embed()` is a network call to an external provider
        # that can rate-limit, time out, or 5xx; doing it AFTER the chunk
        # insert used to leave a durably committed row with no embedding —
        # invisible to `search_ann`'s INNER JOIN and unrecoverable (nothing
        # in this story re-embeds a chunk after the fact). Ordering it first
        # means that failure mode writes nothing at all.
        #
        # The two DB writes below are still two separate transactions (no
        # cross-repo unit-of-work exists in this codebase — `BaseRepo` never
        # exposes a session across repos by design), so a narrower window
        # remains between them. It is now two fast, same-process Postgres
        # calls instead of one Postgres call plus an external HTTP round
        # trip, so the residual risk is a DB-level failure (lost connection,
        # constraint violation), not provider flakiness — and it is
        # compensated below rather than left as an orphan.
        vectors, model, source = await self._embedding_router.embed(
            [content],
            backend=namespace.embedding_backend,
            purpose="document",
            namespace=namespace_name,
        )
        embedding = _first_vector(vectors, namespace_name=namespace_name, model=model)

        chunk = await self._memory_chunk_repo.create(
            namespace_id=namespace.id,
            content=content,
            ttl_seconds=ttl_seconds,
            expires_at=expires_at,
            tenant_id=tenant_id,
        )
        try:
            await self._chunk_embedding_repo.upsert(
                chunk_id=chunk.id,
                model=model,
                embedding=embedding,
                tenant_id=tenant_id,
                source=source,
            )
        except Exception:
            _log.error(
                "memory_manager.embedding_upsert_failed_compensating",
                namespace=namespace_name,
                chunk_id=str(chunk.id),
            )
            with suppress(Exception):
                await self._memory_chunk_repo.delete_by_id(chunk.id, tenant_id=tenant_id)
            raise

        _log.info(
            "memory_manager.chunk_created",
            namespace=namespace_name,
            chunk_id=str(chunk.id),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

        return MemoryChunkCreateView(
            chunk_id=chunk.id,
            namespace=namespace_name,
            content=chunk.content,
            ttl_seconds=effective_ttl_seconds,
            expires_at=chunk.expires_at,
            embedding_model=model,
            created_at=chunk.created_at,
        )

    async def search(
        self,
        *,
        namespace_name: str,
        query: str,
        top_k: int,
        tenant_id: UUID | None = None,
        acting_department: str | None = None,
        include_archived: bool = False,
        rerank: bool = True,
        require_shared_namespace: bool = False,
    ) -> list[MemorySearchResultView]:
        """ANN search over a namespace's chunks — Story 3.1 AC2, 3.3 AC2, 3.4 AC2.

        ``include_archived`` (Story 3.3 AC2) lifts both the ``archived_at``
        and ``expires_at`` filters for audit recovery — see
        :meth:`ChunkEmbeddingRepo.search_ann`'s docstring for the exact
        scope.

        ``rerank`` (Story 3.4 AC2) applies the namespace's temporal decay to
        the ANN candidates. It is a no-op on a namespace whose
        ``decay_policy`` is empty — which is every namespace created before
        Story 3.4 — so the pre-3.4 ordering and the pre-3.4 ANN cost are
        both preserved by construction.

        ``require_shared_namespace`` (Story 3.5, revue IG1) is for callers
        that have NO department identity to present at all, as opposed to
        a caller that presents none because none applies. See
        :meth:`_check_department_access` for why the two are not the same
        thing.

        Raises
        ------
        NotFoundError
            ``namespace_name`` does not exist.
        ForbiddenError
            ``acting_department`` (Story 3.2 AC2) differs from the
            namespace's department, or ``require_shared_namespace`` is set
            and the namespace carries one.
        """
        start = time.monotonic()
        namespace = await self._namespace_repo.require_by_name(namespace_name, tenant_id=tenant_id)
        await self._check_department_access(
            namespace,
            acting_department=acting_department,
            operation="search",
            tenant_id=tenant_id,
            require_shared_namespace=require_shared_namespace,
        )

        decay_policy, decay_policy_valid = self._decay_policy_for(namespace)
        # `rerank=False` opts out of the WHOLE decay path, not just the
        # oversampling: the effective policy becomes the neutral one, so
        # `score == similarity` and the caller gets the raw cosine ranking
        # it asked for. Passing the namespace's real policy here while
        # skipping the oversampling would be the worst of both — a decayed
        # ranking over a set truncated on similarity alone.
        effective_policy = decay_policy if rerank else DecayPolicy()
        # ⚠️ Oversample BEFORE truncating. `search_ann` applies its
        # `LIMIT` in SQL, so reranking its `top_k` rows could only permute
        # a set already cut by cosine distance alone — a recent chunk
        # ranked just past `top_k` would never surface, and AC2 would be
        # satisfied in the unit tests while being false end to end.
        fetch_k = fetch_k_for(top_k, effective_policy, rerank_enabled=rerank)

        # ⚠️ Symétrie write/read (this story's most dangerous pitfall, see
        # Dev Notes): `namespace.embedding_backend` is read from the SAME
        # freshly-fetched `namespace` row `create_chunk` would use for this
        # namespace — never a cached/stale value — so a query embedding is
        # always produced by the same backend as the chunks it is compared
        # against. `search_ann`'s `model=model` below is what makes a
        # mismatch fail LOUDLY... except it doesn't: it returns an empty
        # list, silently. This is the one place in the whole service where
        # getting `backend` from anywhere other than `namespace` directly
        # would be a correctness bug with no exception to catch it.
        vectors, model, _source = await self._embedding_router.embed(
            [query],
            backend=namespace.embedding_backend,
            purpose="query",
            namespace=namespace_name,
        )
        # ONE clock read for the whole request (T4.2). The same instant feeds
        # `search_ann`'s `expires_at` filter and the decay scoring: two
        # separate `datetime.now()` calls would let a chunk be kept by the
        # filter and scored against a slightly later "now", so the filtering
        # and the ranking would not agree on what time it is.
        #
        # Taken AFTER the embedding round-trip, not before it: a slow provider
        # call (retries included) would otherwise have every chunk aged, and
        # every `expires_at` compared, against an instant that is already
        # stale by the time the query runs, widening the window in which an
        # expired chunk still passes the filter (code review Story 3.4, P6).
        now = datetime.now(UTC)
        rows = await self._chunk_embedding_repo.search_ann(
            _first_vector(vectors, namespace_name=namespace_name, model=model),
            model=model,
            namespace_id=namespace.id,
            top_k=fetch_k,
            tenant_id=tenant_id,
            # Keep the HNSW candidate window at least as wide as what we are
            # asking for. `ef_search` defaults to 100 while oversampling takes
            # `fetch_k` to 200 from `top_k >= 11` on, and asking for more rows
            # than the window holds leans entirely on `hnsw.iterative_scan`,
            # which `search_ann`'s own docstring calls "a mitigation, not a
            # hard guarantee". Before this story `top_k` never exceeded 50, so
            # the regime never arose (code review Story 3.4, P4).
            ef_search=min(max(EF_SEARCH_DEFAULT, fetch_k), EF_SEARCH_MAX),
            now=now,
            include_archived=include_archived,
        )
        # Scoring runs under the same "a read degrades, it never 500s" posture
        # as the parsing above (AC3). `_decay_policy_for` only guards
        # `from_mapping`; an arithmetic fault raised inside `decay_factor`
        # would escape from here instead, on the read path (code review
        # Story 3.4, P5).
        try:
            scored = rerank_rows(rows, policy=effective_policy, now=now, top_k=top_k)
        except (ArithmeticError, TypeError, ValueError) as exc:
            _log.warning(
                "memory_manager.decay_scoring_failed",
                namespace=namespace_name,
                decay_function=effective_policy.function.value,
                error=str(exc),
            )
            scored = rerank_rows(rows, policy=DecayPolicy(), now=now, top_k=top_k)

        # `include_archived` is logged because it lifts BOTH retention filters
        # (`archived_at` and `expires_at`), so a caller can read data whose TTL
        # has elapsed. Real audit persistence is Story 9.1; until then this log
        # line is the only trace such an access ever happened (code review
        # Story 3.3, IG2).
        _log.info(
            "memory_manager.search_executed",
            namespace=namespace_name,
            top_k=top_k,
            fetch_k=fetch_k,
            rerank=rerank,
            # The namespace's CONFIGURED function, not the effective one:
            # paired with `rerank`, this tells an operator both "this
            # namespace decays" and "this particular call opted out".
            #
            # It reads `none` in two very different situations, which is why
            # `decay_policy_valid` sits right next to it: either the namespace
            # genuinely never opted in, or its JSONB is malformed and
            # `_decay_policy_for` already fell back. Without the flag the two
            # are indistinguishable here and in the response alike, and an
            # operator cannot tell a namespace that does not decay from one
            # that fails to (code review Story 3.4, IG1).
            #
            # Deliberately NOT surfaced on `MemorySearchResultView`: validity
            # is a namespace-level fact and `GET /memory/namespaces` already
            # exposes `decay_policy_valid` per namespace, whereas the search
            # response is per result.
            decay_function=decay_policy.function.value,
            decay_policy_valid=decay_policy_valid,
            # `result_count` is post-truncation, so at most `top_k`.
            # `candidate_count` is the window the rerank actually saw: read
            # together they tell an operator whether the ANN pass came back
            # saturated (`candidate_count == fetch_k`, i.e. decay reordered an
            # already-truncated set) or comfortable. It is also the only
            # signal for the HNSW under-fill `search_ann` documents (code
            # review Story 3.4, P9).
            result_count=len(scored),
            candidate_count=len(rows),
            include_archived=include_archived,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

        return [
            MemorySearchResultView(
                chunk_id=item.chunk.id,
                content=item.chunk.content,
                score=item.final_score,
                similarity=item.similarity,
                decay_factor=item.decay_factor,
                namespace=namespace_name,
                created_at=item.chunk.created_at,
                archived_at=item.chunk.archived_at,
                expires_at=item.chunk.expires_at,
            )
            for item in scored
        ]

    async def purge_chunk(self, chunk_id: UUID, *, tenant_id: UUID | None = None) -> None:
        """Manually purge (soft-delete) one chunk — Story 3.6 AC1.

        Reuses :meth:`MemoryChunkRepo.mark_archived_in_session` — the SAME
        primitive :class:`~agentive_backend.features.memory_manager.ttl.MemoryArchivalWorker`
        uses (Story 3.3) — never ``delete_by_id``, which is a hard-delete
        reserved for a failed embedding write's compensating action (see
        this story's Dev Notes § Écarts avec la lettre de l'épic: there is
        no separate ``memory_chunks_archived`` table to move a row into).

        Idempotent by construction (mirrors ``delete_template_tool``, Story
        2.5 decision #10): a second purge of the same chunk is a 404, never
        a 204 twice.

        Not subject to :meth:`_check_department_access` — a global
        administration action, same posture as
        ``create_namespace``/``list_namespaces`` (Dev Notes § Décision
        cloisonnement).

        Raises
        ------
        NotFoundError
            ``chunk_id`` does not exist, or is already archived/purged.
        """
        chunk = await self._memory_chunk_repo.get_by_id(chunk_id, tenant_id=tenant_id)
        if chunk is None or chunk.archived_at is not None:
            raise NotFoundError(
                detail=f"Memory chunk '{chunk_id}' not found",
                context={"chunk_id": str(chunk_id)},
            )
        namespace = await self._namespace_repo.get_by_id(chunk.namespace_id, tenant_id=tenant_id)
        namespace_name = namespace.name if namespace is not None else "unknown"

        now = datetime.now(UTC)
        # Same atomicity pattern as `create_namespace`/`update_namespace_decay_policy`
        # — the UPDATE and the outbox publish share one transaction.
        async with self._memory_chunk_repo.with_tenant(tenant_id) as session:
            updated = await self._memory_chunk_repo.mark_archived_in_session(
                session, [chunk_id], archived_at=now
            )
            if updated == 0:
                # Raced with a concurrent purge/archival between the read
                # above and this UPDATE — `mark_archived_in_session`'s own
                # `AND archived_at IS NULL` guard means "already archived",
                # i.e. exactly the idempotence contract AC1 asks for,
                # surfaced a few milliseconds later than the read above.
                raise NotFoundError(
                    detail=f"Memory chunk '{chunk_id}' not found",
                    context={"chunk_id": str(chunk_id)},
                )
            event = MemoryChunkArchivedEvent(
                chunk_id=chunk_id,
                namespace_id=chunk.namespace_id,
                namespace=namespace_name,
                reason="manual_purge",
                tenant_id=tenant_id,
            )
            event_id = await publish(MemoryChunkArchivedEvent.event_type, event, session=session)
            # commit happens at __aexit__ if no exception is raised.

        await notify_best_effort(event_id, MemoryChunkArchivedEvent.event_type)

        _log.info(
            "memory_manager.chunk_purged_manual",
            chunk_id=str(chunk_id),
            namespace=namespace_name,
        )

    async def list_chunks(
        self,
        *,
        namespace_name: str,
        tenant_id: UUID | None = None,
        include_archived: bool = False,
        content_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryChunkListItemView]:
        """List a namespace's chunks for the admin UI — Story 3.6 AC5.

        ``include_archived=False`` by default: the admin table is meant to
        show what is actually live/searchable, mirroring
        ``count_by_namespace_ids``'s own live-chunk filter rather than
        ``MemoryChunkRepo.list_by_namespace``'s historical "everything"
        default (kept unchanged for its other callers, see that method's
        docstring).

        Raises
        ------
        NotFoundError
            ``namespace_name`` does not exist.
        """
        namespace = await self._namespace_repo.require_by_name(namespace_name, tenant_id=tenant_id)
        chunks = await self._memory_chunk_repo.list_by_namespace(
            namespace.id,
            tenant_id=tenant_id,
            include_archived=include_archived,
            content_contains=content_contains,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
        )
        return [
            MemoryChunkListItemView(
                chunk_id=chunk.id,
                namespace=namespace_name,
                content=chunk.content,
                created_at=chunk.created_at,
                expires_at=chunk.expires_at,
                archived_at=chunk.archived_at,
            )
            for chunk in chunks
        ]

    def _decay_policy_for(self, namespace: Namespace) -> tuple[DecayPolicy, bool]:
        """Parse ``namespaces.decay_policy``, degrading instead of failing.

        Returns ``(policy, valid)``. On a malformed JSONB — hand-seeded row,
        legacy data, a ``function`` this version does not know — it logs a
        warning and falls back to an empty (no-decay) policy with
        ``valid=False``.

        Deliberately NOT the same posture as ``create_chunk``'s guard on
        ``retention_policy``, which raises :class:`InternalError`: a **write**
        must fail loudly rather than silently persist under a policy nobody
        asked for, while a **read** must keep serving results. A corrupt
        decay policy degrades a search's *ranking*; refusing to answer at all
        would be the worse failure (Story 3.4 AC3).
        """
        try:
            return DecayPolicy.from_mapping(namespace.decay_policy), True
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            _log.warning(
                "memory_manager.decay_policy_invalid",
                namespace=namespace.name,
                error=str(exc),
                # The offending value itself. `str(exc)` carries it for a
                # `DomainValidationError`, but not for an `AttributeError` or
                # a `TypeError`, and without it an operator learns that a row
                # is broken without learning what to repair. Truncated, and
                # read through `getattr` because the attribute access is
                # itself one of the things that can fail here (code review
                # Story 3.4, P14).
                raw_policy=repr(getattr(namespace, "decay_policy", None))[:200],
            )
            return DecayPolicy(), False

    def _retention_view_for(self, namespace: Namespace) -> tuple[dict[str, int | None], bool]:
        """Parse ``namespaces.retention_policy`` into its view shape,
        degrading instead of failing — twin of :meth:`_decay_policy_for`.

        ``from_mapping`` catches most malformed shapes, but a float/bool TTL
        slips past its own checks and only fails later, at the view's ``int``
        validation (code review Story 3.2, P1) — so the field-level
        validation runs inside the same guard.
        """
        try:
            retention = RetentionPolicy.from_mapping(namespace.retention_policy)
            return (
                _RETENTION_ADAPTER.validate_python(
                    {
                        "default_ttl_seconds": retention.default_ttl_seconds,
                        "archive_after_seconds": retention.archive_after_seconds,
                    }
                ),
                True,
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            OverflowError,
            PydanticValidationError,
        ) as exc:
            _log.warning(
                "memory_manager.namespace_retention_policy_invalid",
                namespace=namespace.name,
                error=str(exc),
            )
            return {"default_ttl_seconds": None, "archive_after_seconds": None}, False

    async def create_namespace(
        self,
        *,
        name: str,
        ns_type: NamespaceType,
        department: str | None = None,
        project: str | None = None,
        retention_policy_override: RetentionPolicy | None = None,
        decay_policy_override: DecayPolicy | None = None,
        embedding_backend: EmbeddingBackend = EmbeddingBackend.CLOUD,
        tenant_id: UUID | None = None,
    ) -> NamespaceCreateView:
        """Create a namespace with a per-type default retention — Story 3.2 AC1.

        ``embedding_backend`` (Story 3.6 AC2/AC3) is set ONCE, at creation,
        and immutable thereafter (no ``PATCH`` support — see
        ``UpdateNamespaceRequest``'s docstring): changing it on a namespace
        that already has chunks would make them invisible to
        :meth:`search`, which always derives its query embedding's backend
        from the SAME namespace field (§ Symétrie write/read, Dev Notes).

        Not subject to ``_check_department_access`` — this is a global
        administration action (John, owner), not a memory read/write.

        Raises
        ------
        ConflictError
            ``name`` already exists.
        ValidationError
            ``ns_type`` is not one of the 4 known namespace types. The HTTP
            router already restricts ``type`` to that same set via
            Pydantic, so this only fires for a non-HTTP caller passing a
            raw string — kept typed rather than an opaque 500 (code review
            Story 3.2, P7).
        """
        if retention_policy_override is not None:
            policy = retention_policy_override
        else:
            try:
                domain_ns_type = DomainNamespaceType(ns_type)
            except ValueError as exc:
                raise ValidationError(detail=f"unknown namespace type {ns_type!r}") from exc
            policy = RetentionPolicy.default_for_type(domain_ns_type)

        # Story 3.4 T5.1 — no `default_for_type` equivalent here, on purpose:
        # an unset `decay_policy` means "no decay", so a namespace created
        # today ranks exactly as it did before 3.4. Enabling decay is an
        # explicit per-namespace opt-in (see the story's Dev Notes).
        decay_policy = decay_policy_override or DecayPolicy()

        # Same atomicity pattern as `AgentRegistryService.create_template` —
        # the row INSERT and the outbox event publish share one transaction
        # so a crash between the two never leaves an unaudited namespace.
        async with self._namespace_repo.with_tenant(tenant_id) as session:
            namespace = await self._namespace_repo.create_in_session(
                session,
                name=name,
                ns_type=ns_type,
                department=department,
                project=project,
                retention_policy=policy.to_mapping(),
                decay_policy=decay_policy.to_mapping(),
                embedding_backend=embedding_backend.value,
                tenant_id=tenant_id,
            )
            event = NamespaceCreatedEvent(
                namespace_id=namespace.id,
                name=namespace.name,
                namespace_type=namespace.type,
                department=namespace.department,
                project=namespace.project,
                tenant_id=tenant_id,
            )
            event_id = await publish(NamespaceCreatedEvent.event_type, event, session=session)
            # commit happens at __aexit__ if no exception is raised.

        await notify_best_effort(event_id, NamespaceCreatedEvent.event_type)

        _log.info(
            "memory_manager.namespace_created",
            namespace=namespace.name,
            namespace_id=str(namespace.id),
        )

        return NamespaceCreateView(
            namespace_id=namespace.id,
            name=namespace.name,
            type=namespace.type,
            department=namespace.department,
            project=namespace.project,
            retention_policy={
                "default_ttl_seconds": policy.default_ttl_seconds,
                "archive_after_seconds": policy.archive_after_seconds,
            },
            decay_policy=decay_policy.to_mapping(),
            embedding_backend=namespace.embedding_backend,
            created_at=namespace.created_at,
        )

    async def update_namespace_decay_policy(
        self,
        *,
        name: str,
        decay_policy_override: DecayPolicy | None,
        tenant_id: UUID | None = None,
    ) -> NamespaceUpdateView:
        """Replace a namespace's decay policy — Story 3.4, code review BS1.

        The reason this exists: ``decay_policy`` was settable at creation
        only, so every namespace Stories 3.1-3.3 had already created — all of
        them — could never opt into decay at all. ``AC1``'s "configurable per
        namespace" was true of new rows and false of the existing table.

        ``decay_policy_override=None`` clears the policy back to "no decay",
        restoring ``final_score == similarity`` on that namespace.

        Not subject to ``_check_department_access``: like creation, this is a
        global administration action, not a memory read/write.

        Raises
        ------
        NotFoundError
            No namespace carries that ``name``.
        """
        decay_policy = decay_policy_override or DecayPolicy()

        # Same atomicity pattern as `create_namespace`: the UPDATE and the
        # outbox publish share one transaction, so a namespace never starts
        # reordering its search results with nothing in the outbox saying
        # when that began.
        async with self._namespace_repo.with_tenant(tenant_id) as session:
            namespace = await self._namespace_repo.set_decay_policy_in_session(
                session,
                name=name,
                decay_policy=decay_policy.to_mapping(),
            )
            if namespace is None:
                raise NotFoundError(
                    detail=f"Namespace '{name}' not found",
                    context={"namespace": name},
                )
            retention_view, _retention_valid = self._retention_view_for(namespace)
            event = NamespaceDecayPolicyUpdatedEvent(
                namespace_id=namespace.id,
                name=namespace.name,
                decay_policy=decay_policy.to_mapping(),
                tenant_id=tenant_id,
            )
            event_id = await publish(
                NamespaceDecayPolicyUpdatedEvent.event_type, event, session=session
            )
            # commit happens at __aexit__ if no exception is raised.

        await notify_best_effort(event_id, NamespaceDecayPolicyUpdatedEvent.event_type)

        _log.info(
            "memory_manager.namespace_decay_policy_updated",
            namespace=namespace.name,
            namespace_id=str(namespace.id),
            decay_function=decay_policy.function.value,
        )

        return NamespaceUpdateView(
            namespace_id=namespace.id,
            name=namespace.name,
            type=namespace.type,
            department=namespace.department,
            project=namespace.project,
            retention_policy=retention_view,
            decay_policy=decay_policy.to_mapping(),
            embedding_backend=namespace.embedding_backend,
            created_at=namespace.created_at,
        )

    async def list_namespaces(
        self, *, tenant_id: UUID | None = None
    ) -> list[NamespaceListItemView]:
        """All namespaces + live chunk count — Story 3.2 AC3 (admin listing).

        No department filter — John (owner) needs to see everything to
        administer the memory system. Grouping by department/type is a
        frontend presentation detail, not part of this contract.
        """
        namespaces = await self._namespace_repo.list_all(tenant_id=tenant_id)
        if len(namespaces) >= NAMESPACE_LISTING_SAFETY_CAP:
            # AC3 promises "tous les namespaces" — the repo's cap is a
            # safety net, not a real ceiling (see its docstring), so
            # hitting it exactly is unexpected enough to warrant a signal
            # rather than a silently truncated response (code review Story
            # 3.2, BS3).
            _log.warning(
                "memory_manager.namespace_listing_hit_safety_cap",
                cap=NAMESPACE_LISTING_SAFETY_CAP,
            )
        counts = await self._memory_chunk_repo.count_by_namespace_ids(
            [ns.id for ns in namespaces], tenant_id=tenant_id
        )

        views: list[NamespaceListItemView] = []
        for ns in namespaces:
            # A hand-seeded/legacy row can carry a malformed JSONB (same P4
            # class of issue as `create_chunk`'s guard) — one bad namespace
            # must not 500 the entire admin listing.
            #
            # The two policies are parsed under SEPARATE guards (Story 3.4
            # T5.2): they are independent columns, so a corrupt
            # `retention_policy` must not brand a healthy `decay_policy`
            # invalid, nor the reverse. Each falls back to its own empty
            # policy and raises only its own `*_valid` flag.
            retention_view, retention_policy_valid = self._retention_view_for(ns)
            decay, decay_policy_valid = self._decay_policy_for(ns)

            views.append(
                NamespaceListItemView(
                    namespace_id=ns.id,
                    name=ns.name,
                    type=ns.type,
                    department=ns.department,
                    project=ns.project,
                    retention_policy=retention_view,
                    retention_policy_valid=retention_policy_valid,
                    decay_policy=decay.to_mapping(),
                    decay_policy_valid=decay_policy_valid,
                    embedding_backend=ns.embedding_backend,
                    chunk_count=counts.get(ns.id, 0),
                    created_at=ns.created_at,
                )
            )
        return views

    async def _check_department_access(
        self,
        namespace: Namespace,
        *,
        acting_department: str | None,
        operation: Literal["create_chunk", "search"],
        tenant_id: UUID | None,
        require_shared_namespace: bool = False,
    ) -> None:
        """Story 3.2 AC2 — deny cross-department read/write, audit the denial.

        Branch point left by Story 3.1's Dev Agent Record (AC3 note). Not a
        Postgres RLS policy (``department`` only exists on ``namespaces``,
        not on ``memory_chunks``/``chunk_embeddings``) — an application-layer
        authorization check, at the exact point 3.1 flagged.

        No restriction applies when ``acting_department`` is ``None`` (no
        ``X-Acting-Department`` header — rest of the callers, including every
        existing 3.1 call site, are unaffected) or when the namespace itself
        has no department (cross-cutting/shared namespace).

        The comparison itself is case- and whitespace-insensitive (product
        decision, code review Story 3.2, IG1): this header is explicitly
        not a real security boundary before Growth RBAC (Sprint 4), so a
        typo'd case or a stray trailing space must not turn into a spurious
        403 plus a persisted audit event for what is really the same
        department.

        ``require_shared_namespace`` (Story 3.5, revue IG1) closes the door
        that the ``acting_department is None`` early return leaves open for
        a caller that has no department identity to present AT ALL, as
        opposed to an HTTP caller that simply sent no header. The header
        callers are people, bounded by a request they made themselves ; a
        header-less internal reader (Push Memory) injects a namespace's
        content into a prompt that is then echoed back to whoever triggered
        the run, so "no identity" must mean "shared namespaces only", not
        "everything". Otherwise pointing a template at another department's
        namespace would be a one-line bypass of AC2. Fail closed until
        Growth RBAC (Sprint 4) gives internal readers a real identity.

        No ``NamespaceAccessDeniedEvent`` on that branch, deliberately :
        the header path fires once per HTTP request, whereas this one
        would fire on EVERY run of a misconfigured template, i.e. an
        unbounded stream of outbox rows for a single config mistake. A
        warning log is the proportionate signal.
        """
        if require_shared_namespace and namespace.department is not None:
            _log.warning(
                "memory_manager.department_scoped_namespace_refused",
                namespace=namespace.name,
                namespace_department=namespace.department,
                operation=operation,
            )
            raise ForbiddenError(
                detail=(
                    f"Namespace {namespace.name!r} is department-scoped and cannot be "
                    "read by a caller with no department identity."
                ),
                context={"namespace": namespace.name, "department": namespace.department},
            )
        if acting_department is None:
            return
        if namespace.department is None:
            return
        if _normalize_department(namespace.department) == _normalize_department(acting_department):
            return

        event = NamespaceAccessDeniedEvent(
            namespace_id=namespace.id,
            namespace=namespace.name,
            namespace_department=namespace.department,
            acting_department=acting_department,
            operation=operation,
            tenant_id=tenant_id,
        )
        # ⚠️ The event MUST be committed before `ForbiddenError` is raised.
        # Raising inside the `with_tenant` block would make `BaseRepo`'s
        # `except BaseException` roll the transaction back — the exact
        # audit event meant to record this denial would vanish with it.
        async with self._namespace_repo.with_tenant(tenant_id) as session:
            event_id = await publish(NamespaceAccessDeniedEvent.event_type, event, session=session)
        await notify_best_effort(event_id, NamespaceAccessDeniedEvent.event_type)

        _log.warning(
            "memory_manager.namespace_access_denied",
            namespace=namespace.name,
            namespace_department=namespace.department,
            acting_department=acting_department,
            operation=operation,
        )

        raise ForbiddenError(
            detail=f"Namespace {namespace.name!r} belongs to a different department.",
            context={"namespace": namespace.name, "department": namespace.department},
        )


__all__ = ["MemoryManagerService"]
