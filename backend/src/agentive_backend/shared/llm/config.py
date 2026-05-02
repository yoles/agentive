"""Per-agent LLM config (consumed by Story 2.2 / M8 Configurator).

Stored inside ``agent_templates.config`` JSONB column — schema validated
on read so a malformed config fails fast instead of leaking ``None`` into
provider calls.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentLLMConfig(BaseModel):
    """LLM configuration attached to an agent template / instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_chain: list[str] = Field(..., min_length=1, max_length=4)
    model: str = Field(..., min_length=1)
    max_tokens: int = Field(..., ge=1, le=200_000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    system: str | None = None
    stop: list[str] | None = None
    timeout_s: float = Field(default=30.0, ge=1.0, le=600.0)

    @field_validator("provider_chain")
    @classmethod
    def _no_duplicate_providers(cls, value: list[str]) -> list[str]:
        """Reject duplicate provider names in the chain.

        Same-provider retry is the responsibility of Story 9.5 (back-off +
        jitter), not of fallback routing. A duplicate here almost always
        signals a config typo where the user meant two distinct providers.
        """
        if len(value) != len(set(value)):
            raise ValueError(f"provider_chain must contain unique provider names; got {value!r}")
        return value
