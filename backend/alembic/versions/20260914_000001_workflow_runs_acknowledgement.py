"""workflow_runs.acknowledgement — accusé de réception rendu au client

Revision ID: 20260914000001
Revises: 20260913000002
Create Date: 2026-09-14

Story 5.1 AC2 : la première frame SSE d'un run doit porter « Compris. Je
mobilise [agents]. ETA ~[X] min. », y compris pour un client qui s'attache
APRÈS le démarrage (frame `state` de rattrapage).

Colonne dédiée plutôt qu'une clé de `checkpoint` : `_sync_checkpoint`
REMPLACE le dict `checkpoint` en entier dès que le premier node atterrit
(cf `WorkflowRunRepo.create_in_session`, docstring du paramètre
`checkpoint`), donc l'accusé y disparaîtrait exactement au moment où le
rattrapage sert. Mirror exact de `20260911_000001` (`mise_en_place`) :
écrite une fois, à la création du run, jamais réécrite.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "20260914000001"
down_revision = "20260913000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `ADD COLUMN` prend un ACCESS EXCLUSIVE sur `workflow_runs` au même titre
    # que le `DROP COLUMN` du downgrade : derrière une transaction longue il
    # se met en file et bloque TOUTE lecture et écriture de la table la plus
    # chaude de cette feature, pour la durée de l'attente. Le raisonnement du
    # downgrade ne portait que sur le DROP ; il vaut des deux côtés.
    #
    # La colonne est `NULL`-able et sans défaut, donc l'ALTER lui-même est
    # instantané (métadonnée seule depuis PG 11) : les 5 s bornent l'ATTENTE
    # du verrou, pas le travail. Dépassé, la migration échoue proprement et
    # se rejoue — plutôt que de tenir l'API en otage.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "workflow_runs",
        sa.Column("acknowledgement", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    # Story 4.11 AC7/T7.1 — see `20260910_000000`'s downgrade for why a
    # `lock_timeout` guards every `DROP COLUMN` on this table.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("workflow_runs", "acknowledgement")
