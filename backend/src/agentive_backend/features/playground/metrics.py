"""Prometheus metrics for the Playground feature (Story 3.5 T7, AC3).

Registered against the global :data:`prometheus_client.REGISTRY`, mirror of
``memory_manager/metrics.py`` (Story 3.3) — first metrics module of the
Playground feature. Against the global registry directly (NOT
``shared.metrics.get_registry()``, still a ``NotImplementedError`` stub,
Sprint 0, Story 7.1 undelivered) — same choice already documented and
justified in ``memory_manager/metrics.py``.

No labels on either counter : cardinality controlled by design (never add a
``namespace`` or ``template_id`` label here — unbounded cardinality).
"""

from __future__ import annotations

from prometheus_client import Counter

# prometheus_client appends `_total` to Counter sample names automatically.
# Pass the base name here; the registry exposes it as `..._injected_total`
# / `..._used_total`.
PLAYGROUND_PUSH_MEMORY_CHUNKS_INJECTED_TOTAL: Counter = Counter(
    "agentive_playground_push_memory_chunks_injected",
    "Number of memory chunks injected into a Playground run's prompt by Push Memory.",
)

PLAYGROUND_PUSH_MEMORY_TOKENS_USED_TOTAL: Counter = Counter(
    "agentive_playground_push_memory_tokens_used",
    "Estimated tokens spent on Push Memory chunks injected into a Playground run's prompt.",
)

__all__ = [
    "PLAYGROUND_PUSH_MEMORY_CHUNKS_INJECTED_TOTAL",
    "PLAYGROUND_PUSH_MEMORY_TOKENS_USED_TOTAL",
]
