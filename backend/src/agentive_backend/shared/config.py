"""Centralized configuration via Pydantic Settings.

CRITICAL: This is the ONLY module allowed to read from os.environ / .env.
All other modules must import `settings` from here.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import quote_plus

from cryptography.fernet import Fernet, InvalidToken
from pydantic import (
    Field,
    PostgresDsn,
    SecretStr,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel strings used in `.env.example` — any match forbids production use.
_DEV_DEFAULT_SENTINELS: tuple[str, ...] = (
    "change_me",
    "change_me_dev_token",
    "change_me_fernet_key",
    "change_me_app_dev",
    "change_me_owner_dev",
    "change_me_audit_dev",
)


class Settings(BaseSettings):
    """Application configuration — loaded from environment variables.

    See `.env.example` at the repo root for documented variables.

    Production safety: model validators below refuse to boot with any
    ``change_me*`` placeholder if ``NODE_ENV == "production"`` to fail fast
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
        default=SecretStr("change_me"), alias="AGENTIVE_API_TOKEN"
    )

    # ─── Encryption (Story 9.2) ───
    agentive_encryption_key: SecretStr = Field(
        default=SecretStr("change_me"), alias="AGENTIVE_ENCRYPTION_KEY"
    )
    # Key-rotation window only (Story 9.2 AC3): set alongside a new
    # AGENTIVE_ENCRYPTION_KEY so `shared.security.crypto` can still decrypt
    # rows written under the outgoing key while `scripts/rotate_encryption_key.py`
    # re-encrypts them under the new one. Unset once the rotation script confirms
    # completion — never required outside of an active rotation.
    agentive_encryption_key_previous: SecretStr | None = Field(
        default=None, alias="AGENTIVE_ENCRYPTION_KEY_PREVIOUS"
    )

    # ─── PostgreSQL (components — the DSN is built app-side via `computed_field`) ───
    # We store the components rather than a full DSN so the password can be
    # URL-encoded when needed (supports `@`, `/`, `:`, `%`, etc.).
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="agentive", alias="POSTGRES_DB")
    postgres_app_password: SecretStr = Field(
        default=SecretStr("change_me"), alias="POSTGRES_APP_PASSWORD"
    )
    postgres_owner_password: SecretStr = Field(
        default=SecretStr("change_me"), alias="POSTGRES_OWNER_PASSWORD"
    )

    # ─── LLM Providers (Stories 1.6 + 9.3) ───
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    voyage_api_key: SecretStr | None = Field(default=None, alias="VOYAGE_API_KEY")

    # ─── MCP Tool Hub (Story 2.5 — P-23 admin-gate) ───
    # Opt-in flag: `POST /tools/servers` accepts user-supplied `command` (stdio
    # subprocess) or `url` (SSE) without sandbox or URL filtering. This is RCE
    # and SSRF surface by design — sandbox bwrap arrives Story 2.6 (D59), URL
    # allowlist Story 4.x (D60). Flip to `true` only in dev/test or after
    # Story 2.6. Default `false` = the endpoint returns 403.
    mcp_allow_registration: bool = Field(default=False, alias="AGENTIVE_ALLOW_MCP_REGISTRATION")

    # ─── Workflow Engine — hybrid routing (Story 4.3) ───
    # Deployment-level tuning for `engine/hybrid_router.py`'s DSL → rules →
    # LLM escalation sequence. Lives in `Settings`, not the rules YAML
    # catalog (T1.3) — two sources of truth for the same setting is the
    # motif already corrected three times in this repo (D84, `config.llm`
    # fantôme de 3.5, `embedding_backend`).
    # `allow_inf_nan=False` is NOT decorative on these two floats: `ge`/`le`/`gt`
    # do not reject NaN (every comparison against NaN is false, so no bound
    # ever trips). A `NaN` threshold would boot cleanly and then make
    # `match.confidence >= threshold` false forever — every decision escalating
    # to the LLM, silently, with no error to trace. Same guard as
    # `routing_catalog._RuleModel` applies to the catalog's own floats (T3.3).
    routing_confidence_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        alias="AGENTIVE_ROUTING_CONFIDENCE_THRESHOLD",
    )
    routing_escalation_model: str = Field(
        default="claude-haiku-4-5", min_length=1, alias="AGENTIVE_ROUTING_ESCALATION_MODEL"
    )
    routing_escalation_timeout_s: float = Field(
        default=15.0, gt=0.0, allow_inf_nan=False, alias="AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S"
    )
    routing_escalation_max_tokens: int = Field(
        default=256, ge=1, le=4096, alias="AGENTIVE_ROUTING_ESCALATION_MAX_TOKENS"
    )

    # ─── Workflow Engine — Dry Run predictif (Story 4.4) ───
    # Deployment-level tuning for `dry_run.py`'s estimation. Same posture as
    # the `AGENTIVE_ROUTING_*` block above: `Settings` is the single source
    # of truth, never a `config.yaml`-style secondary source (D84 lesson,
    # applied again).
    dry_run_history_limit: int = Field(
        default=20, ge=1, le=100, alias="AGENTIVE_DRY_RUN_HISTORY_LIMIT"
    )
    # `le` is not decorative: these two feed `Decimal(tokens) / 1_000_000 *
    # price`, then `.quantize(Decimal("0.000001"))`. Past ~1e22 tokens that
    # quantize exceeds the default decimal context's 28-digit precision and
    # raises `InvalidOperation` — a 500 on every priced Dry Run, caused by an
    # env var. A ceiling two orders of magnitude above the largest real
    # context window keeps the arithmetic in range while still allowing any
    # plausible tuning (review fix P1/P2).
    dry_run_fallback_input_tokens: int = Field(
        default=500, ge=1, le=100_000_000, alias="AGENTIVE_DRY_RUN_FALLBACK_INPUT_TOKENS"
    )
    dry_run_fallback_output_tokens: int = Field(
        default=500, ge=1, le=100_000_000, alias="AGENTIVE_DRY_RUN_FALLBACK_OUTPUT_TOKENS"
    )
    # `None` (default) disables the check entirely — a Sprint-1 safety net,
    # not Story 9.4's real per-department/per-workflow budget caps (see
    # Story 4.4 Dev Notes § Budget cap).
    #
    # Three guards, each earned by a review finding (P1):
    # * `_blank_budget_cap_to_none` below — `AGENTIVE_DRY_RUN_BUDGET_CAP_USD=`
    #   (the natural way to "turn it off" in a copied `.env`) parsed as `""`,
    #   which is neither a valid `Decimal` nor `None`. `Settings` then failed
    #   to instantiate at import time, taking the WHOLE backend down over an
    #   optional knob. `.env.example` documented "vide = désactivé"; now it
    #   is true.
    # * `ge=0` — a negative cap is below every possible estimate, so
    #   `budget_cap_exceeded` fires on every workflow forever: the risk
    #   becomes permanent noise and stops being a signal.
    # * `allow_inf_nan=False` — same reasoning as
    #   `routing_confidence_threshold` above, but worse here: `ge` does not
    #   reject NaN, and unlike a float comparison, `Decimal('1') >
    #   Decimal('NaN')` RAISES `InvalidOperation` rather than returning
    #   False. A NaN cap booted cleanly and 500'd every priced Dry Run.
    dry_run_budget_cap_usd: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = Field(
        default=None, alias="AGENTIVE_DRY_RUN_BUDGET_CAP_USD"
    )

    @field_validator("dry_run_budget_cap_usd", mode="before")
    @classmethod
    def _blank_budget_cap_to_none(cls, value: object) -> object:
        """An empty/whitespace env var means "unset", not "invalid decimal"."""
        return None if isinstance(value, str) and not value.strip() else value

    # ─── CORS ───
    # JSON-parsed from env (e.g. `AGENTIVE_CORS_ALLOW_ORIGINS='["https://app.example.com"]'`).
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "https://localhost:8443"],
        alias="AGENTIVE_CORS_ALLOW_ORIGINS",
    )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Computed DSNs — URL-encoded to support passwords with `@`, `/`, `:`, `%`.
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> PostgresDsn:
        """Runtime DSN for agentive_app (RLS-scoped)."""
        encoded = quote_plus(self.postgres_app_password.get_secret_value())
        return PostgresDsn(
            f"postgresql+psycopg://agentive_app:{encoded}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_owner(self) -> PostgresDsn:
        """DSN for Alembic migrations (agentive_owner role)."""
        encoded = quote_plus(self.postgres_owner_password.get_secret_value())
        return PostgresDsn(
            f"postgresql+psycopg://agentive_owner:{encoded}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def psycopg_dsn(self) -> str:
        """:attr:`database_url` as a plain ``postgresql://`` DSN.

        For the consumers that speak raw psycopg rather than SQLAlchemy —
        ``LISTEN/NOTIFY`` in the outbox publisher, and LangGraph's
        ``PostgresSaver``/``AsyncPostgresSaver`` (Story 4.2). See
        :func:`to_psycopg_dsn`.
        """
        return to_psycopg_dsn(str(self.database_url))

    @property
    def psycopg_dsn_owner(self) -> str:
        """:attr:`database_url_owner` as a plain ``postgresql://`` DSN —
        the ``agentive_owner`` counterpart of :attr:`psycopg_dsn`, used by
        migrations that must issue DDL."""
        return to_psycopg_dsn(str(self.database_url_owner))

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
    def _reject_dev_defaults_in_production(self) -> Settings:
        """Fail-fast on `change_me*` placeholder in production; warn in dev."""
        violations: list[str] = []
        secret_fields = {
            "AGENTIVE_API_TOKEN": self.agentive_api_token.get_secret_value(),
            "AGENTIVE_ENCRYPTION_KEY": self.agentive_encryption_key.get_secret_value(),
            "POSTGRES_APP_PASSWORD": self.postgres_app_password.get_secret_value(),
            "POSTGRES_OWNER_PASSWORD": self.postgres_owner_password.get_secret_value(),
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
    def _reject_invalid_fernet_key_in_production(self) -> Settings:
        """Validate the Fernet encryption key format(s) in production.

        Validates ``AGENTIVE_ENCRYPTION_KEY`` always, and
        ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS`` too when set (Story 9.2 AC3
        rotation window) — a malformed previous key would silently make
        rotation's decrypt-fallback a no-op instead of failing fast.
        """
        if self.environment != "production":
            return self

        keys = {"AGENTIVE_ENCRYPTION_KEY": self.agentive_encryption_key}
        if self.agentive_encryption_key_previous is not None:
            keys["AGENTIVE_ENCRYPTION_KEY_PREVIOUS"] = self.agentive_encryption_key_previous

        for env_name, secret in keys.items():
            key = secret.get_secret_value()
            try:
                Fernet(key.encode() if isinstance(key, str) else key)
            except (ValueError, InvalidToken) as exc:
                raise ValueError(
                    f"{env_name} is not a valid Fernet key "
                    "(expected 32 url-safe base64 bytes). "
                    "Generate one with: `python -c 'from cryptography.fernet import Fernet;"
                    " print(Fernet.generate_key().decode())'`."
                ) from exc
        return self


# SQLAlchemy dialect markers that a raw psycopg / LangGraph consumer must not
# see. `postgresql://` is listed so an already-converted DSN round-trips.
_SQLALCHEMY_DIALECT_PREFIXES = (
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgresql://",
)


def to_psycopg_dsn(url: str) -> str:
    """Strip SQLAlchemy's dialect marker so raw psycopg / LangGraph can connect.

    Single home for a conversion that was open-coded in five places (app
    lifespan, outbox publisher, the T1.2 checkpointer migration, and two test
    fixtures). Each copy was a bare ``.replace(...)``, which is a SILENT no-op
    on any unexpected scheme: a mistyped or future DSN would sail through
    unconverted and only surface as a connection error somewhere far away.
    This raises instead.

    Raises:
        ValueError: ``url`` does not carry a recognised PostgreSQL scheme.
    """
    for prefix in _SQLALCHEMY_DIALECT_PREFIXES:
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    raise ValueError(
        f"not a recognised PostgreSQL DSN: {url.split('://', 1)[0]!r} "
        f"(expected one of {_SQLALCHEMY_DIALECT_PREFIXES})"
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return singleton Settings instance (cached)."""
    return Settings()


settings = get_settings()
