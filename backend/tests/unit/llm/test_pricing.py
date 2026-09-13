"""Unit tests — :func:`provider_for_model` (Story 4.12 AC6 / D98).

No test file existed for ``infra/llm/pricing.py`` before this story —
closed here alongside the fix.
"""

from __future__ import annotations

import pytest

from agentive_backend.infra.llm.pricing import (
    ANTHROPIC_MODEL_PRICING,
    OPENAI_MODEL_PRICING,
    provider_for_model,
)


def test_provider_for_model_resolves_a_priced_anthropic_model() -> None:
    assert provider_for_model("claude-sonnet-4-6") == "anthropic"


def test_provider_for_model_resolves_a_priced_openai_model() -> None:
    assert provider_for_model("gpt-5") == "openai"


def test_provider_for_model_falls_back_to_prefix_for_an_unpriced_anthropic_model() -> None:
    """THE bug (Story 4.9 D98): a model released after this file's pricing
    snapshot must still resolve an owner, or `provider_chain` rotation and
    coherence checks silently no-op for it."""
    unpriced_model = "claude-opus-5"
    assert unpriced_model not in ANTHROPIC_MODEL_PRICING
    assert provider_for_model(unpriced_model) == "anthropic"


def test_provider_for_model_falls_back_to_prefix_for_an_unpriced_openai_model() -> None:
    unpriced_model = "gpt-6-does-not-exist-yet"
    assert unpriced_model not in OPENAI_MODEL_PRICING
    assert provider_for_model(unpriced_model) == "openai"


def test_provider_for_model_still_returns_none_for_a_genuinely_unknown_family() -> None:
    """The fallback is a NAMING-FAMILY match, not a guess for everything —
    a model from a provider this repo has no notion of stays `None`, exactly
    as it did before this story."""
    assert provider_for_model("mistral-large-2") is None


def test_provider_for_model_prefers_the_exact_pricing_entry_over_the_prefix() -> None:
    """The pricing table is checked FIRST — a priced model never falls
    through to the (coarser) prefix match even though it would also match."""
    assert provider_for_model("claude-haiku-4-5") == "anthropic"


@pytest.mark.parametrize(
    "model",
    ["gpt-oss-120b", "gpt-oss-20b", "gpt-neox-20b", "gpt-j-6b"],
)
def test_a_third_party_gpt_family_is_not_claimed_for_openai(model: str) -> None:
    """`gpt-oss-*` and friends are real open-weights families, commonly
    self-hosted or served behind a gateway. Claiming them for OpenAI makes
    `resolve_provider_chain` rotate OpenAI to the head of the chain, so the
    runtime posts a model OpenAI has never heard of to its API — a `fatal`
    400 the dispatcher refuses to retry. `None` means "do not rotate", which
    leaves the operator's configured chain alone."""
    assert provider_for_model(model) is None


@pytest.mark.parametrize(
    "model",
    ["gpt-4", "gpt-4o", "gpt-5", "gpt-5-mini", "gpt-3.5-turbo", "gpt-6-does-not-exist-yet"],
)
def test_every_openai_gpt_generation_still_resolves_including_unreleased_ones(
    model: str,
) -> None:
    """The P-10 narrowing must not re-open D98. Requiring a DIGIT after the
    stem — rather than listing generations, which would need an edit per
    release — keeps future models resolving while excluding the third-party
    families above."""
    assert provider_for_model(model) == "openai"
