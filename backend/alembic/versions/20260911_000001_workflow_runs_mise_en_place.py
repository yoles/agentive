"""workflow_runs.mise_en_place — pre-workflow check report column

Revision ID: 20260911000001
Revises: 20260910000001
Create Date: 2026-09-11

Story 4.5 AC1: the Mise en Place hook persists a report (4 `CheckResult`
entries + bypass metadata) for every `POST /workflows/{workflow_id}/runs`
call THAT STARTS A RUN. A refused launch (AC2) creates no `workflow_runs`
row, so it writes nothing here — the report only reaches the caller in the
503 body. Unlike Story 4.4 (Dry Run — deliberately no migration, pure
read), this story writes a new fact, so it needs somewhere to put it:
`WorkflowRun` (`infra/db/models.py`) carried no such column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260911000001"
down_revision = "20260910000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("mise_en_place", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "mise_en_place")
