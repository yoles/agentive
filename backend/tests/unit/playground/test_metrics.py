"""Unit tests — Playground Prometheus metrics are registered at import time
(Story 3.5 T7, mirror ``tests/unit/memory_manager/test_metrics.py``)."""

from __future__ import annotations

from prometheus_client import REGISTRY

# Importing the module is the registration side-effect we want to validate.
import agentive_backend.features.playground.metrics  # noqa: F401

_EXPECTED_METRIC_NAMES: set[str] = {
    "agentive_playground_push_memory_chunks_injected",
    "agentive_playground_push_memory_tokens_used",
}


def test_playground_metrics_registered() -> None:
    collected_names: set[str] = set()
    for metric_family in REGISTRY.collect():
        collected_names.add(metric_family.name)
    missing = _EXPECTED_METRIC_NAMES - collected_names
    assert not missing, f"Missing playground metrics in registry: {missing}"


def test_push_memory_counters_are_plain_incrementable_counters() -> None:
    """The two counters accept a positive increment and accumulate it.

    P11 (revue 3.5) : this used to be the ONLY test naming them, which made
    it look like AC3's counter wiring was covered. It is not: calling
    ``.inc()`` here and reading the value back exercises ``prometheus_client``,
    not ``PlaygroundService.run``. The real coverage lives in
    ``test_service.py::test_push_memory_counters_incremented_by_run`` and its
    two siblings, which drive the counters through ``run()`` and would fail if
    the ``.inc()`` calls were deleted from the service.
    """
    from agentive_backend.features.playground.metrics import (
        PLAYGROUND_PUSH_MEMORY_CHUNKS_INJECTED_TOTAL,
        PLAYGROUND_PUSH_MEMORY_TOKENS_USED_TOTAL,
    )

    before_chunks = PLAYGROUND_PUSH_MEMORY_CHUNKS_INJECTED_TOTAL._value.get()
    before_tokens = PLAYGROUND_PUSH_MEMORY_TOKENS_USED_TOTAL._value.get()

    PLAYGROUND_PUSH_MEMORY_CHUNKS_INJECTED_TOTAL.inc(2)
    PLAYGROUND_PUSH_MEMORY_TOKENS_USED_TOTAL.inc(50)

    assert PLAYGROUND_PUSH_MEMORY_CHUNKS_INJECTED_TOTAL._value.get() == before_chunks + 2
    assert PLAYGROUND_PUSH_MEMORY_TOKENS_USED_TOTAL._value.get() == before_tokens + 50
