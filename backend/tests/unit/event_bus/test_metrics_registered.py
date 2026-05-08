"""Unit tests — Prometheus metrics are registered at import time."""

from __future__ import annotations

from prometheus_client import REGISTRY

# Importing the module is the registration side-effect we want to validate.
import agentive_backend.shared.event_bus.metrics  # noqa: F401

_EXPECTED_METRIC_NAMES: set[str] = {
    "agentive_event_bus_publish_seconds",
    "agentive_event_bus_notify_seconds",
    "agentive_event_bus_outbox_backlog",
    "agentive_event_bus_handler_seconds",
    # prometheus_client strips `_total` from the family name; sample name keeps it.
    "agentive_event_bus_handler_failures",
}


def test_event_bus_metrics_registered() -> None:
    """All four event-bus metrics must be findable in the global registry."""
    collected_names: set[str] = set()
    for metric_family in REGISTRY.collect():
        # Histograms expose `_bucket`, `_count`, `_sum` series — collect base name.
        collected_names.add(metric_family.name)
    missing = _EXPECTED_METRIC_NAMES - collected_names
    assert not missing, f"Missing event-bus metrics in registry: {missing}"
