"""Unit tests for ``_api_key_configured`` (review P3, Story 4.5).

``_build_llm_router`` falls back to :class:`MockProvider` when a provider's
key is absent OR blank, while Story 4.5's ``llm_providers_configured`` check
decides whether a launch may proceed. The two read the SAME predicate on
purpose: an ``is not None`` test alone clears the gate for a blank
``AGENTIVE_ANTHROPIC_API_KEY=``, which the router will never build a real
provider for.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from agentive_backend.app.lifespan import _api_key_configured, _mock_llm_provider_active
from agentive_backend.shared.config import settings


def test_api_key_configured_when_key_absent_should_be_false() -> None:
    assert _api_key_configured(None) is False


def test_api_key_configured_when_key_blank_should_be_false() -> None:
    """`AGENTIVE_ANTHROPIC_API_KEY=` (exported but empty) yields
    `SecretStr('')`, not `None` — the case the old `is not None` test
    reported as configured."""
    assert _api_key_configured(SecretStr("")) is False


def test_api_key_configured_when_key_present_should_be_true() -> None:
    assert _api_key_configured(SecretStr("sk-ant-xxx")) is True


# ─── mock-provider mode (review IG1) ─────────────────────────────────────


def test_mock_llm_provider_active_when_no_key_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the branch `_build_llm_router` takes to wire `MockProvider`,
    and the one Story 4.5's `llm_providers_configured` check must not
    block on."""
    monkeypatch.setattr(settings, "anthropic_api_key", None)
    monkeypatch.setattr(settings, "openai_api_key", None)
    assert _mock_llm_provider_active() is True


def test_mock_llm_provider_active_when_keys_are_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr(""))
    monkeypatch.setattr(settings, "openai_api_key", SecretStr(""))
    assert _mock_llm_provider_active() is True


def test_mock_llm_provider_inactive_when_one_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PARTIAL configuration builds real providers, not the mock — the
    check must keep blocking a template that targets the unconfigured one."""
    monkeypatch.setattr(settings, "anthropic_api_key", SecretStr("sk-ant-xxx"))
    monkeypatch.setattr(settings, "openai_api_key", None)
    assert _mock_llm_provider_active() is False
