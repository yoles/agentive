"""HNSW index for voyage-3-lite 512-dimensional embeddings.

Revision ID: 20260909000000
Revises: 20260906000000
Create Date: 2026-09-09

Story 3.6 code review: ``voyage-3-lite`` returns 512-dimensional vectors.
The dimension-less ``chunk_embeddings.embedding`` column accepts them, but
without a matching expression index searches fall back to a sequential scan.
As with the existing BGE-384 and OpenAI-1536 indexes, the model predicate keeps
the fixed-dimension HNSW graph isolated from vectors produced by other models.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260909000000"
down_revision = "20260906000000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX chunk_embeddings_voyage_hnsw
        ON chunk_embeddings
        USING hnsw ((embedding::vector(512)) vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE model = 'voyage-3-lite'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS chunk_embeddings_voyage_hnsw")
