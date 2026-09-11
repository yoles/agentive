"""Prometheus metrics for the Workflow Engine feature (Story 4.3 T8, AC3).

First metrics module of this feature — registered against the global
:data:`prometheus_client.REGISTRY`, mirror of ``features/playground/metrics.py``
/ ``features/memory_manager/metrics.py``. NOT against
``shared.metrics.get_registry()``: that call is still a ``NotImplementedError``
stub (Epic 7, Story 7.1 undelivered), the same choice already made and
justified in both sibling modules above.

Cardinality is bounded BY DESIGN: ``mode``/``source`` are each a small fixed
set of literals. Never add a ``workflow_id``/``run_id``/``node_id``/
``correlation_id``/``tenant_id`` label here — unbounded cardinality, a
mistake this repo has already corrected twice in other metrics modules
(``LLM_COST_USD_TOTAL``, ``EMBEDDING_COST_USD_TOTAL``). The per-workflow
``% routages déterministes vs LLM`` ratio (AC3) is deliberately NOT a
Prometheus label — it is persisted in ``workflow_runs.metrics`` JSONB and
aggregated in SQL by ``GET /workflows/{workflow_id}/routing-stats``
(``shared/repositories/workflow_repo.py::aggregate_routing_modes``, T10).
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# prometheus_client appends `_total` to Counter sample names automatically.
# Pass the base name here; the registry exposes it as `..._decisions_total`.
ROUTING_DECISIONS_TOTAL: Counter = Counter(
    "agentive_workflow_engine_routing_decisions",
    "Number of hybrid-routing decisions taken, by mode and source.",
    labelnames=["mode", "source"],
)

# Buckets calibrated on the useful range of a short Haiku call, upper-bounded
# by `AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S`'s default (15s).
ROUTING_ESCALATION_SECONDS: Histogram = Histogram(
    "agentive_workflow_engine_routing_escalation_seconds",
    "Latency of a hybrid-routing LLM escalation call.",
    buckets=[0.1, 0.25, 0.5, 1, 2, 5, 10, 15],
)

#: Escalations that never produced a decision (review IG2).
#:
#: A SEPARATE metric rather than a fourth ``source`` value on
#: :data:`ROUTING_DECISIONS_TOTAL`: AC3 fixes that counter's label set to
#: ``source ∈ {dsl, rules, llm}`` and defines it as counting DECISIONS — a
#: failed escalation is precisely the absence of one, so folding it in would
#: both break the declared contract and corrupt the ratio's denominator.
#:
#: Without this, the only escalations invisible to monitoring were the
#: pathological ones: a timeout, an exhausted provider chain or an
#: unusable response raised straight past every counter, so the dashboards
#: showed a feature that never failed while runs died of it.
#:
#: ``reason`` is a closed set of three literals — bounded, like every label
#: in this module.
ROUTING_ESCALATION_FAILURES_TOTAL: Counter = Counter(
    "agentive_workflow_engine_routing_escalation_failures",
    "Hybrid-routing escalations that produced no usable decision, by reason.",
    labelnames=["reason"],
)

__all__ = [
    "ROUTING_DECISIONS_TOTAL",
    "ROUTING_ESCALATION_FAILURES_TOTAL",
    "ROUTING_ESCALATION_SECONDS",
]
