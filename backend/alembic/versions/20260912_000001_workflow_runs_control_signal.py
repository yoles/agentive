"""workflow_runs.control_signal — cooperative pause/cancel request columns

Revision ID: 20260912000001
Revises: 20260911000001
Create Date: 2026-09-12

Story 4.6 AC1/AC2: ``pause`` and ``cancel`` on a LIVE run are REQUESTS, not
transitions — the driver is mid-superstep and will only observe them at the
next superstep boundary, the one instant where stopping loses no work and
cuts no already-billed LLM call. The request therefore needs somewhere to
live that is NOT ``status``.

Why not ``pausing``/``cancelling`` statuses: four independent pieces of code
compare ``status`` against the literal ``"running"`` —
``WorkflowRunRepo.claim_stale_running``, ``update_status(only_if_status=...)``,
``WorkflowExecutionService._resume_run``'s guard and
``WorkflowRecoveryWorker._abandon_one``. A new status value means teaching
all four about it, and each one is a chance to miss a case. A separate
column leaves every one of them correct as written: a running run keeps
saying ``running`` until it actually stops.

It also makes the mechanism multi-worker for free — the signal travels
through the database, not through an in-process task registry — and it makes
an UNOBSERVED request self-healing: a run whose process died before it could
see the signal stays ``running``, so the recovery sweep still claims it, and
the driver settles the signal before executing a single node.

``paused`` and ``cancelled`` need no migration of their own:
``workflow_runs.status`` is a free ``String(50)`` with no CHECK constraint.
Same reasoning for these two columns — the legal values live in
``domain/run_control.py``, where the transition table already is, rather than
being split between a DB constraint and the domain.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260912000001"
down_revision = "20260911000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("control_signal", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "workflow_runs",
        sa.Column("control_requested_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # DATA FIRST, schema second — dropping the columns alone leaves rows the
    # restored code cannot handle.
    #
    # Pre-4.6 code knows exactly two terminal statuses (`completed`,
    # `error`) and has no notion of `paused` or `cancelled`. A downgrade
    # that only drops the columns therefore strands every row this story
    # created:
    #
    # * `paused` is not terminal to the restored code, so no SSE stream ever
    #   closes on it; it is not `running`, so `claim_stale_running` never
    #   claims it; and the `resume` endpoint is gone. The run is immobile
    #   FOREVER — the single worst outcome of the whole migration.
    # * `cancelled` runs are genuinely finished, but the restored code does
    #   not recognise the status as terminal either, so their streams hang
    #   until `_MAX_STREAM_DURATION_S` and the UI shows a status it has no
    #   label for.
    #
    # `paused` -> `running` is not a guess: it is the exact mechanism the
    # upgrade docstring above relies on for an unobserved signal. The row
    # keeps its checkpoint and its NULL `ended_at` (`_observe_control` never
    # sets one on the pause branch — a paused run is suspended, not
    # finished), so the restored recovery sweep claims it once
    # `last_checkpoint_at` goes stale and re-drives it from its checkpoint.
    # The run finishes instead of being lost.
    #
    # `cancelled` -> `error` is LOSSY and deliberately so: `error` is the
    # only terminal non-success status the restored code has. The operator's
    # intent survives in the run's events and metrics, not in `status`.
    #
    # A pending `control_signal` on a still-`running` row needs no handling:
    # dropping the column discards the request, and the run proceeds to
    # completion — which is precisely what the restored code would have done
    # with it.
    #
    # Story 4.11 AC7/T7.1 — the reconciliation above is NOT enough by
    # itself against LIVE traffic (`infra/scripts/deploy-staging.sh` can run
    # this while the previous deploy's backend, still running a 4.6 driver,
    # is mid-superstep). A driver can settle a pending `pause` — writing
    # `status = 'paused'` — in the WINDOW between the reconciliation UPDATE
    # above and the `DROP COLUMN` below, producing exactly the row this
    # migration's own docstring calls unacceptable: immobile forever, since
    # the restored code has no notion of `paused` to reclaim it.
    #
    # `LOCK TABLE ... IN EXCLUSIVE MODE` closes the window: it conflicts
    # with the `ROW EXCLUSIVE` lock any INSERT/UPDATE/DELETE needs, so no
    # other transaction can write a NEW `paused` row once it is held — held
    # for the rest of THIS transaction, past the `DROP COLUMN` below.
    # Ordinary reads are untouched (Postgres MVCC needs no lock for
    # `SELECT`). `lock_timeout` (same 5s default as
    # `AGENTIVE_WORKFLOW_CREATE_LOCK_TIMEOUT_S`, Story 4.14 AC2) makes
    # acquiring it fail fast and typed if a live writer already holds a
    # conflicting lock, instead of this migration parking indefinitely.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE workflow_runs IN EXCLUSIVE MODE")
    op.execute(sa.text("UPDATE workflow_runs SET status = 'running' WHERE status = 'paused'"))
    op.execute(sa.text("UPDATE workflow_runs SET status = 'error' WHERE status = 'cancelled'"))
    op.drop_column("workflow_runs", "control_requested_at")
    op.drop_column("workflow_runs", "control_signal")
