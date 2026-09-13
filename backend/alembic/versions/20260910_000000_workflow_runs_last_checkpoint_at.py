"""workflow_runs.last_checkpoint_at — staleness detection column

Revision ID: 20260910000000
Revises: 20260909000000
Create Date: 2026-09-10

Story 4.2 AC3: the recovery worker needs to tell "a run just started" from
"a run has gone silent" apart. ``started_at``/``ended_at`` alone can't make
that distinction — a run started 30s ago and one with no activity for
10 minutes both only have ``started_at`` set. This column is updated
alongside ``workflow_runs.checkpoint`` after every completed node (T2.2),
so ``now() - COALESCE(last_checkpoint_at, started_at)`` is the staleness
signal ``WorkflowRunRepo.list_stale_running`` (T2.3) queries on.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260910000000"
down_revision = "20260909000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("last_checkpoint_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Story 4.11 AC7/T7.1 — `DROP COLUMN` takes `ACCESS EXCLUSIVE` on
    # `workflow_runs`, which can run against LIVE traffic during a staging
    # rollback (`infra/scripts/deploy-staging.sh` applies migrations before
    # swapping the backend). Without a bound, a live writer holding even a
    # row-level lock on this table parks this DDL — and every subsequent
    # statement against `workflow_runs`, reads included — until that writer
    # finishes. `lock_timeout` turns an indefinite park into a typed,
    # bounded failure (`psycopg.errors.LockNotAvailable`) instead. Mirrors
    # the 5s default `AGENTIVE_WORKFLOW_CREATE_LOCK_TIMEOUT_S` already
    # established for the same class of wait (Story 4.14 AC2).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "last_checkpoint_at")
