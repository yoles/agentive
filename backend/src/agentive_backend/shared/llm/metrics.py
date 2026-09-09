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

from decimal import Decimal
from typing import Final

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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Embedding cost — Story 3.6 AC4
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mirror of `LLM_COST_USD_TOTAL` above, labelled `(backend, model)` rather
# than `(provider, model)`: `backend` is the namespace-facing name
# (`local`/`cloud`/`voyage`, `EmbeddingBackend`'s 3 values) an operator
# actually configures, whereas `provider` on the chat side already IS the
# vendor name. Bounded to the 3-4 known (backend, model) pairs — the epic's
# literal ask, `embedding_cost_per_namespace`, would label by `namespace`
# instead, exactly the unbounded-cardinality label this module's own
# docstring (and `features/memory_manager/metrics.py`) already forbid. See
# Story 3.6 Dev Notes § Métriques coût for the full rationale.
EMBEDDING_COST_USD_TOTAL: Counter = Counter(
    "agentive_embedding_cost_usd",
    "Estimated USD cost of embedding calls based on EMBEDDING_MODEL_PRICING. "
    "Zero for the local backend (no network call is billed).",
    labelnames=("backend", "model"),
)

# USD per 1M INPUT tokens — embeddings have no output tokens, unlike chat's
# `MODEL_PRICING` tuple (`infra/llm/openai_adapter.py`). Token count is
# estimated with the same `len(text) // 4` heuristic as Story 3.5
# (`features/playground/service.py::_estimate_tokens` — no real tokenizer
# dependency exists in this repo). Snapshot verified 2026-09-08 against
# https://platform.openai.com/docs/pricing and
# https://docs.voyageai.com/docs/pricing — reverify before relying on it for
# real budgeting (same "Snapshot ... — verify against" posture as chat's
# `MODEL_PRICING`).
EMBEDDING_MODEL_PRICING: Final[dict[str, Decimal]] = {
    "text-embedding-3-small": Decimal("0.02"),
    "voyage-3-lite": Decimal("0.02"),
    "bge-small-en-v1.5": Decimal("0"),
}
