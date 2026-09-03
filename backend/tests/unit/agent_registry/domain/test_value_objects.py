"""Unit tests — Agent Registry domain value objects (Sprint 2 DDD Palier 1).

Framework-free, zero I/O, microsecond-fast — the point of the domain core is
that these invariants are testable without a DB, a session mock, or the app.
"""

from __future__ import annotations

import pytest

from agentive_backend.features.agent_registry.domain.value_objects import (
    AgentConfig,
    Archetype,
    Contract,
    DomainValidationError,
    ErrorPolicy,
    LLMParams,
    OnTimeoutPolicy,
    ProviderChain,
    ProviderId,
    Version,
)

# ─── Version ──────────────────────────────────────────────────────


def test_version_next_increments() -> None:
    assert Version(1).next() == Version(2)
    assert Version(41).next().value == 42


def test_version_rejects_below_one() -> None:
    with pytest.raises(DomainValidationError, match="version must be >= 1"):
        Version(0)
    with pytest.raises(DomainValidationError):
        Version(-3)


def test_version_int_and_str() -> None:
    assert int(Version(7)) == 7
    assert str(Version(7)) == "7"


# ─── Archetype ────────────────────────────────────────────────────


def test_archetype_has_the_eight_known_slugs() -> None:
    assert {a.value for a in Archetype} == {
        "orchestrateur",
        "chercheur",
        "analyste",
        "producteur",
        "stratege",
        "controleur",
        "veilleur",
        "communicateur",
    }


def test_archetype_rejects_unknown_slug() -> None:
    with pytest.raises(ValueError, match="banana"):
        Archetype("banana")


# ─── LLMParams ────────────────────────────────────────────────────


def test_llm_params_defaults() -> None:
    params = LLMParams()
    assert params.temperature == 0.7
    assert params.max_tokens == 4096


def test_llm_params_temperature_bounds() -> None:
    with pytest.raises(DomainValidationError, match="temperature"):
        LLMParams(temperature=-0.1)
    with pytest.raises(DomainValidationError, match="temperature"):
        LLMParams(temperature=2.1)


def test_llm_params_max_tokens_bounds() -> None:
    with pytest.raises(DomainValidationError, match="max_tokens"):
        LLMParams(max_tokens=0)
    with pytest.raises(DomainValidationError, match="max_tokens"):
        LLMParams(max_tokens=200_001)


def test_llm_params_round_trip() -> None:
    raw = {"temperature": 0.3, "max_tokens": 2048}
    assert LLMParams.from_mapping(raw).to_mapping() == raw


# ─── ErrorPolicy ──────────────────────────────────────────────────


def test_error_policy_defaults() -> None:
    policy = ErrorPolicy()
    assert policy.on_timeout is OnTimeoutPolicy.RETRY_WITH_BACKOFF
    assert policy.max_retries == 3


def test_error_policy_max_retries_bounds() -> None:
    with pytest.raises(DomainValidationError, match="max_retries"):
        ErrorPolicy(max_retries=-1)
    with pytest.raises(DomainValidationError, match="max_retries"):
        ErrorPolicy(max_retries=11)


def test_error_policy_rejects_unknown_on_timeout() -> None:
    with pytest.raises(ValueError, match="ignore_silently"):
        ErrorPolicy.from_mapping({"on_timeout": "ignore_silently"})


def test_error_policy_round_trip_emits_plain_strings() -> None:
    raw = {"on_timeout": "fail_fast", "max_retries": 5, "backoff_strategy": "linear"}
    out = ErrorPolicy.from_mapping(raw).to_mapping()
    assert out == raw
    # StrEnum members must serialize to plain strings, not "OnTimeoutPolicy.FAIL_FAST".
    assert isinstance(out["on_timeout"], str)
    assert out["on_timeout"] == "fail_fast"


# ─── ProviderChain ────────────────────────────────────────────────


def test_provider_chain_rejects_duplicates() -> None:
    with pytest.raises(DomainValidationError, match="duplicate"):
        ProviderChain((ProviderId.ANTHROPIC, ProviderId.ANTHROPIC))


