"""Prometheus metrics for the LLM layer.

Registered against the global :data:`prometheus_client.REGISTRY`. The
``/metrics`` HTTP endpoint is wired in Story 1.9 (observability foundations).

Cardinality discipline
----------------------
Labels are kept to ``provider`` / ``model`` / ``status`` (3-4 values).
Never label by ``correlation_id`` / ``agent_id`` / ``tenant_id`` — that
explodes the time-series cardinality and breaks Prometheus.

Story 9.4 (budget caps) consumes ``LLM_TOKENS_TOTAL`` and
``LLM_COST_USD_TOTAL`` to enforce per-department / per-workflow budgets.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Bucket layout tuned for typical LLM latencies: 100ms (cache hit) up to 30s
# (long generation). Default Prometheus buckets stop at 10s, which would
# undercount slow generations.
_LATENCY_BUCKETS = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    20.0,
    30.0,
    60.0,
)

LLM_REQUEST_LATENCY_SECONDS: Histogram = Histogram(
    "agentive_llm_request_seconds",
    "Wall-clock latency of one provider call (success or failure).",
    labelnames=("provider", "model", "status"),
    buckets=_LATENCY_BUCKETS,
)

LLM_TOKENS_TOTAL: Counter = Counter(
    "agentive_llm_tokens",
    "Tokens consumed by direction (input/output).",
    labelnames=("provider", "model", "direction"),
)

LLM_FALLBACK_TRIGGERED_TOTAL: Counter = Counter(
    "agentive_llm_fallback_triggered",
    "Number of times the router fell back from one provider to another.",
    labelnames=("failed_provider", "next_provider", "error_class"),
)

LLM_COST_USD_TOTAL: Counter = Counter(
    "agentive_llm_cost_usd",
    "Estimated USD cost based on per-model pricing snapshot. "
    "Skipped when the model is absent from MODEL_PRICING (no double-counting).",
    labelnames=("provider", "model"),
)

LLM_REQUESTS_IN_FLIGHT: Gauge = Gauge(
    "agentive_llm_requests_in_flight",
    "Number of LLM provider calls currently in flight.",
    labelnames=("provider",),
)
