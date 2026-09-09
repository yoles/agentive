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


# ─────────────────────────────────────────────────────────────
# `_build_embedding_router` (Story 3.1 T1.5, renamed + extended Story 3.6
# T6.1): same decision matrix for the mandatory `cloud` backend as
# `_build_llm_router`, minus the fallback chain, PLUS `local` (always
# attempted, degrades silently on failure) and `voyage` (only if
# `VOYAGE_API_KEY` is set). Added by the Story 3.1 code review (P12): the
# function can refuse the production boot and had no test at all, every
# suite short-circuiting it via `app.state.embedder`
# (now `app.state.embedding_router`).
#
# `fastembed.TextEmbedding` is mocked in every test below (same convention
# as `test_fastembed_adapter.py`) so this suite never depends on network
# access to the HuggingFace Hub — `_build_embedding_router` always attempts
# the `local` backend regardless of which key is under test.
# ─────────────────────────────────────────────────────────────


def test_build_embedding_router_with_openai_key_wires_openai_as_cloud(fresh_settings) -> None:
    fresh_settings(
        anthropic=None,
        openai="sk-test-fake-key-1234567890abcdefghij123456",
        environment="development",
    )
    from unittest.mock import patch

    from agentive_backend.app.lifespan import _build_embedding_router
    from agentive_backend.infra.llm import OpenAIProvider

    with patch("agentive_backend.infra.llm.fastembed_adapter.TextEmbedding"):
        router = _build_embedding_router()

    embedder, model = router.resolve("cloud")
    assert isinstance(embedder, OpenAIProvider)
    assert model == "text-embedding-3-small"


def test_build_embedding_router_without_key_in_test_env_yields_mock_cloud(fresh_settings) -> None:
    fresh_settings(anthropic=None, openai=None, environment="test")
    from unittest.mock import patch

    from agentive_backend.app.lifespan import _build_embedding_router
    from agentive_backend.shared.llm.testing import MockEmbedder

    with patch("agentive_backend.infra.llm.fastembed_adapter.TextEmbedding"):
        router = _build_embedding_router()

    embedder, _model = router.resolve("cloud")
    assert isinstance(embedder, MockEmbedder)


def test_build_embedding_router_without_key_in_production_raises(fresh_settings) -> None:
    fresh_settings(anthropic=None, openai=None, environment="production")
    from agentive_backend.app.lifespan import _build_embedding_router

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _build_embedding_router()


def test_build_embedding_router_treats_whitespace_only_key_as_missing(fresh_settings) -> None:
    """P12 (Story 3.1): a blank secret is truthy. Before the fix the
    production boot succeeded and every `/memory/*` request then failed 401
    at runtime."""
    fresh_settings(anthropic=None, openai="   ", environment="production")
    from agentive_backend.app.lifespan import _build_embedding_router

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _build_embedding_router()


def test_build_embedding_router_always_wires_local_backend(fresh_settings) -> None:
    """Story 3.6 T6.1 — `local` needs no API key, unlike `cloud`/`voyage`."""
    fresh_settings(anthropic=None, openai=None, environment="test")
    from unittest.mock import patch

    from agentive_backend.app.lifespan import _build_embedding_router
    from agentive_backend.infra.llm import FastEmbedProvider

    with patch("agentive_backend.infra.llm.fastembed_adapter.TextEmbedding"):
        router = _build_embedding_router()

    embedder, model = router.resolve("local")
    assert isinstance(embedder, FastEmbedProvider)
    assert model == "bge-small-en-v1.5"


def test_build_embedding_router_degrades_when_local_backend_fails_to_load(fresh_settings) -> None:
    """T6.1 decision: a FastEmbed load failure (network unreachable, e.g.)
    must not block the process boot — `"local"` is simply absent from
    `providers`, and `resolve("local")` degrades to `"cloud"`."""
    fresh_settings(anthropic=None, openai=None, environment="test")
    from unittest.mock import patch

    from agentive_backend.app.lifespan import _build_embedding_router
    from agentive_backend.shared.llm.testing import MockEmbedder

    with patch(
        "agentive_backend.infra.llm.fastembed_adapter.TextEmbedding",
        side_effect=RuntimeError("no network"),
    ):
        router = _build_embedding_router()

    embedder, model = router.resolve("local")
    assert isinstance(embedder, MockEmbedder)
    assert model == "text-embedding-3-small"


def test_build_embedding_router_skips_voyage_without_key(fresh_settings) -> None:
    """Absent `VOYAGE_API_KEY`, `"voyage"` is simply never wired —
    `resolve("voyage")` degrades to `"cloud"` (T4.2)."""
    fresh_settings(anthropic=None, openai=None, environment="test")
    from unittest.mock import patch

    from agentive_backend.app.lifespan import _build_embedding_router
    from agentive_backend.shared.llm.testing import MockEmbedder

    with patch("agentive_backend.infra.llm.fastembed_adapter.TextEmbedding"):
        router = _build_embedding_router()

    embedder, model = router.resolve("voyage")
    assert isinstance(embedder, MockEmbedder)
    assert model == "text-embedding-3-small"
