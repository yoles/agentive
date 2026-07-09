"""Backfill outbox_events.event_type — mN. prefixes → module names

Revision ID: 20260709000001
Revises: 20260709000000
Create Date: 2026-07-09

DDD rename (ADR ``docs/decisions/module-naming.md``) : the ``mN`` planning
prefixes are removed from the published event language. Rule: the event
prefix IS the feature package name (``agent_registry``, ``tool_hub``, …).

This migration rewrites the rows already persisted in
``outbox_events.event_type`` (dev/staging data only — no external consumer
exists at this point, which is precisely why the rename happens NOW).
Unprocessed rows (``processed_at IS NULL``) are thereby replayed under the
NEW names, consistent with the renamed contracts/subscribers.

``audit_events`` has no event_type column (verified) — nothing else stores
event names. Explicit per-value UPDATEs (no SQL regex): the m7 mapping is
resegmented (``m7.playground.run_completed`` → ``playground.run.completed``),
a blind prefix rewrite would get it wrong.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "20260709000001"
down_revision = "20260709000000"
branch_labels = None
depends_on = None

# (old, new) — complete mapping of every mN.* event_type ever published.
_EVENT_TYPE_MAPPING: tuple[tuple[str, str], ...] = (
    ("m2.agent_template.created", "agent_registry.agent_template.created"),
    ("m2.agent_template.updated", "agent_registry.agent_template.updated"),
    ("m2.agent_instance.created", "agent_registry.agent_instance.created"),
    ("m2.agent_template.tool_assigned", "agent_registry.agent_template.tool_assigned"),
    ("m2.agent_template.tool_unassigned", "agent_registry.agent_template.tool_unassigned"),
    ("m5.tool_server.connected", "tool_hub.tool_server.connected"),
    ("m5.tool.discovered", "tool_hub.tool.discovered"),
    ("m5.tool.invoked", "tool_hub.tool.invoked"),
    ("m7.playground.run_completed", "playground.run.completed"),
    ("m3.llm.fallback_triggered", "workflow_engine.llm.fallback_triggered"),
)

_UPDATE = text("UPDATE outbox_events SET event_type = :new WHERE event_type = :old")


def upgrade() -> None:
    conn = op.get_bind()
    for old, new in _EVENT_TYPE_MAPPING:
        conn.execute(_UPDATE, {"old": old, "new": new})


def downgrade() -> None:
    conn = op.get_bind()
    for old, new in _EVENT_TYPE_MAPPING:
        conn.execute(_UPDATE, {"old": new, "new": old})
