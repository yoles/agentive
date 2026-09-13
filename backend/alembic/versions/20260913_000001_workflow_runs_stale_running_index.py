"""workflow_runs — partial expression index for the recovery sweep

Revision ID: 20260913000001
Revises: 20260913000000
Create Date: 2026-09-13

Story 4.10 AC3: ``WorkflowRunRepo.claim_stale_running`` filters
``status = 'running'`` and orders by ``COALESCE(last_checkpoint_at,
started_at)`` every ``interval_s`` (30s by default), per API replica — and
until this migration the only index on this table was
``ix_workflow_runs_workflow`` (``workflow_id``), so every sweep tick paid a
full sequential scan plus a sort once the table stopped being tiny.

``CREATE INDEX CONCURRENTLY`` — this table can carry production rows by the
time this migration runs, and an ordinary ``CREATE INDEX`` takes
``ACCESS EXCLUSIVE`` for the whole build. Confirmed working with no harness
changes by Story 4.14 T3.1 (``docs/runbooks/concurrent-index-migrations.md``):
``op.get_context().autocommit_block()`` commits the ambient transaction and
runs the statement outside one, which is what ``CONCURRENTLY`` requires.
Only this statement is wrapped — everything else in a migration that had
more than one step would keep ordinary transactional DDL, though this one
has nothing else to do.

PARTIAL + EXPRESSION, not a plain index on ``status``: the sweep only ever
reads ``running`` rows, which shrink toward a small, roughly-constant
fraction of the table as it grows (most rows are terminal) — a total index
would pay to maintain entries the sweep never reads. The expression matches
the query's own ``ORDER BY`` exactly (see
``infra/db/models.py::WorkflowRun.__table_args__``, the same declaration
this migration's raw SQL mirrors byte for byte, for the reason Story 4.8 P4
already established: an index absent from ``Base.metadata`` is a
``DROP INDEX`` waiting for the next ``alembic revision --autogenerate``).
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260913000001"
down_revision = "20260913000000"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_workflow_runs_stale_running"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Replayable. A `CREATE INDEX CONCURRENTLY` interrupted mid-build
        # leaves an INVALID index that Postgres never rebuilds, and since
        # `autocommit_block()` runs outside a transaction it is not rolled
        # back while the revision is also never stamped — so every later
        # `alembic upgrade head` fails on "already exists" until a human
        # drops it (`docs/runbooks/concurrent-index-migrations.md`).
        # The DROP is unconditional because `IF NOT EXISTS` alone would keep
        # an invalid index, which the planner never uses; dropping a valid
        # one on replay is harmless since the rebuild is CONCURRENTLY.
        # `DROP INDEX CONCURRENTLY` cannot run inside a `DO` block, so the
        # invalid-only variant is unavailable.
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} ON workflow_runs "
            "(COALESCE(last_checkpoint_at, started_at)) WHERE status = 'running'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
