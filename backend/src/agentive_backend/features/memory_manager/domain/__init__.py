"""Domain core for the Memory Manager bounded context.

Framework-free layer (dataclasses + :class:`enum.StrEnum`, no Pydantic, no
SQLAlchemy) introduced *ahead* of the feature implementation, per the Sprint 2
technical audit (Étape 4 §4.5): the memory retention / expiry rule is scoped as
a domain concept now, so the eventual service never reproduces the anemic model.

* :mod:`.value_objects` — ``NamespaceType`` and ``RetentionPolicy`` (single
  ``from_mapping``/``to_mapping`` over the ``namespaces.retention_policy``
  JSONB; owns the write-time "when does a chunk expire / archive" decision).
* :mod:`.entities` — ``MemoryChunk`` (owns the read-time "is this chunk expired
  / archivable now" decision).
"""

from __future__ import annotations

from agentive_backend.features.memory_manager.domain.entities import MemoryChunk
from agentive_backend.features.memory_manager.domain.value_objects import (
    DomainValidationError,
    NamespaceType,
    RetentionPolicy,
)

__all__ = [
    "DomainValidationError",
    "MemoryChunk",
    "NamespaceType",
    "RetentionPolicy",
]
