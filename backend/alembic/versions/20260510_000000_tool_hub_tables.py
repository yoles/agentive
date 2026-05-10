"""tool_hub tables — tool_servers + tools + agent_template_tools

Revision ID: 20260510000000
Revises: 20260508000000
Create Date: 2026-05-10

Story 2.5 — Tool Hub MCP plomberie. Crée 3 tables pour la registry des
serveurs MCP, les outils découverts, et la junction d'assignment aux
agent-templates :

- ``tool_servers`` : 1 row par serveur MCP enregistré (UNIQUE name + tenant
  NULLS NOT DISTINCT pour single-tenant). Transport stdio | sse (CHECK).
- ``tools`` : 1 row par outil exposé (UNIQUE server_id + name pour rejeter
  les doublons côté discovery). FK ON DELETE CASCADE vers tool_servers.
- ``agent_template_tools`` : junction PK composite (template_id, tool_id).
  FK ON DELETE CASCADE des deux côtés (suppression template OU tool nettoie
  l'assignment).

RLS ``tenant_isolation`` posée sur les 3 tables — Sprint 1 single-tenant
(tenant_id NULL toujours match), prêt pour Story 12 multi-tenant.

Anti-scope :
- Pas d'index sur ``tools.server_id`` Sprint 1 — UNIQUE (server_id, name)
  inclut server_id en première position, suffit pour les queries
  ``WHERE server_id = ?`` (D52).
- Pas d'index sur ``agent_template_tools.tool_id`` (queries ``WHERE
  agent_template_id = ?`` couvertes par PK composite ; reverse query
  rare Sprint 1).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers
revision: str = "20260510000000"
down_revision: str | Sequence[str] | None = "20260508000000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # tool_servers
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.create_table(
        "tool_servers",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("connection_config", JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "discovered_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "transport IN ('stdio', 'sse')",
            name="ck_tool_server_transport",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_tool_server_status",
        ),
    )
    # UNIQUE (name, tenant_id) NULLS NOT DISTINCT — same pattern as
    # uq_agent_template (Story 2.1 P-07 fix). Without NULLS NOT DISTINCT,
    # 2 rows with tenant_id=NULL would never collide and single-tenant
    # MVP would silently allow duplicate server names.
    op.execute(
        "ALTER TABLE tool_servers "
        "ADD CONSTRAINT uq_tool_server_name UNIQUE NULLS NOT DISTINCT "
        "(name, tenant_id)"
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # tools
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.create_table(
        "tools",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "server_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tool_servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("input_schema", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("output_schema", JSONB(), nullable=True),
        sa.Column(
            "discovered_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.UniqueConstraint("server_id", "name", name="uq_tool_per_server"),
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # agent_template_tools (junction)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    op.create_table(
        "agent_template_tools",
        sa.Column(
            "agent_template_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agent_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tools.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "assigned_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "assigned_by_actor",
            sa.Text(),
            nullable=False,
            server_default="system",
        ),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "agent_template_id",
            "tool_id",
            name="pk_agent_template_tools",
        ),
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # RLS — tenant_isolation policy on the 3 new tables.
    # Sprint 1 single-tenant : tenant_id IS NULL toujours match. Prêt pour
    # Story 12 multi-tenant (le `current_setting('app.tenant_id', true)`
    # est SET LOCAL par BaseRepo.with_tenant en context manager).
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    for table in ("tool_servers", "tools", "agent_template_tools"):
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

    # Grants for agentive_app (cohérent pattern Story 1.1/1.5 — RLS active
    # mais le rôle métier doit pouvoir SELECT/INSERT/UPDATE/DELETE).
    for table in ("tool_servers", "tools", "agent_template_tools"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO agentive_app")


def downgrade() -> None:
    # Order matters : drop the junction first (FK), then tools (FK to servers),
    # then tool_servers.
    op.drop_table("agent_template_tools")
    op.drop_table("tools")
    op.drop_table("tool_servers")
