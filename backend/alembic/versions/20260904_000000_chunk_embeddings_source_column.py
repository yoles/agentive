"""chunk_embeddings.source — provenance of a stored vector

Revision ID: 20260904000000
Revises: 20260709000001
Create Date: 2026-09-04

Story 3.1 code review, intent gap IG3: outside production, `MockEmbedder`
persists deterministic sha256-derived vectors under the very same `model`
string a real OpenAI embedding would carry ("text-embedding-3-small") —
nothing in the row distinguishes one from the other. If an environment
that is supposed to have `OPENAI_API_KEY` boots without it (misconfigured
`development`/`test`, a lost secret after rotation), the app degrades to
the mock silently (a WARNING log, not a boot failure) and keeps writing
data that looks exactly like the real thing.

This migration does not change search behaviour — mock and real rows are
still searched together, which is what local/CI development actually
wants. It only makes the provenance queryable after the fact, so a
poisoned namespace can be found (`SELECT ... WHERE source = 'mock'`) and
re-embedded once fixed, instead of silently degrading search quality
forever.

Nullable, no backfill: no row predates this migration in any environment
that has actually run Story 3.1 (freshly shipped), and NULL simply means
"unknown provenance" for any that somehow do.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260904000000"
down_revision = "20260709000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunk_embeddings",
        sa.Column("source", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chunk_embeddings", "source")
