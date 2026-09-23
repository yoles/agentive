"""namespaces.decay_policy — per-namespace temporal decay configuration

Revision ID: 20260906000000
Revises: 20260904000000
Create Date: 2026-09-06

Story 3.4 AC1: search relevance becomes `similarity x decay_factor(age)`,
and the decay function is configurable per namespace. This column holds
that configuration, read through the `DecayPolicy` domain value object
(`features/memory_manager/domain/value_objects.py`), which is the only
place that parses or serializes it.

A separate column rather than new keys inside `retention_policy`: the two
concern different lifecycles (retention decides when a chunk *disappears*,
decay decides how much a *live* chunk is still worth), and
`RetentionPolicy.to_mapping()` rebuilds its dict from its own two fields
only — piggy-backing decay keys on that JSONB would see them silently
dropped on the next namespace write.

No backfill and no per-type default. `'{}'` already means "no decay"
(`DecayFunction.NONE`, factor 1.0, `final_score == similarity`), so every
namespace created by Stories 3.1-3.3 keeps its exact current search
ordering. Turning decay on is an explicit per-namespace opt-in through
`POST /api/v1/memory/namespaces` (see the story's Dev Notes for why a
per-type default would silently reorder existing results).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260906000000"
down_revision = "20260904000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "namespaces",
        sa.Column(
            "decay_policy",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("namespaces", "decay_policy")
