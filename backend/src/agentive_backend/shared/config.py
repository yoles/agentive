"""Centralized configuration via Pydantic Settings.

CRITICAL: This is the ONLY module allowed to read from os.environ / .env.
All other modules must import `settings` from here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel strings used in `.env.example` — any match forbids production use.
_DEV_DEFAULT_SENTINELS: tuple[str, ...] = (
    "change_me_dev_token",
    "change_me_fernet_key",
    "change_me_app_dev",
    "change_me_owner_dev",
    "change_me_audit_dev",
)


class Settings(BaseSettings):
    """Application configuration — loaded from environment variables.

    See `.env.example` at the repo root for documented variables.

    Production safety: the model validator below refuses to boot with any
    ``change_me_*`` placeholder if ``NODE_ENV == "production"`` to fail fast
    instead of silently running with dev credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Runtime ───
    environment: Literal["development", "production", "test"] = Field(
        default="development", alias="NODE_ENV"
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    # ─── Auth (Story 1.7) ───
    agentive_api_token: SecretStr = Field(
        default=SecretStr("change_me_dev_token"), alias="AGENTIVE_API_TOKEN"
    )

    # ─── Encryption (Story 9.2) ───
    # Fernet requires 32 url-safe base64 bytes. The sentinel default is intentionally
    # invalid so `Fernet(key)` fails fast if it is ever instantiated with the default.
    agentive_encryption_key: SecretStr = Field(
        default=SecretStr("change_me_fernet_key"), alias="AGENTIVE_ENCRYPTION_KEY"
    )

    # ─── Database ───
    database_url: PostgresDsn = Field(
        default=PostgresDsn(
            "postgresql+psycopg://agentive_app:change_me_app_dev@localhost:5432/agentive"
        ),
        alias="DATABASE_URL",
    )
    database_url_owner: PostgresDsn = Field(
        default=PostgresDsn(
            "postgresql+psycopg://agentive_owner:change_me_owner_dev@localhost:5432/agentive"
        ),
        alias="DATABASE_URL_OWNER",
    )

    # ─── LLM Providers (Stories 1.6 + 9.3) ───
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    voyage_api_key: SecretStr | None = Field(default=None, alias="VOYAGE_API_KEY")

    # ─── CORS ───
    # JSON-parsed from env (e.g. `AGENTIVE_CORS_ALLOW_ORIGINS='["https://app.example.com"]'`).
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "https://localhost:8443"],
        alias="AGENTIVE_CORS_ALLOW_ORIGINS",
    )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Validators
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @model_validator(mode="after")
    def _reject_dev_defaults_in_production(self) -> "Settings":
        """Fail-fast if a placeholder value is detected while ``NODE_ENV=production``.

        This prevents a misconfigured prod deploy from silently booting with
        known-bad dev credentials / tokens / encryption keys.

        In development we only emit a warning (via stderr, since structlog may
        not be configured yet) to alert the developer without blocking hot-reload.
        """
        violations: list[str] = []
        secret_fields = {
            "AGENTIVE_API_TOKEN": self.agentive_api_token.get_secret_value(),
            "AGENTIVE_ENCRYPTION_KEY": self.agentive_encryption_key.get_secret_value(),
            "DATABASE_URL": str(self.database_url),
            "DATABASE_URL_OWNER": str(self.database_url_owner),
        }
        for name, value in secret_fields.items():
            if any(sentinel in value for sentinel in _DEV_DEFAULT_SENTINELS):
                violations.append(name)

        if not violations:
            return self

        if self.environment == "production":
            raise ValueError(
                "Refusing to start in production with dev placeholder values for: "
                f"{', '.join(violations)}. Set the corresponding env vars to real secrets."
            )

        # Development / test : warn loudly but do not block.
        # Using sys.stderr directly because structlog may not be configured yet
        # (Settings() is instantiated at module import time).
        import sys
        print(
            "⚠️  agentive-backend config WARNING: dev placeholder values detected for "
            f"{', '.join(violations)}. "
            "The default AGENTIVE_API_TOKEN is PUBLIC (committed in .env.example) — "
            "anyone can authenticate. Override via environment variables for any "
            "non-throwaway deployment.",
            file=sys.stderr,
        )
        return self

    @model_validator(mode="after")
    def _reject_invalid_fernet_key_in_production(self) -> "Settings":
        """Validate the Fernet encryption key format in production.

        In dev we tolerate the sentinel default (so `uv run` doesn't break),
        but in prod the key must be a valid Fernet key — otherwise the first
        call to `Fernet(key)` crashes a potentially business-critical code path.
        """
        if self.environment != "production":
            return self

        key = self.agentive_encryption_key.get_secret_value()
        try:
            Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, InvalidToken) as exc:
            raise ValueError(
                "AGENTIVE_ENCRYPTION_KEY is not a valid Fernet key "
                "(expected 32 url-safe base64 bytes). "
                "Generate one with: `python -c 'from cryptography.fernet import Fernet;"
                " print(Fernet.generate_key().decode())'`."
            ) from exc
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return singleton Settings instance (cached)."""
    return Settings()


settings = get_settings()
