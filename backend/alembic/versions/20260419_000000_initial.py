"""initial schema

Revision ID: 20260419000000
Revises:
Create Date: 2026-04-19

Première migration — Sprint 0 Story 1.1 T6.

Crée :
    - extension vector (pgvector)
    - tables core : users (1 row owner), sessions, feature_flags, namespaces
    - tables tenant-ready : memory_chunks, chunk_embeddings, workflows, workflow_runs,
      agent_templates, agent_instances, prompts (toutes avec tenant_id NULL)
    - outbox_events (Outbox Pattern pour event bus)
    - audit_events partitionnée par mois + REVOKE DELETE/UPDATE (immutabilité)
    - 2 index HNSW partiels sur chunk_embeddings (par modèle embedding)
    - RLS sur memory_chunks, workflows, audit_events, agent_instances, chunk_embeddings
    - Grants par rôle (agentive_app, agentive_audit_admin) appliqués après RLS
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers
revision: str = "20260419000000"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 1. Extension pgvector
    # Note : CREATE EXTENSION vector nécessite superuser → exécuté dans
    # infra/postgres/init.sql (par le rôle postgres à l'initialisation du container).
    # On vérifie juste que l'extension existe bien avant de continuer.
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    conn = op.get_bind()
    result = conn.execute(sa.text("SELECT COUNT(*) FROM pg_extension WHERE extname = 'vector'"))
    if result.scalar_one() == 0:
        raise RuntimeError(
            "Extension 'vector' not found. Ensure infra/postgres/init.sql "
            "is applied (creates the extension as superuser)."
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 2. Core tables — users, sessions, feature_flags, namespaces
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.create_table(
        "users",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("role", sa.String(50), nullable=False, server_default="owner"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
    )

    op.create_table(
        "sessions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "feature_flags",
        sa.Column("name", sa.String(100), primary_key=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("rollout_percentage", sa.Integer, nullable=False, server_default="0"),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_table(
        "namespaces",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "type", sa.String(50), nullable=False
        ),  # client/metier/operationnelle/contextuelle
        sa.Column("department", sa.String(100), nullable=True),
        sa.Column("project", sa.String(100), nullable=True),
        sa.Column("retention_policy", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("embedding_backend", sa.String(50), nullable=False, server_default="cloud"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "type IN ('client', 'metier', 'operationnelle', 'contextuelle')",
            name="ck_namespace_type",
        ),
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 3. Tenant-ready tables
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.create_table(
        "memory_chunks",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "namespace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("namespaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("ttl_seconds", sa.Integer, nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_memory_chunks_namespace", "memory_chunks", ["namespace_id"])
    op.create_index(
        "ix_memory_chunks_expires_at",
        "memory_chunks",
        ["expires_at"],
        postgresql_where=sa.text("archived_at IS NULL AND expires_at IS NOT NULL"),
    )

    op.create_table(
        "chunk_embeddings",
        sa.Column(
            "chunk_id",
            UUID(as_uuid=True),
            sa.ForeignKey("memory_chunks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.String(100), primary_key=True),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
    )

    # Index HNSW partiels par modèle embedding (dimension variable)
    # Paramètres initiaux m=16, ef_construction=64 — ajustés Story 1.3 après benchmark
    op.execute(
        """
        CREATE INDEX chunk_embeddings_bge_hnsw
        ON chunk_embeddings
        USING hnsw ((embedding::vector(384)) vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE model = 'bge-small-en-v1.5'
        """
    )
    op.execute(
        """
        CREATE INDEX chunk_embeddings_openai_hnsw
        ON chunk_embeddings
        USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE model = 'text-embedding-3-small'
        """
    )

    op.create_table(
        "workflows",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("dag", JSONB, nullable=False),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
    )

    op.create_table(
        "workflow_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workflow_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(50), nullable=False, server_default="running"),
        sa.Column("correlation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("checkpoint", JSONB, nullable=True),
        sa.Column("metrics", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_workflow_runs_workflow", "workflow_runs", ["workflow_id"])

    op.create_table(
        "agent_templates",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("archetype", sa.String(50), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("config", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("name", "version", "tenant_id", name="uq_agent_template"),
    )

    op.create_table(
        "agent_instances",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "template_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_templates.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("template_version", sa.Integer, nullable=False),
        sa.Column(
            "workflow_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("snapshot", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
    )

    op.create_table(
        "prompts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agent_template_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("agent_template_id", "version", name="uq_prompt_template_version"),
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 4. Outbox events (Outbox Pattern — Story 1.4)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.create_table(
        "outbox_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("correlation_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(255), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
    )
    # Index partiel sur les events non-processed (replay worker)
    op.execute(
        "CREATE INDEX ix_outbox_unprocessed ON outbox_events (created_at) "
        "WHERE processed_at IS NULL"
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 5. Audit events — partitioned by month, immutable
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.execute(
        """
        CREATE TABLE audit_events (
            id UUID NOT NULL DEFAULT gen_random_uuid(),
            actor VARCHAR(255) NOT NULL,
            action VARCHAR(255) NOT NULL,
            target VARCHAR(255),
            correlation_id UUID NOT NULL,
            payload_hash VARCHAR(128),
            metadata JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            tenant_id UUID,
            PRIMARY KEY (id, created_at)
        ) PARTITION BY RANGE (created_at)
        """
    )
    # Partitions : 12 mois glissants pré-créés (2026-04 → 2027-03)
    # pour éviter la fenêtre de crash si le worker d'auto-partitionnement
    # (Story 9.1 / 7.3) n'est pas encore déployé.
    _partitions = [
        ("2026_04", "2026-04-01", "2026-05-01"),
        ("2026_05", "2026-05-01", "2026-06-01"),
        ("2026_06", "2026-06-01", "2026-07-01"),
        ("2026_07", "2026-07-01", "2026-08-01"),
        ("2026_08", "2026-08-01", "2026-09-01"),
        ("2026_09", "2026-09-01", "2026-10-01"),
        ("2026_10", "2026-10-01", "2026-11-01"),
        ("2026_11", "2026-11-01", "2026-12-01"),
        ("2026_12", "2026-12-01", "2027-01-01"),
        ("2027_01", "2027-01-01", "2027-02-01"),
        ("2027_02", "2027-02-01", "2027-03-01"),
        ("2027_03", "2027-03-01", "2027-04-01"),
    ]
    for suffix, start, end in _partitions:
        op.execute(
            f"""
            CREATE TABLE audit_events_{suffix} PARTITION OF audit_events
            FOR VALUES FROM ('{start}') TO ('{end}')
            """
        )
    # DEFAULT partition : safety net — absorbe toute ligne hors des partitions
    # explicites (future-proof si le worker d'auto-partitionnement tarde).
    # À surveiller : si elle grossit, c'est que le worker cron n'a pas pris le relais.
    op.execute(
        """
        CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT
        """
    )
    # Note : partition creation automatique mensuelle à implémenter via pg_cron
    # ou cron sidecar (Story 9.1 T1 ou scheduler M11 Story 7.3) — la DEFAULT
    # partition est un filet de sécurité, pas un substitut au worker.

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 6. RLS (Row Level Security) — activée sur toutes tables tenant-scoped
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Tables avec colonne tenant_id UUID NULL → RLS obligatoire.
    # `FORCE` applique la RLS même pour le rôle propriétaire (`agentive_owner`),
    # évitant le bypass silencieux lors d'opérations admin/migrations.
    # `WITH CHECK` garantit que les INSERT/UPDATE ne peuvent pas créer/modifier
    # des lignes en dehors du tenant courant (pas seulement les lectures).
    rls_tables = [
        "memory_chunks",
        "chunk_embeddings",
        "namespaces",
        "workflows",
        "workflow_runs",
        "agent_templates",
        "agent_instances",
        "prompts",
        "outbox_events",
        "audit_events",
    ]
    for table in rls_tables:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                tenant_id IS NULL
                OR tenant_id = current_setting('app.tenant_id', true)::uuid
            )
            WITH CHECK (
                tenant_id IS NULL
                OR tenant_id = current_setting('app.tenant_id', true)::uuid
            )
            """
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 7. Grants par rôle (principe du moindre privilège)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # agentive_app : SELECT/INSERT/UPDATE/DELETE sur tables métier
    # Les default privileges de init.sql couvrent déjà les nouvelles tables
    # mais on rappelle explicitement ici pour être exhaustif
    app_writable_tables = [
        "users",
        "sessions",
        "feature_flags",
        "namespaces",
        "memory_chunks",
        "chunk_embeddings",
        "workflows",
        "workflow_runs",
        "agent_templates",
        "agent_instances",
        "prompts",
        "outbox_events",
    ]
    for table in app_writable_tables:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO agentive_app")

    # agentive_app : aucun droit sur audit_events (lecture seule réservée à
    # agentive_audit_admin pour l'intégrité auditable).
    # Les lignes audit_events sont écrites via un canal dédié :
    # - soit via agentive_audit_admin (le middleware/interceptor d'audit),
    # - soit via un trigger BEFORE INSERT sur les tables métier (à implémenter
    #   Story 9.1 pour tracer les mutations).
    # L'app runtime n'a donc pas de droit direct sur audit_events — elle publie
    # un event via outbox_events qu'un worker d'audit (rôle audit_admin) persiste.

    # Liste des partitions audit (alignée sur les 12 mois + default créés plus haut)
    _audit_partitions = [
        "audit_events_2026_04",
        "audit_events_2026_05",
        "audit_events_2026_06",
        "audit_events_2026_07",
        "audit_events_2026_08",
        "audit_events_2026_09",
        "audit_events_2026_10",
        "audit_events_2026_11",
        "audit_events_2026_12",
        "audit_events_2027_01",
        "audit_events_2027_02",
        "audit_events_2027_03",
        "audit_events_default",
    ]

    # agentive_audit_admin : INSERT + SELECT sur audit_events parent + partitions (AC6)
    op.execute("GRANT SELECT, INSERT ON TABLE audit_events TO agentive_audit_admin")
    for partition in _audit_partitions:
        op.execute(f"GRANT SELECT, INSERT ON TABLE {partition} TO agentive_audit_admin")

    # agentive_app : REVOKE toute écriture sur audit_events (y compris SELECT).
    # init.sql applique DEFAULT PRIVILEGES SELECT/INSERT/UPDATE/DELETE pour
    # agentive_app — on les retire explicitement sur les tables d'audit pour
    # enforcer la séparation des rôles définie par AC6.
    op.execute("REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE audit_events FROM agentive_app")
    for partition in _audit_partitions:
        op.execute(f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLE {partition} FROM agentive_app")

    # Immutabilité : REVOKE DELETE/UPDATE pour agentive_audit_admin aussi
    # (seul INSERT autorisé → pas de tamper possible sur l'audit trail).
    op.execute("REVOKE DELETE, UPDATE ON TABLE audit_events FROM agentive_audit_admin")
    for partition in _audit_partitions:
        op.execute(f"REVOKE DELETE, UPDATE ON TABLE {partition} FROM agentive_audit_admin")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 8. Seed data : 1 user owner (John)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.execute(
        """
        INSERT INTO users (email, name, role, tenant_id)
        VALUES ('john@agentive.local', 'John', 'owner', NULL)
        ON CONFLICT (email) DO NOTHING
        """
    )


def downgrade() -> None:
    """Drop everything except the `vector` extension (reverse order).

    Note : on ne DROP PAS l'extension `vector` — elle a été créée par
    `init.sql` (rôle postgres), et d'autres schémas/schemas applicatifs
    pourraient en dépendre. Un downgrade ne doit affecter que le schéma
    applicatif Agentive.
    """
    # Drop audit partitions (DEFAULT + 12 mois)
    for partition in [
        "audit_events_default",
        "audit_events_2027_03",
        "audit_events_2027_02",
        "audit_events_2027_01",
        "audit_events_2026_12",
        "audit_events_2026_11",
        "audit_events_2026_10",
        "audit_events_2026_09",
        "audit_events_2026_08",
        "audit_events_2026_07",
        "audit_events_2026_06",
        "audit_events_2026_05",
        "audit_events_2026_04",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {partition}")
    op.execute("DROP TABLE IF EXISTS audit_events")

    # Drop tenant-ready tables
    op.drop_table("prompts")
    op.drop_table("agent_instances")
    op.drop_table("agent_templates")
    op.drop_table("workflow_runs")
    op.drop_table("workflows")
    op.drop_table("chunk_embeddings")
    op.drop_table("memory_chunks")
    op.drop_table("outbox_events")

    # Drop core tables
    op.drop_table("namespaces")
    op.drop_table("feature_flags")
    op.drop_table("sessions")
    op.drop_table("users")

    # NOTE: l'extension `vector` N'EST PAS droppée — elle a été créée par
    # `init.sql` (rôle superuser postgres) et peut être utilisée par d'autres
    # schémas. Un downgrade du schéma applicatif ne doit pas toucher aux
    # extensions database-wide.
