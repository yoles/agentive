"""``AgentLLMConfig`` validation — strict bounds enforced."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentive_backend.shared.llm.config import AgentLLMConfig


def test_valid_config() -> None:
    cfg = AgentLLMConfig(
        provider_chain=["anthropic", "openai"],
        model="claude-sonnet-4-6",
        max_tokens=4096,
    )
    assert cfg.temperature == 0.7  # default
    assert cfg.timeout_s == 30.0
    assert cfg.system is None


def test_provider_chain_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        AgentLLMConfig(provider_chain=[], model="m", max_tokens=1)


def test_provider_chain_max_length_4() -> None:
    with pytest.raises(ValidationError):
        AgentLLMConfig(
            provider_chain=["a", "b", "c", "d", "e"],
            model="m",
            max_tokens=1,
        )


def test_max_tokens_zero_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentLLMConfig(provider_chain=["a"], model="m", max_tokens=0)


def test_max_tokens_above_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentLLMConfig(provider_chain=["a"], model="m", max_tokens=200_001)


def test_temperature_above_2_rejected() -> None:
    with pytest.raises(ValidationError):
        AgentLLMConfig(
            provider_chain=["a"],
            model="m",
            max_tokens=1,
            temperature=2.5,
        )


def test_extra_kwarg_forbidden() -> None:
    with pytest.raises(ValidationError):
        AgentLLMConfig(  # type: ignore[call-arg]
            provider_chain=["a"],
            model="m",
            max_tokens=1,
            unknown_field="boom",
        )


def test_frozen() -> None:
    cfg = AgentLLMConfig(provider_chain=["a"], model="m", max_tokens=1)
    with pytest.raises(ValidationError):
        cfg.model = "n"  # type: ignore[misc]
