"""Repository Pattern — the ONLY way to access the DB from features/.

**Stub Sprint 0** — full implementation in Story 1.5.

Direct imports of `sqlalchemy` or `psycopg` from `features.*` are forbidden
(enforced by `import-linter` — see `.import-linter` at repo root).

Future public API :
    AgentRepo, MemoryChunkRepo, WorkflowRepo, AuditEventRepo, ...

Every repo method wraps `SET LOCAL app.tenant_id = :tenant_id` at the start
of each transaction to enforce RLS (Row Level Security).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any


class BaseRepo:
    """Placeholder — concrete base implemented in Story 1.5."""

    async def with_tenant(self, tenant_id: Any | None) -> AsyncIterator[Any]:
        raise NotImplementedError(
            "Repository Pattern base implemented in Story 1.5 "
            "(SET LOCAL app.tenant_id context manager + AsyncSession wrapper)."
        )
