"""Unit tests : check_llm_diversity (Story 2.8 T3.5, AC3 — moved to
``shared.contracts`` by Story 4.1 T1.5, D84 point 2)."""

from __future__ import annotations

from agentive_backend.shared.contracts.diversity import (
    INCOMPLETE_CONFIG_REASON,
    SAME_CONFIG_REASON,
    LLMSelection,
    check_llm_diversity,
)


def _selection(model: str, temperature: float = 0.7, max_tokens: int = 4096) -> LLMSelection:
    return LLMSelection(model=model, temperature=temperature, max_tokens=max_tokens)


def test_same_model_and_params_is_not_diverse() -> None:
    controller = _selection("claude-sonnet-4-6")
    producer = _selection("claude-sonnet-4-6")

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is False
    assert result.reason == SAME_CONFIG_REASON


def test_different_model_is_diverse() -> None:
    controller = _selection("claude-sonnet-4-6")
    producer = _selection("gpt-5")

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is True


def test_same_model_but_different_max_tokens_is_diverse() -> None:
    controller = _selection("claude-sonnet-4-6", max_tokens=4096)
    producer = _selection("claude-sonnet-4-6", max_tokens=8192)

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is True


def test_same_model_but_different_temperature_is_diverse() -> None:
    controller = _selection("claude-sonnet-4-6", temperature=0.7)
    producer = _selection("claude-sonnet-4-6", temperature=0.2)

    result = check_llm_diversity(controller, producer)

    assert result.is_diverse is True


def test_controller_none_is_incomplete() -> None:
    """Controller LLM config not yet set (Story 2.2 not yet applied)."""
    producer = _selection("claude-sonnet-4-6")

    result = check_llm_diversity(None, producer)

    assert result.is_diverse is None
    assert result.reason == INCOMPLETE_CONFIG_REASON


def test_producer_none_is_incomplete() -> None:
    controller = _selection("claude-sonnet-4-6")

    result = check_llm_diversity(controller, None)

    assert result.is_diverse is None
    assert result.reason == INCOMPLETE_CONFIG_REASON


def test_both_none_is_incomplete() -> None:
    result = check_llm_diversity(None, None)

    assert result.is_diverse is None
    assert result.reason == INCOMPLETE_CONFIG_REASON


def test_identical_selection_instance_compared_with_itself_is_not_diverse() -> None:
    """Comparing a selection with an equal-but-distinct instance — dataclass
    value equality, not identity, drives the comparison."""
    selection_a = _selection("claude-3-5-sonnet-20241022", temperature=0.5, max_tokens=2048)
    selection_b = _selection("claude-3-5-sonnet-20241022", temperature=0.5, max_tokens=2048)

    result = check_llm_diversity(selection_a, selection_b)

    assert result.is_diverse is False
    assert result.reason == SAME_CONFIG_REASON
