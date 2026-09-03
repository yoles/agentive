"""Unit tests : check_llm_diversity (Story 2.8 T3.5, AC3)."""

from __future__ import annotations

from agentive_backend.features.agent_registry.domain.diversity import (
    INCOMPLETE_CONFIG_REASON,
    SAME_CONFIG_REASON,
    check_llm_diversity,
)
from agentive_backend.features.agent_registry.domain.value_objects import AgentConfig, LLMParams


def _config(
    model: str | None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    *,
    with_params: bool = True,
) -> AgentConfig:
    """Build an ``AgentConfig`` with model and params set INDEPENDENTLY.

    P-08 : the helper used to derive ``llm_params`` from ``model``, so the
    "model absent but params present" branch of the guard was never exercised.
    """
    return AgentConfig(
        llm_model=model,
        llm_params=LLMParams(temperature=temperature, max_tokens=max_tokens)
        if with_params
        else None,
    )


def test_same_model_and_params_is_not_diverse() -> None:
    controller = _config("claude-sonnet-4-6")
    producer = _config("claude-sonnet-4-6")

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is False
    assert result.reason == SAME_CONFIG_REASON


def test_different_model_is_diverse() -> None:
    controller = _config("claude-sonnet-4-6")
    producer = _config("gpt-5")

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is True


def test_same_model_but_different_max_tokens_is_diverse() -> None:
    controller = _config("claude-sonnet-4-6", max_tokens=4096)
    producer = _config("claude-sonnet-4-6", max_tokens=8192)

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is True


def test_same_model_but_different_temperature_is_diverse() -> None:
    controller = _config("claude-sonnet-4-6", temperature=0.7)
    producer = _config("claude-sonnet-4-6", temperature=0.2)

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is True


def test_controller_missing_llm_model_but_having_params_is_incomplete() -> None:
    """P-08 : model absent while params ARE present, the branch the old helper hid."""
    controller = _config(None, with_params=True)
    producer = _config("claude-sonnet-4-6")

    assert controller.llm_params is not None  # guard against the old coupling

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is None
    assert result.reason == INCOMPLETE_CONFIG_REASON


def test_controller_missing_both_model_and_params_is_incomplete() -> None:
    controller = _config(None, with_params=False)
    producer = _config("claude-sonnet-4-6")

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is None
    assert result.reason == INCOMPLETE_CONFIG_REASON


def test_stored_null_llm_params_is_incomplete_not_falsely_identical() -> None:
    """Story 2.8 P-02 : regression guard on the three-state contract.

    Two templates whose stored ``config`` carries ``"llm_params": null`` used to
    be parsed into the defaults ``(0.7, 4096)``, so they compared equal and the
    check answered ``is_diverse=False`` ("falsely blocking") instead of ``None``.
    """
    stored = {"llm_model": "claude-3-5-sonnet-20241022", "llm_params": None}

    result = check_llm_diversity(AgentConfig.from_mapping(stored), AgentConfig.from_mapping(stored))

    assert result.is_diverse is None
    assert result.reason == INCOMPLETE_CONFIG_REASON


def test_producer_missing_llm_params_is_incomplete() -> None:
    """P-08 : params absent while the model IS present, symmetric of the test above."""
    controller = _config("claude-sonnet-4-6")
    producer = _config("claude-sonnet-4-6", with_params=False)

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is None
    assert result.reason == INCOMPLETE_CONFIG_REASON