def test_provider_chain_from_mapping_and_back() -> None:
    chain = ProviderChain.from_mapping(["anthropic", "openai"])
    assert chain.providers == (ProviderId.ANTHROPIC, ProviderId.OPENAI)
    assert chain.to_mapping() == ["anthropic", "openai"]


def test_provider_chain_from_none_is_empty() -> None:
    assert ProviderChain.from_mapping(None).providers == ()


# ─── Contract ─────────────────────────────────────────────────────


def test_contract_round_trip() -> None:
    raw = {"core": {"q": "string"}, "extras": {"meta": "ok"}}
    assert Contract.from_mapping(raw).to_mapping() == raw


def test_contract_from_none_defaults_empty() -> None:
    contract = Contract.from_mapping(None)
    assert contract.to_mapping() == {"core": {}, "extras": {}}


# ─── AgentConfig — single parse/serialize point ───────────────────


def _full_config_dict() -> dict:
    return {
        "prompt_base": "base",
        "role": "producer",
        "system_prompt": "sp",
        "input_contract": {"core": {"q": "string"}, "extras": {}},
        "output_contract": {"core": {}, "extras": {"note": "x"}},
        "llm_model": "claude-3-5-sonnet-20241022",
        "llm_params": {"temperature": 0.2, "max_tokens": 4096},
        "provider_chain": ["anthropic", "openai"],
        "error_policy": {
            "on_timeout": "retry_with_backoff",
            "max_retries": 3,
            "backoff_strategy": "exponential",
        },
    }


def test_agent_config_full_round_trip_is_lossless() -> None:
    raw = _full_config_dict()
    assert AgentConfig.from_mapping(raw).to_mapping() == raw


def test_agent_config_partial_round_trip_preserves_only_present_keys() -> None:
    raw = {"prompt_base": "base", "role": "producer"}
    config = AgentConfig.from_mapping(raw)
    assert config.to_mapping() == raw
    # Absent keys stay absent (progressive-build semantics), not defaulted-in.
    assert "system_prompt" not in config.to_mapping()
    assert "llm_params" not in config.to_mapping()


@pytest.mark.parametrize("stored", [None, {}, "0.7", 0.7, []])
def test_agent_config_llm_params_present_but_not_a_populated_mapping_is_none(
    stored: object,
) -> None:
    """Story 2.8 P-02 — a present-but-empty ``llm_params`` is NOT "configured".

    The key used to be detected with ``"llm_params" in raw``, so a ``null`` /
    ``{}`` / scalar value silently produced the defaults ``(0.7, 4096)``, which
    defeated the three-state contract of ``check_llm_diversity``.
    """
    config = AgentConfig.from_mapping({"llm_model": "gpt-4o", "llm_params": stored})
    assert config.llm_params is None
    assert "llm_params" not in config.to_mapping()


def test_agent_config_from_empty_mapping() -> None:
    assert AgentConfig.from_mapping({}).to_mapping() == {}
    assert AgentConfig.from_mapping(None).to_mapping() == {}


def test_agent_config_merge_updates_overrides_only_non_none() -> None:
    base = AgentConfig.from_mapping({"prompt_base": "base", "role": "producer"})
    merged = base.merge_updates(
        system_prompt="new prompt",
        llm_params=LLMParams(temperature=0.1, max_tokens=1000),
    )
    out = merged.to_mapping()
    # Unchanged keys preserved.
    assert out["prompt_base"] == "base"
    assert out["role"] == "producer"
    # Overridden keys applied.
    assert out["system_prompt"] == "new prompt"
    assert out["llm_params"] == {"temperature": 0.1, "max_tokens": 1000}
    # Original is untouched (frozen VO — merge returns a new instance).
    assert base.system_prompt is None


def test_agent_config_merge_updates_leaves_prompt_base_and_role_untouched() -> None:
    base = AgentConfig.from_mapping({"prompt_base": "keep", "role": "keep-role"})
    merged = base.merge_updates(llm_model="gpt-4o")
    assert merged.prompt_base == "keep"
    assert merged.role == "keep-role"
    assert merged.llm_model == "gpt-4o"
