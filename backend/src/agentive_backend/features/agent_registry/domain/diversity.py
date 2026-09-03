"""Contrôleur/Producteur LLM diversity check (Story 2.8, FR15).

Framework-free (dataclass only, no Pydantic/SQLAlchemy), same rationale as
:mod:`.value_objects` : testable in isolation without a DB, cf ADR
``docs/decisions/agent-registry-domain-core.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentive_backend.features.agent_registry.domain.value_objects import AgentConfig

INCOMPLETE_CONFIG_REASON = "controller or producer llm configuration incomplete"
SAME_CONFIG_REASON = "same llm_model and same llm_params"
DIVERSE_REASON = "different llm_model or llm_params"


@dataclass(frozen=True, slots=True)
class DiversityCheckResult:
    """Outcome of :func:`check_llm_diversity`.

    ``is_diverse`` is ``bool | None`` (not just a ``bool``) : an incomplete
    LLM config (``llm_model``/``llm_params`` not yet set, Story 2.2 not yet
    applied) must not produce a false-positive ``True`` (falsely reassuring)
    nor a false-negative ``False`` (falsely blocking), cf Dev Note #8.
    """

    is_diverse: bool | None
    reason: str


def check_llm_diversity(controller: AgentConfig, producer: AgentConfig) -> DiversityCheckResult:
    """Compare two agent-template LLM configs for diversity (FR15).

    ``is_diverse=False`` iff both ``llm_model`` AND ``llm_params`` (strict
    ``LLMParams`` equality on temperature AND max_tokens, Dev Note #7) are
    identical between controller and producer. ``is_diverse=None`` if either
    template's LLM config is incomplete.
    """
    if (
        controller.llm_model is None
        or controller.llm_params is None
        or producer.llm_model is None
        or producer.llm_params is None
    ):
        return DiversityCheckResult(is_diverse=None, reason=INCOMPLETE_CONFIG_REASON)

    same_config = (
        controller.llm_model == producer.llm_model and controller.llm_params == producer.llm_params
    )
    if same_config:
        return DiversityCheckResult(is_diverse=False, reason=SAME_CONFIG_REASON)
    return DiversityCheckResult(is_diverse=True, reason=DIVERSE_REASON)


__all__ = ["DiversityCheckResult", "check_llm_diversity"]
