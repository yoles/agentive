"""CHECK constraint on agent_templates.archetype

Revision ID: 20260709000000
Revises: 20260510000000
Create Date: 2026-07-09

Audit 2026-07 M-05 (§4.4b) — ``agent_templates.archetype`` is an enum-like
String(50) with no DB guard, unlike ``tool_servers.transport`` / ``status``
(CHECK since 20260510000000). The invariant (8 universal archetype IDs,
source ``features/m2_agent_registry/templates/archetype-schema.yaml``) only
lived in the registry/service: ``archetype = 'banana'`` passed the DB.

Note — the audit also flagged ``namespaces.type`` as unguarded; that was a
FALSE POSITIVE: ``ck_namespace_type`` exists since the initial migration
(20260419000000). Only the ORM model was missing the declaration — fixed in
``infra/db/models.py`` (model/DB re-sync, no DDL needed here).

Adding a 9th archetype = YAML entry + a migration extending this CHECK.
That friction is deliberate: archetypes are a stable product concept.

``ALTER TABLE ... ADD CONSTRAINT`` validates existing rows — on a database
with out-of-enum values the migration fails loudly (fix data, then retry).
Rows written through the API are always valid (registry-derived), so this
only triggers on manually-injected data.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260709000000"
down_revision = "20260510000000"
branch_labels = None
depends_on = None

_ARCHETYPE_CHECK = (
    "archetype IN ('orchestrateur', 'chercheur', 'analyste', 'producteur', "
    "'stratege', 'controleur', 'veilleur', 'communicateur')"
)


def upgrade() -> None:
    op.create_check_constraint(
        "ck_agent_template_archetype",
        "agent_templates",
        _ARCHETYPE_CHECK,
    )


def downgrade() -> None:
    op.drop_constraint("ck_agent_template_archetype", "agent_templates", type_="check")
