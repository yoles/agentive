"""Tests Pydantic v2 — UpdateTemplateRequest + ErrorPolicy + LLMParams + ContractDefinition (Story 2.2 T1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentive_backend.features.m2_agent_registry.schemas import (
    ContractDefinition,
    ErrorPolicy,
    LLMParams,
    UpdateTemplateRequest,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# UpdateTemplateRequest
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_update_request_accepts_full_payload() -> None:
    """AC1 happy path — payload complet (8 champs) passe la validation strict."""
    payload = UpdateTemplateRequest(
        system_prompt="Tu es un agent Producteur expert TypeScript.",
        input_contract=ContractDefinition(core={"task": "string"}, extras={}),
        output_contract=ContractDefinition(core={"code": "string"}, extras={}),
        llm_model="claude-3-5-sonnet-20241022",
        llm_params=LLMParams(temperature=0.2, max_tokens=4096),
        provider_chain=["anthropic"],
        error_policy=ErrorPolicy(
            on_timeout="retry_with_backoff", max_retries=3, backoff_strategy="exponential"
        ),
    )
    assert payload.llm_model == "claude-3-5-sonnet-20241022"
    assert payload.provider_chain == ["anthropic"]


def test_update_request_accepts_partial_payload() -> None:
    """AC1 PATCH-like — un champ unique suffit, les autres restent None (= unchanged côté service)."""
    payload = UpdateTemplateRequest(system_prompt="Just the prompt.")
    assert payload.system_prompt == "Just the prompt."
    assert payload.llm_model is None
    assert payload.error_policy is None


def test_update_request_extra_field_forbidden() -> None:
    """AC1 strict mode — un champ inconnu ⇒ Pydantic ValidationError (422 RFC 7807 côté route)."""
    with pytest.raises(PydanticValidationError) as exc:
        UpdateTemplateRequest.model_validate({"system_prompt": "x", "rogue_field": "should fail"})
    # Pydantic v2 utilise "extra_forbidden" comme code d'erreur.
    assert any(err["type"] == "extra_forbidden" for err in exc.value.errors())


def test_update_request_invalid_llm_model() -> None:
    """AC1 — llm_model hors whitelist Literal ⇒ rejet."""
    with pytest.raises(PydanticValidationError):
        UpdateTemplateRequest.model_validate({"llm_model": "gpt-5-omni-1234"})


def test_update_request_invalid_provider_chain_member() -> None:
    """provider_chain ne peut contenir que `anthropic` ou `openai` Sprint 1."""
    with pytest.raises(PydanticValidationError):
        UpdateTemplateRequest.model_validate({"provider_chain": ["mistral"]})


def test_update_request_provider_chain_max_length() -> None:
    """provider_chain bornée à 4 (anti-abuse + sane defaults)."""
    with pytest.raises(PydanticValidationError):
        UpdateTemplateRequest.model_validate(
            {"provider_chain": ["anthropic", "openai", "anthropic", "openai", "anthropic"]}
        )


def test_update_request_provider_chain_no_duplicates_p13() -> None:
    """P-13 fix Story 2.2 review — duplicates rejetés (le runtime fallback
    Story 4.6 retry-erait sur le même provider, défaisant la chain)."""
    with pytest.raises(PydanticValidationError, match="duplicate"):
        UpdateTemplateRequest.model_validate(
            {"provider_chain": ["anthropic", "openai", "anthropic"]}
        )


def test_update_request_empty_payload_rejected_p03() -> None:
    """P-03 fix Story 2.2 review — un payload `{}` produit un audit event
    spurious sur un UPDATE no-op : on refuse en amont via model_validator."""
    with pytest.raises(PydanticValidationError, match="at least one field"):
        UpdateTemplateRequest.model_validate({})


def test_update_request_all_none_rejected_p03() -> None:
    """P-03 fix — un payload avec UNIQUEMENT des champs `null` est aussi rejeté."""
    with pytest.raises(PydanticValidationError, match="at least one field"):
        UpdateTemplateRequest.model_validate(
            {
                "system_prompt": None,
                "input_contract": None,
                "output_contract": None,
                "llm_model": None,
                "llm_params": None,
                "provider_chain": None,
                "error_policy": None,
            }
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ContractDefinition (AC2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_contract_definition_requires_core_dict() -> None:
    """AC2 — `core` n'étant pas un dict ⇒ rejet."""
    with pytest.raises(PydanticValidationError):
        ContractDefinition.model_validate({"core": "not-a-dict", "extras": {}})


def test_contract_definition_defaults_empty_dicts() -> None:
    """AC2 — instanciation sans args ⇒ core et extras = dicts vides."""
    contract = ContractDefinition()
    assert contract.core == {}
    assert contract.extras == {}


def test_contract_definition_extras_accepts_anything() -> None:
    """AC2 — la zone extras est volontairement permissive (`dict[str, Any]`)."""
    contract = ContractDefinition(
        core={"q": "string"}, extras={"meta": {"nested": [1, 2, "three"]}}
    )
    assert contract.extras["meta"]["nested"] == [1, 2, "three"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ErrorPolicy (AC4 — NFR14)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_error_policy_defaults() -> None:
    """AC4 — defaults raisonnables (3 retries exponentiels)."""
    policy = ErrorPolicy()
    assert policy.on_timeout == "retry_with_backoff"
    assert policy.max_retries == 3
    assert policy.backoff_strategy == "exponential"


def test_error_policy_max_retries_bounds() -> None:
    """AC4 — max_retries dans [0, 10]."""
    with pytest.raises(PydanticValidationError):
        ErrorPolicy(max_retries=-1)
    with pytest.raises(PydanticValidationError):
        ErrorPolicy(max_retries=11)


def test_error_policy_invalid_on_timeout() -> None:
    """AC4 — on_timeout hors enum ⇒ rejet."""
    with pytest.raises(PydanticValidationError):
        ErrorPolicy.model_validate({"on_timeout": "ignore_silently"})


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# LLMParams
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_llm_params_temperature_bounds() -> None:
    """temperature ∈ [0.0, 2.0] (intervalle Anthropic + OpenAI)."""
    with pytest.raises(PydanticValidationError):
        LLMParams(temperature=-0.1)
    with pytest.raises(PydanticValidationError):
        LLMParams(temperature=2.1)


def test_llm_params_max_tokens_bounds() -> None:
    """max_tokens ∈ [1, 200_000] (anti-abuse + cohérent avec context windows actuels)."""
    with pytest.raises(PydanticValidationError):
        LLMParams(max_tokens=0)
    with pytest.raises(PydanticValidationError):
        LLMParams(max_tokens=200_001)
