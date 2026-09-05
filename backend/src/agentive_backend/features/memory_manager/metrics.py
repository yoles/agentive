"""Prometheus metrics for the Memory Manager feature (Story 3.3 T4).

Registered against the global :data:`prometheus_client.REGISTRY`, mirror of
``shared/event_bus/metrics.py`` — first feature-level metrics module in the
repo (the event bus's metrics live under ``shared/`` because the bus itself
is a shared component; this one is feature-scoped).

Keep cardinality low — ``reason`` is bounded to the 2 values
:class:`~agentive_backend.shared.contracts.events.memory_events.MemoryChunkArchivedEvent`
declares (``ttl_expired``, ``archive_after_seconds``). Never add a
``namespace`` label here (unbounded cardinality).
"""

from __future__ import annotations

from prometheus_client import Counter

# prometheus_client appends `_total` to Counter sample names automatically.
# Pass the base name here; the registry exposes it as `..._archived_total`.
MEMORY_MANAGER_CHUNKS_ARCHIVED_TOTAL: Counter = Counter(
    "agentive_memory_manager_chunks_archived",
    "Number of memory_chunks archived by MemoryArchivalWorker, per reason.",
    labelnames=("reason",),
)

__all__ = ["MEMORY_MANAGER_CHUNKS_ARCHIVED_TOTAL"]
