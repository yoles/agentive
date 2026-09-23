"""Pure error-policy resolution and backoff — Story 4.6 T8.1/T12.2, AC3.

Two properties matter here and neither can be checked by sampling:

* ``resolve_error_policy`` reads FREE-FORM JSONB (``agent_templates.config``
  is not validated at read time), so every corrupted shape must fall back to
  the Story 2.2 defaults instead of raising inside a node.
* ``backoff_delay_s`` must be BOUNDED. An unbounded exponential with
  ``max_retries=10`` would park a node for hours and make it
  indistinguishable from a crashed one to the recovery worker.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentive_backend.features.workflow_engine.domain.error_policy import (
    DEFAULT_BACKOFF_STRATEGY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_ON_TIMEOUT,
    MAX_ERROR_POLICY_RETRIES,
    ResolvedErrorPolicy,
    backoff_delay_s,
    resolve_error_policy,
)

_DEFAULTS = ResolvedErrorPolicy(
    on_timeout=DEFAULT_ON_TIMEOUT,
    max_retries=DEFAULT_MAX_RETRIES,
    backoff_strategy=DEFAULT_BACKOFF_STRATEGY,
)


def test_resolve_error_policy_when_config_is_complete_should_read_every_field() -> None:
    resolved = resolve_error_policy(
        {
            "error_policy": {
                "on_timeout": "fail_fast",
                "max_retries": 7,
                "backoff_strategy": "linear",
            }
        }
    )
    assert resolved == ResolvedErrorPolicy(
        on_timeout="fail_fast", max_retries=7, backoff_strategy="linear"
    )


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({}, id="absent"),
        pytest.param({"error_policy": None}, id="null"),
        pytest.param({"error_policy": "retry_with_backoff"}, id="string-not-object"),
        pytest.param({"error_policy": ["retry_with_backoff"]}, id="list-not-object"),
        pytest.param({"error_policy": {"on_timeout": "explode"}}, id="unknown-on_timeout"),
        pytest.param({"error_policy": {"backoff_strategy": "fibonacci"}}, id="unknown-strategy"),
        pytest.param({"error_policy": {"max_retries": "three"}}, id="max_retries-not-int"),
        pytest.param({"error_policy": {"max_retries": None}}, id="max_retries-null"),
        pytest.param({"error_policy": {"max_retries": -1}}, id="max_retries-negative"),
        pytest.param({"error_policy": {"max_retries": True}}, id="max_retries-bool"),
    ],
)
def test_resolve_error_policy_when_config_is_corrupt_should_fall_back_to_defaults(
    config: dict[str, Any],
) -> None:
    """JSONB is free-form — a bad value must never raise inside a node."""
    assert resolve_error_policy(config) == _DEFAULTS


def test_resolve_error_policy_when_max_retries_exceeds_schema_cap_should_clamp() -> None:
    """`ErrorPolicy` (Story 2.2) validates `le=10`, but the column is free
    JSONB and an import/migration can hold anything."""
    resolved = resolve_error_policy({"error_policy": {"max_retries": 9_999}})
    assert resolved.max_retries == MAX_ERROR_POLICY_RETRIES


def test_resolve_error_policy_when_partially_specified_should_keep_other_defaults() -> None:
    resolved = resolve_error_policy({"error_policy": {"max_retries": 0}})
    assert resolved.max_retries == 0
    assert resolved.on_timeout == DEFAULT_ON_TIMEOUT
    assert resolved.backoff_strategy == DEFAULT_BACKOFF_STRATEGY


@pytest.mark.parametrize(
    ("on_timeout", "expected"),
    [("retry_with_backoff", 3), ("fail_fast", 0), ("fallback_provider", 0)],
)
def test_effective_retries_when_policy_disables_retry_should_be_zero(
    on_timeout: str, expected: int
) -> None:
    """`fail_fast`/`fallback_provider` mean ZERO NODE RETRIES — never "no
    fallback". NFR12's provider chain is not negotiable by template."""
    policy = resolve_error_policy({"error_policy": {"on_timeout": on_timeout, "max_retries": 3}})
    assert policy.effective_retries == expected


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        ("exponential", [1.0, 2.0, 4.0, 8.0]),
        ("linear", [1.0, 2.0, 3.0, 4.0]),
        ("constant", [1.0, 1.0, 1.0, 1.0]),
    ],
)
def test_backoff_delay_s_when_strategy_varies_should_follow_its_formula(
    strategy: str, expected: list[float]
) -> None:
    policy = resolve_error_policy({"error_policy": {"backoff_strategy": strategy}})
    delays = [backoff_delay_s(n, policy, base_s=1.0, max_s=1000.0) for n in range(4)]
    assert delays == expected


def test_backoff_delay_s_when_exponential_grows_should_stop_at_the_cap() -> None:
    policy = resolve_error_policy({"error_policy": {"backoff_strategy": "exponential"}})
    delays = [backoff_delay_s(n, policy, base_s=1.0, max_s=5.0) for n in range(10)]
    assert max(delays) == 5.0
    assert delays[-1] == 5.0


def test_backoff_delay_s_when_attempt_is_large_should_not_overflow() -> None:
    """`base * 2**n` with a big n is a real float overflow risk — the cap
    must be applied without ever computing the unbounded value."""
    policy = resolve_error_policy({"error_policy": {"backoff_strategy": "exponential"}})
    assert backoff_delay_s(5_000, policy, base_s=1.0, max_s=30.0) == 30.0


def test_backoff_delay_s_when_attempt_is_negative_should_clamp_to_first_delay() -> None:
    policy = resolve_error_policy({})
    assert backoff_delay_s(-3, policy, base_s=2.0, max_s=30.0) == 2.0
