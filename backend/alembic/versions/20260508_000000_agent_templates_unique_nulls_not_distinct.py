"""agent_templates unique constraint NULLS NOT DISTINCT

Revision ID: 20260508000000
Revises: 20260419000000
Create Date: 2026-05-08

Story 2.1 fix — the original `uq_agent_template (name, version, tenant_id)`
constraint uses Postgres' default NULLS DISTINCT semantics, so two rows with
``tenant_id = NULL`` never collide and the single-tenant MVP path silently
allows duplicate template names. Story 2.1 AC3 (duplicate name → 409) was
catching this bug in smoke tests.

Fix : drop + recreate the constraint with NULLS NOT DISTINCT (Postgres 15+).
This matches the pattern already used on the ``prompts.unique_active_per_template``
constraint and ensures the unique constraint enforces in single-tenant mode.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260508000000"
down_revision = "20260419000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Story 2.1 P-07 — pre-scan duplicates so the migration fails loudly
    # with an actionable hint rather than mid-flight inside ALTER TABLE.
    # Running on a fresh DB this DO block is a no-op (count = 0).
    op.execute(
        """
        DO $$
        DECLARE
            duplicate_count INT;
        BEGIN
            SELECT COUNT(*) INTO duplicate_count FROM (
                SELECT name, version, tenant_id, COUNT(*) AS n
                FROM agent_templates
                GROUP BY name, version, tenant_id
                HAVING COUNT(*) > 1
            ) AS dups;
            IF duplicate_count > 0 THEN
                RAISE EXCEPTION
                    'Migration 20260508000000 cannot proceed: % duplicate (name, version, tenant_id) groups already exist in agent_templates. '
                    'These duplicates predate the NULLS NOT DISTINCT fix and must be resolved manually before retrying. '
                    'Run: SELECT name, version, tenant_id, COUNT(*) FROM agent_templates GROUP BY 1,2,3 HAVING COUNT(*) > 1;',
                    duplicate_count;
            END IF;
        END $$;
        """
    )

    op.drop_constraint("uq_agent_template", "agent_templates", type_="unique")
    # Use raw SQL — SQLAlchemy doesn't expose `NULLS NOT DISTINCT` as a
    # parameter on `UniqueConstraint` for the Alembic op DSL, so we issue
    # the canonical Postgres 15+ form directly.
    op.execute(
        "ALTER TABLE agent_templates "
        "ADD CONSTRAINT uq_agent_template UNIQUE NULLS NOT DISTINCT "
        "(name, version, tenant_id)"
    )


def downgrade() -> None:
    op.drop_constraint("uq_agent_template", "agent_templates", type_="unique")
    op.create_unique_constraint(
        "uq_agent_template",
        "agent_templates",
        ["name", "version", "tenant_id"],
    )
