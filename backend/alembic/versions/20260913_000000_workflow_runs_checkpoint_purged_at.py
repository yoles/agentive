"""workflow_runs.checkpoint_purged_at — checkpoint retention marker

Revision ID: 20260913000000
Revises: 20260912000002
Create Date: 2026-09-13

Story 4.10 AC1: nothing in this repo ever purges
``checkpoints``/``checkpoint_writes``/``checkpoint_blobs`` (migration
``20260910_000001``) — a terminal run's full node-output history
accumulates in Postgres forever, unbounded. ``CheckpointRetentionWorker``
(``features/workflow_engine/retention.py``) purges a terminal run's
checkpoint history via ``AsyncPostgresSaver.adelete_thread`` once it is
older than ``AGENTIVE_WORKFLOW_CHECKPOINT_RETENTION_DAYS`` (default 90,
mirroring the ``audit_events`` retention precedent).

This column is the marker that makes the sweep idempotent: without it,
every pass would re-select every terminal run past the retention window
forever, re-issuing a (harmless but wasted) ``DELETE`` against tables that
already hold nothing for that ``thread_id``. ``NULL`` covers both "not old
enough yet" and "predates this story" — the two read identically (nothing
purged), which is the correct answer for both.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260913000000"
down_revision = "20260912000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("checkpoint_purged_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Story 4.11 AC7/T7.1 — see `20260910_000000`'s downgrade for why a
    # `lock_timeout` guards every `DROP COLUMN` on this table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "checkpoint_purged_at")
