"""``_build_llm_router`` matrix — env-var-driven provider wiring."""

from __future__ import annotations

import pytest

from agentive_backend.shared.llm.testing import MockProvider


@pytest.fixture
def fresh_settings(monkeypatch: pytest.MonkeyPatch):
    """Yield the canonical Settings — patches caller-controlled keys + env."""
    from agentive_backend.shared import config

    def _build(*, anthropic: str | None, openai: str | None, environment: str):
        monkeypatch.setenv("NODE_ENV", environment)
        # Production Settings refuses dev placeholders — always ship a non-
        # placeholder set of internal secrets so we exercise the LLM branch
        # rather than tripping the secret validator.
        from cryptography.fernet import Fernet

        monkeypatch.setenv("AGENTIVE_API_TOKEN", "test-fixture-token")
        monkeypatch.setenv("AGENTIVE_ENCRYPTION_KEY", Fernet.generate_key().decode())
        monkeypatch.setenv("POSTGRES_APP_PASSWORD", "test_fixture_app_pw")
        monkeypatch.setenv("POSTGRES_OWNER_PASSWORD", "test_fixture_owner_pw")
        for var, value in (
            ("ANTHROPIC_API_KEY", anthropic),
            ("OPENAI_API_KEY", openai),
        ):
            if value is None:
                monkeypatch.delenv(var, raising=False)
            else:
                monkeypatch.setenv(var, value)
        # Recompute settings with the patched env.
        new_settings = config.Settings()
        monkeypatch.setattr(config, "settings", new_settings)
        # `app.lifespan` imports `settings` by name at module load — replace
        # the rebound name there too.
        from agentive_backend.app import lifespan as lifespan_module

        monkeypatch.setattr(lifespan_module, "settings", new_settings)

    return _build


def test_both_keys_set_yields_chain_anthropic_first(fresh_settings) -> None:
    fresh_settings(
        anthropic="sk-ant-test-fake-1234567890abcdefghij1234567890",
        openai="sk-test-fake-key-1234567890abcdefghij123456",
        environment="development",
    )
    from agentive_backend.app.lifespan import _build_llm_router

    router = _build_llm_router()
    assert sorted(router.providers) == ["anthropic", "openai"]
    assert router.default_chain == ("anthropic", "openai")


def test_anthropic_only_yields_single_provider_chain(fresh_settings) -> None:
    fresh_settings(
        anthropic="sk-ant-test-fake-1234567890abcdefghij1234567890",
        openai=None,
        environment="development",
    )
    from agentive_backend.app.lifespan import _build_llm_router

    router = _build_llm_router()
    assert list(router.providers) == ["anthropic"]
    assert router.default_chain == ("anthropic",)


def test_openai_only_yields_single_provider_chain(fresh_settings) -> None:
    fresh_settings(
        anthropic=None,
        openai="sk-test-fake-key-1234567890abcdefghij123456",
        environment="development",
    )
    from agentive_backend.app.lifespan import _build_llm_router

    router = _build_llm_router()
    assert list(router.providers) == ["openai"]
    assert router.default_chain == ("openai",)


def test_no_keys_in_test_env_yields_mock(fresh_settings) -> None:
    fresh_settings(anthropic=None, openai=None, environment="test")
    from agentive_backend.app.lifespan import _build_llm_router

    router = _build_llm_router()
    assert list(router.providers) == ["mock"]
    assert router.default_chain == ("mock",)
    assert isinstance(router.providers["mock"], MockProvider)


def test_no_keys_in_production_raises(fresh_settings) -> None:
    fresh_settings(anthropic=None, openai=None, environment="production")
    from agentive_backend.app.lifespan import _build_llm_router

    with pytest.raises(RuntimeError, match="any LLM provider"):
        _build_llm_router()
