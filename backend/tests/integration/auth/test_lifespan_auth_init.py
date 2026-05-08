"""Integration tests for ``_init_auth_token`` — Story 1.7 (AC5)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from agentive_backend.app.lifespan import _init_auth_token


@pytest.fixture
def fake_app() -> FastAPI:
    return FastAPI()


def test_plaintext_token_stored_as_is(fake_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plaintext AGENTIVE_API_TOKEN → stored verbatim, mode=plaintext."""
    from agentive_backend.app import lifespan as lifespan_module
    from agentive_backend.shared import config as _config

    new_settings = _config.Settings.model_construct(
        **{
            **_config.settings.model_dump(),
            "agentive_api_token": __import__("pydantic", fromlist=["SecretStr"]).SecretStr(
                "change_me"
            ),
            "environment": "development",
        }
    )
    monkeypatch.setattr(lifespan_module, "settings", new_settings)
    _init_auth_token(fake_app)
    assert fake_app.state.auth_token_hash == "change_me"


def test_bcrypt_hash_stored_as_is(fake_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """A $2b$ prefixed value → stored verbatim, mode=bcrypt."""
    import bcrypt
    from pydantic import SecretStr

    from agentive_backend.app import lifespan as lifespan_module
    from agentive_backend.shared import config as _config

    raw = "supersecrettoken"
    bcrypt_hash = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(4)).decode()  # rounds=4 for speed
    assert bcrypt_hash.startswith("$2b$")

    new_settings = _config.Settings.model_construct(
        **{
            **_config.settings.model_dump(),
            "agentive_api_token": SecretStr(bcrypt_hash),
            "environment": "development",
        }
    )
    monkeypatch.setattr(lifespan_module, "settings", new_settings)
    _init_auth_token(fake_app)
    assert fake_app.state.auth_token_hash == bcrypt_hash


def test_production_plaintext_raises(fake_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plaintext token in production → RuntimeError (fail-fast)."""
    from pydantic import SecretStr

    from agentive_backend.app import lifespan as lifespan_module
    from agentive_backend.shared import config as _config

    new_settings = _config.Settings.model_construct(
        **{
            **_config.settings.model_dump(),
            "agentive_api_token": SecretStr("plaintext-token"),
            "environment": "production",
        }
    )
    monkeypatch.setattr(lifespan_module, "settings", new_settings)

    with pytest.raises(RuntimeError, match="bcrypt hash in production"):
        _init_auth_token(fake_app)


# ─── P4 — Empty AGENTIVE_API_TOKEN must always fail-fast ─────────────────────


def test_empty_token_raises_in_dev(fake_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty token raises in dev — never silently accepted."""
    from pydantic import SecretStr

    from agentive_backend.app import lifespan as lifespan_module
    from agentive_backend.shared import config as _config

    new_settings = _config.Settings.model_construct(
        **{
            **_config.settings.model_dump(),
            "agentive_api_token": SecretStr(""),
            "environment": "development",
        }
    )
    monkeypatch.setattr(lifespan_module, "settings", new_settings)

    with pytest.raises(RuntimeError, match="AGENTIVE_API_TOKEN is empty"):
        _init_auth_token(fake_app)


# ─── P3 — $2y$ / $2a$ bcrypt prefixes must be detected as bcrypt mode ────────


def test_bcrypt_2y_prefix_accepted(fake_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """A $2y$-prefixed hash (PHP-style) must be treated as bcrypt, not plaintext."""
    import bcrypt
    from pydantic import SecretStr

    from agentive_backend.app import lifespan as lifespan_module
    from agentive_backend.shared import config as _config

    raw = "supersecrettoken"
    h = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(4)).decode()
    h_2y = "$2y$" + h[len("$2b$") :]

    new_settings = _config.Settings.model_construct(
        **{
            **_config.settings.model_dump(),
            "agentive_api_token": SecretStr(h_2y),
            "environment": "production",  # prod-strict mode
        }
    )
    monkeypatch.setattr(lifespan_module, "settings", new_settings)
    # Must NOT raise — $2y$ is valid bcrypt.
    _init_auth_token(fake_app)
    assert fake_app.state.auth_token_hash == h_2y


# ─── P3 — Malformed $2b$ hash must fail-fast at boot, not at request time ────


def test_malformed_bcrypt_hash_raises(fake_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated $2b$ string must crash the boot with a clear message."""
    from pydantic import SecretStr

    from agentive_backend.app import lifespan as lifespan_module
    from agentive_backend.shared import config as _config

    # Looks like bcrypt by prefix, but truncated → bcrypt.checkpw raises.
    new_settings = _config.Settings.model_construct(
        **{
            **_config.settings.model_dump(),
            "agentive_api_token": SecretStr("$2b$12$truncated"),
            "environment": "development",
        }
    )
    monkeypatch.setattr(lifespan_module, "settings", new_settings)

    with pytest.raises(RuntimeError, match="malformed"):
        _init_auth_token(fake_app)
