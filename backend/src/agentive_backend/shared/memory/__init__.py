"""Shared memory ports — cross-feature access to Memory Manager (Story 3.5).

Submodules:
    - push_memory : ``PushMemoryProvider`` (ISP Protocol), ``DEFAULT_SIMILARITY_THRESHOLD``
"""

from agentive_backend.shared.memory.push_memory import (
    DEFAULT_SIMILARITY_THRESHOLD,
    PushMemoryProvider,
)

__all__ = ["DEFAULT_SIMILARITY_THRESHOLD", "PushMemoryProvider"]
