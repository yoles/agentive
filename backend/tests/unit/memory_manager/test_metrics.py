"""Unit tests — Memory Manager Prometheus metrics are registered at import
time (Story 3.3 T10.5, mirror ``tests/unit/event_bus/test_metrics_registered.py``)."""

from __future__ import annotations

from prometheus_client import REGISTRY

# Importing the module is the registration side-effect we want to validate.
import agentive_backend.features.memory_manager.metrics  # noqa: F401

_EXPECTED_METRIC_NAMES: set[str] = {
    # prometheus_client strips `_total` from the family name; sample name keeps it.
    "agentive_memory_manager_chunks_archived",
}


def test_memory_manager_metrics_registered() -> None:
    collected_names: set[str] = set()
    for metric_family in REGISTRY.collect():
        collected_names.add(metric_family.name)
    missing = _EXPECTED_METRIC_NAMES - collected_names
    assert not missing, f"Missing memory_manager metrics in registry: {missing}"


def test_chunks_archived_total_has_a_reason_label() -> None:
    from agentive_backend.features.memory_manager.metrics import (
        MEMORY_MANAGER_CHUNKS_ARCHIVED_TOTAL,
    )

    MEMORY_MANAGER_CHUNKS_ARCHIVED_TOTAL.labels(reason="ttl_expired").inc()
    MEMORY_MANAGER_CHUNKS_ARCHIVED_TOTAL.labels(reason="archive_after_seconds").inc(2)

    samples = {
        sample.labels["reason"]: sample.value
        for metric_family in REGISTRY.collect()
        if metric_family.name == "agentive_memory_manager_chunks_archived"
        for sample in metric_family.samples
        if sample.name.endswith("_total")
    }
    assert samples["ttl_expired"] >= 1
    assert samples["archive_after_seconds"] >= 2
