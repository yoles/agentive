"""Repository Pattern — the ONLY way to access the DB from features/.

Direct imports of ``sqlalchemy`` or ``psycopg`` from ``features.*`` are
forbidden (enforced by ``import-linter`` Contract 3 — see ``.import-linter``
at repo root). Every concrete repo subclasses :class:`BaseRepo` and uses
:meth:`BaseRepo.with_tenant` as the canonical transaction entry point so
the Postgres RLS policy ``tenant_isolation`` is bound on every request.

Public API:
    BaseRepo  — context manager ``with_tenant(tenant_id)``.
    UserRepo  — users.
    NamespaceRepo  — namespaces.
    MemoryChunkRepo  — memory_chunks (vector chunks live here).
    ChunkEmbeddingRepo  — chunk_embeddings (per-model embeddings).
    WorkflowRepo / WorkflowRunRepo  — workflow definitions and runs.
    AgentTemplateRepo / AgentInstanceRepo  — agent templates and instances.
    PromptRepo  — versioned prompts.
    AuditEventRepo  — INSERT-only audit log (writer wired in Story 9.1).
    OutboxRepo  — outbox_events public surface (event_bus internals stay
        on raw SQL, see Story 1.5 hors-scope notes).
    FeatureFlagRepo  — global feature flags.
"""

from __future__ import annotations

from agentive_backend.shared.repositories.agent_repo import (
    AgentInstanceRepo,
    AgentTemplateRepo,
)
from agentive_backend.shared.repositories.audit_repo import AuditEventRepo
from agentive_backend.shared.repositories.base import BaseRepo
from agentive_backend.shared.repositories.chunk_embedding_repo import ChunkEmbeddingRepo
from agentive_backend.shared.repositories.feature_flag_repo import FeatureFlagRepo
from agentive_backend.shared.repositories.memory_chunk_repo import MemoryChunkRepo
from agentive_backend.shared.repositories.namespace_repo import NamespaceRepo
from agentive_backend.shared.repositories.outbox_repo import OutboxRepo
from agentive_backend.shared.repositories.prompt_repo import PromptRepo
from agentive_backend.shared.repositories.tool_hub_repo import (
    AgentTemplateToolRepo,
    ToolRepo,
    ToolServerRepo,
)
from agentive_backend.shared.repositories.user_repo import UserRepo
from agentive_backend.shared.repositories.workflow_repo import (
    WorkflowRepo,
    WorkflowRunRepo,
)

__all__ = [
    "AgentInstanceRepo",
    "AgentTemplateRepo",
    "AgentTemplateToolRepo",
    "AuditEventRepo",
    "BaseRepo",
    "ChunkEmbeddingRepo",
    "FeatureFlagRepo",
    "MemoryChunkRepo",
    "NamespaceRepo",
    "OutboxRepo",
    "PromptRepo",
    "ToolRepo",
    "ToolServerRepo",
    "UserRepo",
    "WorkflowRepo",
    "WorkflowRunRepo",
]
