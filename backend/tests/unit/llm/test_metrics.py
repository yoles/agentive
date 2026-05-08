"""LLM metrics — registry contract + Gauge zero-leak."""

from __future__ import annotations

from prometheus_client import REGISTRY

from agentive_backend.shared.llm.metrics import (
    LLM_COST_USD_TOTAL,
    LLM_FALLBACK_TRIGGERED_TOTAL,
    LLM_REQUEST_LATENCY_SECONDS,
    LLM_REQUESTS_IN_FLIGHT,
    LLM_TOKENS_TOTAL,
)


def _metric_names() -> set[str]:
    return {m.name for m in REGISTRY.collect()}


def test_all_five_metrics_registered() -> None:
    names = _metric_names()
    assert "agentive_llm_request_seconds" in names
    assert "agentive_llm_tokens" in names
    assert "agentive_llm_fallback_triggered" in names
    assert "agentive_llm_cost_usd" in names
    assert "agentive_llm_requests_in_flight" in names


def test_request_latency_labels() -> None:
    LLM_REQUEST_LATENCY_SECONDS.labels(
        provider="anthropic", model="claude-haiku-4-5", status="success"
    ).observe(0.5)
    # No raise = labels accepted.


def test_tokens_direction_label() -> None:
    LLM_TOKENS_TOTAL.labels(provider="anthropic", model="claude-haiku-4-5", direction="input").inc(
        10
    )
    LLM_TOKENS_TOTAL.labels(provider="anthropic", model="claude-haiku-4-5", direction="output").inc(
        20
    )


def test_fallback_counter_labels() -> None:
    LLM_FALLBACK_TRIGGERED_TOTAL.labels(
        failed_provider="anthropic",
        next_provider="openai",
        error_class="retriable_with_fallback",
    ).inc()


def test_cost_counter_labels() -> None:
    LLM_COST_USD_TOTAL.labels(provider="anthropic", model="claude-haiku-4-5").inc(0.001)


def test_in_flight_gauge_returns_to_zero_after_inc_dec() -> None:
    label_value = "test_in_flight_gauge"
    gauge = LLM_REQUESTS_IN_FLIGHT.labels(provider=label_value)
    # Reset to a known baseline (Prometheus Gauge starts at 0 but tests may
    # have left it elsewhere).
    gauge.set(0)
    gauge.inc()
    assert gauge._value.get() == 1.0  # type: ignore[attr-defined]
    gauge.dec()
    assert gauge._value.get() == 0.0  # type: ignore[attr-defined]
