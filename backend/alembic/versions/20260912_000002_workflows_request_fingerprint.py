"""workflows.request_fingerprint — idempotence de POST /api/v1/workflows

Revision ID: 20260912000002
Revises: 20260912000001
Create Date: 2026-09-12

Story 4.8 AC1 : un client qui rejoue ``POST /api/v1/workflows`` après un
timeout réseau (la première requête ayant déjà committé) ne doit créer ni
seconde row ``workflows`` ni second event ``workflow_engine.workflow.created``.

Le discriminant est le CONTENU, pas une clé cliente : le scénario de l'AC
décrit un client qui rejoue son corps, sans header d'idempotence. La colonne
porte donc le SHA-256 hex de ``{name, dag}`` (``WorkflowService
._request_fingerprint``), et c'est l'unicité posée par Postgres — pas un
``SELECT`` applicatif préalable — qui rend le mécanisme correct sous
concurrence réelle : en ``READ COMMITTED`` deux transactions concurrentes ne
voient pas la row l'une de l'autre et insèreraient toutes les deux.

``NULLS NOT DISTINCT`` (Postgres 15+, ici pg17) : sans lui, la sémantique par
défaut ``NULLS DISTINCT`` rend l'index inopérant en mono-tenant, où
``tenant_id IS NULL`` pour toutes les rows — exactement le bug que la
migration ``20260508000000`` a dû corriger a posteriori sur
``uq_agent_template``.

L'index est émis en SQL brut ici par simple symétrie avec ``20260508000000``,
et NON parce que SQLAlchemy ne saurait pas l'exprimer : une revue de code a
établi que la version épinglée (2.0.49) compile
``Index(..., postgresql_nulls_not_distinct=True, postgresql_where=...)`` vers
exactement la chaîne ci-dessous, caractère pour caractère. L'index EST donc
déclaré dans ``Workflow.__table_args__``, et il devait l'être :
``alembic/env.py`` pointe ``target_metadata`` sur ``Base.metadata``, donc un
index présent en base et absent des modèles est émis en ``DROP INDEX`` par le
prochain ``alembic revision --autogenerate`` — ce qui désactiverait
silencieusement toute l'idempotence de la story.

L'index est PARTIEL (``WHERE request_fingerprint IS NOT NULL``), et c'est le
piège principal de cette migration. La colonne est nullable parce que les
rows créées avant cette story n'ont pas d'empreinte, et les backfiller
supposerait de reproduire en SQL le ``json.dumps(sort_keys=True,
ensure_ascii=False, separators=(",", ":"))`` de Python octet pour octet —
fragile pour une valeur qui n'a aucune utilité rétroactive (personne ne
rejouera la création d'un workflow de la semaine dernière). Mais sous
``NULLS NOT DISTINCT`` deux ``NULL`` sont ÉGAUX : un index total refuserait
la deuxième row sans empreinte et ferait échouer la migration sur toute base
en portant **au moins deux** (une base à une seule row legacy passerait — la
borne est à deux, pas à « non vide »). Le prédicat partiel les sort de
l'index et le problème disparaît.

Contrepartie assumée, cf ADR § *Conséquences assumées* n°5 : les rows hors
index ne peuvent JAMAIS déclencher un rejeu idempotent. Un client qui rejoue
la création d'un workflow antérieur à cette migration obtient un second
workflow et un second event, avec ``201`` / ``idempotent_replay: false``.
Le trou est permanent, mais borné à une population qui ne grandit plus.

Aucun ``GRANT`` ni aucune politique RLS nouvelle : ``workflows`` est déjà
dans ``app_writable_tables`` et porte déjà ``tenant_isolation`` (migration
initiale ``20260419000000``) ; un index n'a ni l'un ni l'autre.

``CREATE UNIQUE INDEX`` ci-dessous est délibérément SANS ``CONCURRENTLY``
(Story 4.14 AC3, mesuré après coup — cette migration elle-même n'est pas
retouchée : seul ce paragraphe documente le résultat). Non problématique ICI
précisément parce que le prédicat partiel ne matche AUCUNE row au moment de
la construction (colonne tout juste ajoutée, entièrement NULL) : le coût est
celui d'un balayage de table, pas d'une écriture d'index. Mesuré en conditions
réelles sur ce dépôt : 13,6 ms sur 100 000 lignes, 46,7 ms sur 1 000 000 —
négligeable. Ces chiffres bornent la DURÉE DE DÉTENTION de l'``ACCESS
EXCLUSIVE``, pas son ACQUISITION : le DDL doit d'abord attendre la fin de
toute transaction conflictuelle sur ``workflows``, et il bloque tout le monde
derrière lui pendant qu'il fait la queue (revue 4.14, finding 10). Sur une
base chargée, poser un ``lock_timeout`` sur la session de migration et
réessayer — cf ``docs/runbooks/concurrent-index-migrations.md``. Un index dont le prédicat matche
une fraction significative des lignes existantes n'a PAS cette propriété et
doit être construit en ``CONCURRENTLY`` (cf
``docs/runbooks/concurrent-index-migrations.md``, confirmé fonctionner sans
modification du harnais Alembic de ce dépôt).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "20260912000002"
down_revision = "20260912000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    # Raw SQL for symmetry with `20260508000000`, not out of necessity: this
    # is character-for-character what the `Index(...)` declared on
    # `Workflow.__table_args__` compiles to under SQLAlchemy 2.0.49. Keep the
    # two in sync — the declaration is what stops autogenerate from dropping
    # the index, this statement is what actually builds it.
    op.execute(
        "CREATE UNIQUE INDEX uq_workflow_request_fingerprint "
        "ON workflows (request_fingerprint, tenant_id) NULLS NOT DISTINCT "
        "WHERE request_fingerprint IS NOT NULL"
    )


def downgrade() -> None:
    # No data step here, unlike the sibling migration `20260912000001`, and
    # the asymmetry is deliberate rather than an oversight: dropping this
    # column strands no row. Pre-4.8 code reads and writes `workflows`
    # without ever looking at `request_fingerprint`; it simply loses the
    # idempotence guarantee and starts creating a second row on a replay —
    # which is exactly the behaviour it had before this story.
    op.execute("DROP INDEX IF EXISTS uq_workflow_request_fingerprint")
    op.drop_column("workflows", "request_fingerprint")
