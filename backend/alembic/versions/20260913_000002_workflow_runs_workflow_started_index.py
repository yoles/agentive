"""workflow_runs — composite index for the windowed metrics aggregates

Revision ID: 20260913000002
Revises: 20260913000001
Create Date: 2026-09-13

Serves ``WorkflowRunRepo.aggregate_routing_modes`` /
``aggregate_token_reduction`` (``GET /workflows/{id}/routing-stats`` and
``/handoff-stats``), whose predicate is
``WHERE workflow_id = ? AND started_at >= ?``.

Story 4.10 AC4's trailing window bounded how many rows those endpoints
AGGREGATE, not how many they READ: the only usable index was
``ix_workflow_runs_workflow`` on ``workflow_id`` alone
(``20260419_000000``), and the partial index one revision earlier
(``20260913_000001``) is scoped to ``status = 'running'`` while these
aggregates read terminal rows. Postgres therefore still walked every run of
the workflow and filtered afterwards.

``started_at DESC`` because every consumer reads a TRAILING window, so the
same index also serves a future "latest N runs" query without a sort.

``CREATE INDEX CONCURRENTLY`` with the same replay guards as
``20260913_000001`` — see that migration for why they are required.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260913000002"
down_revision = "20260913000001"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_workflow_runs_workflow_started"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Replayable — see `20260913_000001`.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON workflow_runs (workflow_id, started_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
