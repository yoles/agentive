"""Prometheus metrics for the event bus.

Registered against the global :data:`prometheus_client.REGISTRY`. The
``/metrics`` HTTP endpoint is wired in Story 1.9 (observability foundations).

Metric design (S1)
------------------
The publish path has two distinct phases — INSERT-into-outbox and NOTIFY-on-
autocommit-conn — with different failure modes (DB contention vs network /
connection-pool saturation). They are measured separately so operators can
diagnose which phase regresses.

The migration-to-Redis-Streams gating (cf.
``docs/decisions/event-bus-migration-trigger.md``) compares the **sum** of
INSERT and NOTIFY p95 latencies against the 100 ms threshold. The exact
PromQL uses ``histogram_quantile`` over both ``_bucket`` series and adds the
results — see the ADR for the canonical query. ``EVENT_BUS_OUTBOX_BACKLOG``
is the second migration signal.

Keep cardinality low — labels limited to ``event_type``.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

EVENT_BUS_PUBLISH_LATENCY: Histogram = Histogram(
    "agentive_event_bus_publish_seconds",
    "Latency of publish() — validate + INSERT outbox row (commit excluded).",
    labelnames=("event_type",),
)

EVENT_BUS_NOTIFY_LATENCY: Histogram = Histogram(
    "agentive_event_bus_notify_seconds",
    "Latency of emit_notify() — open autocommit conn + pg_notify call.",
    labelnames=("event_type",),
)

EVENT_BUS_OUTBOX_BACKLOG: Gauge = Gauge(
    "agentive_event_bus_outbox_backlog",
    "Number of outbox_events rows with processed_at IS NULL (sampled every 30s).",
)

EVENT_BUS_HANDLER_DURATION: Histogram = Histogram(
    "agentive_event_bus_handler_seconds",
    "Per-handler dispatch latency.",
    labelnames=("event_type", "handler_module"),
)

# prometheus_client appends `_total` to Counter sample names automatically.
# Pass the base name here; the registry exposes it as `..._failures_total`.
EVENT_BUS_HANDLER_FAILURES_TOTAL: Counter = Counter(
    "agentive_event_bus_handler_failures",
    "Number of handler invocations that raised an exception.",
    labelnames=("event_type", "handler_module", "error_type"),
)
