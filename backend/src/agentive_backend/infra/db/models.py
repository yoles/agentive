"""SQLAlchemy ORM models — core tables for Agentive.

Tables created in Alembic migration initiale (Story 1.1 T6) :
    - users, sessions, feature_flags, namespaces
    - memory_chunks, chunk_embeddings (with tenant_id NULL)
    - workflows, workflow_runs, agent_templates, agent_instances, prompts
    - outbox_events
    - audit_events (partitioned by month, immutable)

**Sprint 0 scaffolding** — these models define the schema shape. They will be
enriched in their respective epic stories with full columns, constraints,
indexes, and relationships. For Sprint 0 we only ensure the DDL matches the
Alembic migration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentive_backend.infra.db.types import EncryptedJSONB


class Base(DeclarativeBase):
    """Base for all ORM models."""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Core tables (no tenant_id — global / system)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, server_default="owner")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    rollout_percentage: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Namespace(Base):
    __tablename__ = "namespaces"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Type enum — enforced by ck_namespace_type below. The CHECK exists in
    # the DB since the initial migration (20260419000000); the model was
    # missing the declaration (model/DB desync fixed by audit M-05).
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    project: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retention_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    # Story 3.4 AC1 — per-namespace temporal decay, read through the
    # `DecayPolicy` domain VO. Kept separate from `retention_policy`: that
    # one's `to_mapping()` rebuilds its dict from its own two fields, so
    # decay keys stored there would be dropped on the next write.
    decay_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    embedding_backend: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="cloud"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "type IN ('client', 'metier', 'operationnelle', 'contextuelle')",
            name="ck_namespace_type",
        ),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tenant-ready tables (tenant_id NULL + RLS activated via migration)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MemoryChunk(Base):
    __tablename__ = "memory_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    namespace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("namespaces.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    ttl_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (Index("ix_memory_chunks_namespace", "namespace_id"),)


class ChunkEmbedding(Base):
    """Embeddings stored in a separate table for per-model partial indexes.

    Primary key : (chunk_id, model) — allows multiple embeddings per chunk
    (e.g. one local + one cloud, or when switching embedding model).
    """

    __tablename__ = "chunk_embeddings"

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("memory_chunks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model: Mapped[str] = mapped_column(String(100), primary_key=True)
    # Vector dimension varies by model (bge-small 384, openai-3-small 1536, voyage-3-lite 512)
    # Stored as generic Vector; partial HNSW indexes per model created in migration
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Which `Embedder.provider_name` produced this vector ("openai", "mock",
    # ...). Nullable/unenforced provenance trail, not a search filter — code
    # review Story 3.1, IG3: a mock-derived vector is otherwise stored under
    # the exact same `model` string as a real one and indistinguishable
    # after the fact.
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    dag: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Story 4.8 AC1 — SHA-256 hex of the creation request's `{name, dag}`,
    # the key that makes a replayed `POST /api/v1/workflows` idempotent.
    #
    # Nullable for two distinct populations, and only the first is benign:
    # rows predating this story (never backfilled — see the migration), and
    # rows written through `WorkflowRepo.create`/`create_in_session` without
    # the kwarg, whose default is still `None`. The latter are permanently
    # exempt from the partial index, so pass the fingerprint on any path that
    # is meant to be idempotent.
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Story 4.8 AC1 — the uniqueness rule behind idempotent replay.
        #
        # Declared HERE and not only in migration `20260912_000002`, contrary
        # to what an earlier revision of this comment claimed: SQLAlchemy
        # 2.0.49 (the pinned version) does express both halves, via
        # `postgresql_nulls_not_distinct` and `postgresql_where`, and
        # compiles to the migration's raw SQL character for character. The
        # migration still issues it by hand — that is fine, the two agree —
        # but the DECLARATION has to exist in `Base.metadata`, because that
        # metadata is what `alembic/env.py` points autogenerate at, and it
        # should describe the schema the application expects.
        #
        # Story 4.15 AC4 — what it does NOT buy, contrary to what this comment
        # used to claim: protection against `--autogenerate` emitting a
        # `DROP INDEX`. This is a `postgresql_where` PARTIAL index, and
        # Alembic does not reliably compare partial predicates (nor expression
        # indexes) against the reflected database — see the two sibling
        # comments on `WorkflowRun.__table_args__` and
        # `docs/runbooks/concurrent-index-migrations.md` point 2. The real
        # protection is a mandatory human read of any autogenerated migration
        # touching this index; without it, idempotence could indeed be
        # switched off silently.
        #
        # PARTIAL (`WHERE request_fingerprint IS NOT NULL`) because under
        # `NULLS NOT DISTINCT` two NULLs are EQUAL, so a total index would
        # reject the second fingerprint-less row on any base carrying more
        # than one of them. `NULLS NOT DISTINCT` itself is what keeps the
        # index operative in the single-tenant MVP, where `tenant_id IS NULL`
        # on every row — the bug `20260508000000` had to fix after the fact
        # on `uq_agent_template`.
        Index(
            "uq_workflow_request_fingerprint",
            "request_fingerprint",
            "tenant_id",
            unique=True,
            postgresql_nulls_not_distinct=True,
            postgresql_where=text("request_fingerprint IS NOT NULL"),
        ),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, server_default="running")
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    last_checkpoint_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Story 4.5 AC1 — the pre-workflow Mise en Place report (4 `CheckResult`
    # entries + bypass metadata) for runs that STARTED. A refused launch
    # (AC2: a failing check without `force`) creates no `workflow_runs` row
    # at all, so it leaves no report here. AC1's "persisted whether the
    # workflow starts or not" cannot hold on a column of this table and AC2
    # is the stronger requirement; a refused launch is traced in the outbox
    # instead, as `workflow_engine.workflow_run.mise_en_place_refused`
    # carrying the same report (review BS2). `None` only for runs predating
    # this story.
    mise_en_place: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Story 5.1 AC2 — l'accusé de réception rendu au client sur la PREMIÈRE
    # frame SSE : `{message, agents[], eta_minutes, eta_source}`. Écrit une
    # seule fois, dans l'INSERT du run, jamais réécrit.
    #
    # Colonne et non clé de `checkpoint` : `_sync_checkpoint` remplace ce
    # dernier EN ENTIER dès que le premier node atterrit, donc l'accusé
    # disparaîtrait de la frame de rattrapage exactement au moment où elle
    # sert. `None` pour les runs antérieurs à cette story.
    acknowledgement: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Story 4.6 AC1/AC2 — a PENDING pause/cancel REQUEST (`"pause"`,
    # `"cancel"`, or `NULL`), deliberately NOT a status.
    #
    # Interrupting a run is cooperative: the driver observes this column at
    # the end of a superstep, the one instant where LangGraph has committed
    # its checkpoint and stopping loses no work and cuts no already-billed
    # LLM call. Until it does, the run is still genuinely `running` and must
    # keep saying so — four separate pieces of code compare `status` against
    # the literal `"running"` (`claim_stale_running`,
    # `update_status(only_if_status=...)`, `_resume_run`'s guard,
    # `_abandon_one`), and a `pausing`/`cancelling` status would have
    # required teaching every one of them a new value.
    #
    # Two consequences fall out for free: the signal is multi-worker (it
    # travels through the DB, not through an in-process task set), and an
    # unobserved request self-heals — a run whose process died still reads
    # `running`, so the recovery sweep claims it and the driver settles the
    # signal before executing any node.
    #
    # `paused`/`cancelled` ARE statuses and live in `status` above, which is
    # a free `String(50)`. Legal values and transitions: `domain/run_control.py`.
    control_signal: Mapped[str | None] = mapped_column(String(20), nullable=True)
    control_requested_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # Story 4.10 AC1 — stamped by `CheckpointRetentionWorker` once this run's
    # LangGraph checkpoint history (`checkpoints`/`checkpoint_writes`/
    # `checkpoint_blobs`, migration `20260910_000001`) has been purged via
    # `AsyncPostgresSaver.adelete_thread`. `NULL` means either "not old
    # enough yet" or "predates this story" — both read identically (nothing
    # purged), which is correct: a purge worker that ran once is idempotent
    # against either. Never set back to `NULL`: a purge is not reversible,
    # so nothing in this codebase un-stamps it.
    checkpoint_purged_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        # Story 4.10 AC3 — `WorkflowRunRepo.claim_stale_running` filters
        # `status = 'running'` and orders by
        # `COALESCE(last_checkpoint_at, started_at)`, and until this story
        # the only index on this table was `ix_workflow_runs_workflow`
        # (`workflow_id`) — every sweep tick (every 30s, per API replica) ran
        # a full seq scan + sort once the table stopped being tiny.
        #
        # PARTIAL (`WHERE status = 'running'`) rather than total: the sweep
        # only ever reads `running` rows, which shrink toward a small,
        # roughly-constant fraction of the table as it grows (most rows are
        # terminal) — a total index would pay to maintain entries the sweep
        # never reads. EXPRESSION on the same `COALESCE` the query itself
        # orders by, so the index's sort order matches the query's `ORDER
        # BY` exactly rather than merely narrowing the row set.
        #
        # Declared here (not only in the migration) for the same reason as
        # `uq_workflow_request_fingerprint` (Story 4.8 P4): `Base.metadata`
        # should describe the schema the application expects, and an index
        # missing from it is a gap between the two.
        #
        # Story 4.15 AC4 — what this declaration does NOT buy, contrary to
        # what this comment used to claim: protection against `alembic
        # revision --autogenerate`. Alembic does not reliably compare
        # EXPRESSION indexes (the `coalesce(...)` below) nor `postgresql_where`
        # predicates against the reflected database, so declaring it here does
        # not reliably suppress a spurious diff — the usual outcome is a
        # parasitic `drop_index`/`create_index` pair emitted WITHOUT
        # `CONCURRENTLY`, i.e. an `ACCESS EXCLUSIVE` on `workflow_runs` for the
        # whole rebuild. The real protection is a mandatory human read of any
        # autogenerated migration touching these two indexes; see
        # `docs/runbooks/concurrent-index-migrations.md`.
        Index(
            "ix_workflow_runs_stale_running",
            func.coalesce(last_checkpoint_at, started_at),
            postgresql_where=text("status = 'running'"),
        ),
        # Serves the windowed metrics aggregates (`aggregate_routing_modes` /
        # `aggregate_token_reduction`), whose predicate is
        # `workflow_id = ? AND started_at >= ?`. Neither existing index can:
        # `ix_workflow_runs_workflow` covers only the first column, and the
        # partial index above is scoped to `status = 'running'` while these
        # aggregates read TERMINAL rows — so the window bounded what was
        # aggregated, never what was scanned. `DESC` because every consumer
        # reads a trailing window. Declared here as well as in the migration,
        # same reason as its sibling above — including the caveat: a plain
        # multi-column index is compared more reliably than the expression one,
        # but `--autogenerate` output touching either still requires a human
        # read before it is applied.
        Index("ix_workflow_runs_workflow_started", "workflow_id", started_at.desc()),
    )


class AgentTemplate(Base):
    __tablename__ = "agent_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    archetype: Mapped[str] = mapped_column(String(50), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("name", "version", "tenant_id", name="uq_agent_template"),
        # Audit M-05 (4.4b) — the 8 universal archetype IDs (source:
        # features/agent_registry/templates/archetype-schema.yaml).
        # Before this CHECK, `archetype = 'banana'` passed the DB — the
        # invariant only lived in the registry/service. Adding a 9th
        # archetype = YAML entry + migration extending this constraint
        # (deliberate friction: archetypes are a stable product concept).
        CheckConstraint(
            "archetype IN ('orchestrateur', 'chercheur', 'analyste', 'producteur', "
            "'stratege', 'controleur', 'veilleur', 'communicateur')",
            name="ck_agent_template_archetype",
        ),
    )


class AgentInstance(Base):
    __tablename__ = "agent_instances"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_templates.id", ondelete="RESTRICT"), nullable=False
    )
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    # Frozen snapshot of template config at instantiation time
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Prompt(Base):
    """Versioned prompts — each edit creates a new row (audit trail).

    .. note:: Sprint 1 schema is intentionally minimal (Story 1.5). Architecture
       H2 describes the target schema with `parent_version`, `is_active`
       (UNIQUE constraint) and `metadata` JSONB ; that evolution is deferred
       to Story 2.4 / 2.7 when runtime instances + rollback semantics are
       implemented (cf P-16 follow-up Story 2.2 code-review 2026-05-09).
    """

    __tablename__ = "prompts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    agent_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_templates.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("agent_template_id", "version", name="uq_prompt_template_version"),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Outbox & Audit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class OutboxEvent(Base):
    """Outbox Pattern — durability layer for LISTEN/NOTIFY events (Story 1.4)."""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class AuditEvent(Base):
    """Audit log — partitioned by month, immutable (REVOKE DELETE/UPDATE).

    The actual partitioning is declared via ``__table_args__`` below AND in
    the Alembic migration. The migration remains the source of truth for
    partition ranges + grants — the ORM declaration is here so a caller
    accidentally running ``Base.metadata.create_all()`` produces a
    compatible (partitioned) table instead of a plain one that would
    later conflict with the migration.

    Write access reserved to ``agentive_audit_admin`` (INSERT-only).
    """

    __tablename__ = "audit_events"

    # Tell SQLAlchemy this is a RANGE-partitioned table on ``created_at``.
    # Requires SQLAlchemy 2.0+ PostgreSQL dialect.
    __table_args__ = {  # noqa: RUF012 — SQLAlchemy-specific metadata, not shared state
        "postgresql_partition_by": "RANGE (created_at)",
    }

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), primary_key=True, nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.5 — Tool Hub MCP (registry + assignment, runtime exec defer 2.6)
# Migration : 20260510_000000_tool_hub_tables.py
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ToolServer(Base):
    """A registered MCP server (stdio or sse). 1 row per `POST /tools/servers`.

    The ``connection_config`` JSONB shape depends on ``transport`` :
    - stdio : ``{"command": str, "args": list[str], "env"?: dict}``
    - sse   : ``{"url": str, "headers"?: dict[str, str]}``

    ``connection_config`` is encrypted at rest via :class:`EncryptedJSONB`
    (Fernet, Story 9.2 / audit M-09) — the DB holds ciphertext, callers see a
    plaintext ``dict`` transparently. Legacy plaintext rows remain readable.
    """

    __tablename__ = "tool_servers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(Text, nullable=False)  # 'stdio' | 'sse'
    connection_config: Mapped[dict[str, Any]] = mapped_column(EncryptedJSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    discovered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        # UNIQUE constraint with NULLS NOT DISTINCT is created via raw SQL in
        # the Alembic migration (SQLAlchemy DSL doesn't expose the keyword).
        # Declaring it here as a regular UniqueConstraint would diverge from
        # the actual DB shape ; we list it as a docstring instead.
        CheckConstraint(
            "transport IN ('stdio', 'sse')",
            name="ck_tool_server_transport",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_tool_server_status",
        ),
    )


class Tool(Base):
    """A discovered tool exposed by a ToolServer. 1 row per tool from MCP
    `list_tools()`. UNIQUE (server_id, name) so a server cannot expose 2
    tools with the same name (re-discovery would 409).

    ``input_schema`` is the JSON Schema (typically draft-07) returned by the
    MCP server's tool definition. Sprint 1 stores it as-is (no Pydantic
    validation — the MCP server is the authority on its tools' schemas).
    """

    __tablename__ = "tools"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tool_servers.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (UniqueConstraint("server_id", "name", name="uq_tool_per_server"),)


class AgentTemplateTool(Base):
    """Junction (PK composite) between agent_templates and tools.

    Both FKs are ``ON DELETE CASCADE`` — removing a template OR a tool
    cleans the assignment automatically. ``assigned_by_actor`` defaults to
    ``"system"`` Sprint 1 (D1 defer Story 9.1 — auth context resolution).
    """

    __tablename__ = "agent_template_tools"

    agent_template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_templates.id", ondelete="CASCADE"),
        nullable=False,
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tools.id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), nullable=False
    )
    assigned_by_actor: Mapped[str] = mapped_column(Text, nullable=False, server_default="system")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint(
            "agent_template_id",
            "tool_id",
            name="pk_agent_template_tools",
        ),
    )
