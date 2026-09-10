"""LangGraph checkpointer tables — checkpoints/checkpoint_writes/checkpoint_blobs

Revision ID: 20260910000001
Revises: 20260910000000
Create Date: 2026-09-10

Story 4.2 (ADR ``docs/decisions/m3-spike-result.md``, section "Schema
interactions"): ``AsyncPostgresSaver.setup()`` creates its OWN tables —
these are the LangGraph engine's source of truth for node history, channel
values and checkpoint blobs. They are NOT the ``workflow_runs.checkpoint``
JSONB column (a separate, application-level summary — see T2.2).

Why the SYNCHRONOUS ``PostgresSaver`` here, not ``AsyncPostgresSaver``:
``alembic/env.py`` runs every migration's ``upgrade()`` synchronously under
``connection.run_sync(...)`` (the async engine is wrapped for Alembic's
sync migration API). There is no clean nested event loop to ``await``
``AsyncPostgresSaver.setup()`` from inside that callback. The sync
``PostgresSaver`` variant is the same package, produces the identical DDL,
and needs no event loop — the runtime app still uses ``AsyncPostgresSaver``
(Story 4.2 T5/T9), only this migration uses the sync class.

Runs with ``agentive_owner`` (``CREATE TABLE`` privilege — same role Alembic
already connects as, cf ``alembic/env.py``). Grants DML-only privileges to
``agentive_app`` afterward, mirroring the least-privilege posture of
``20260419_000000_initial.py``'s grant block — the app runtime never needs
``CREATE``/``DROP`` on these tables, only the LangGraph saver's own
read/write access to them at run/resume time.
"""

from __future__ import annotations

from alembic import op

from agentive_backend.shared.config import settings

# revision identifiers, used by Alembic.
revision = "20260910000001"
down_revision = "20260910000000"
branch_labels = None
depends_on = None

_CHECKPOINT_TABLES = ("checkpoints", "checkpoint_writes", "checkpoint_blobs")


def _owner_dsn() -> str:
    """Canonical ``postgresql://`` DSN for the (sync) ``PostgresSaver``.

    Mirror exact of ``spike/m3_langgraph.py::_checkpoint_dsn`` — LangGraph /
    psycopg want the plain scheme, not SQLAlchemy's ``+psycopg`` dialect
    marker.
    """
    return settings.psycopg_dsn_owner


def upgrade() -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(_owner_dsn()) as saver:
        saver.setup()

    for table in _CHECKPOINT_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentive_app') THEN
                    GRANT SELECT, INSERT, UPDATE, DELETE
                        ON TABLE {table} TO agentive_app;
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    # Guarded like the DROPs below. A bare REVOKE aborts if the table (or the
    # role) is absent — so a downgrade from a PARTIALLY-applied upgrade, or on
    # a database provisioned without `agentive_app`, failed on its very first
    # statement and left the rest of the teardown undone. Postgres has no
    # `REVOKE ... IF EXISTS`, hence the catalog checks.
    for table in _CHECKPOINT_TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_tables WHERE tablename = '{table}')
                   AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agentive_app')
                THEN
                    REVOKE SELECT, INSERT, UPDATE, DELETE
                        ON TABLE {table} FROM agentive_app;
                END IF;
            END $$;
            """
        )
    # Reverse FK dependency order: checkpoint_blobs/checkpoint_writes
    # reference checkpoints — drop the dependents first. `checkpoint_migrations`
    # (the saver's own schema-version bookkeeping table, `agentive_owner`-only —
    # never granted to `agentive_app`, cf module docstring) has no FK to the
    # other three and can be dropped in any order.
    op.execute("DROP TABLE IF EXISTS checkpoint_blobs")
    op.execute("DROP TABLE IF EXISTS checkpoint_writes")
    op.execute("DROP TABLE IF EXISTS checkpoints")
    op.execute("DROP TABLE IF EXISTS checkpoint_migrations")
