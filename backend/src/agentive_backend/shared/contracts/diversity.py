"""Contrôleur/Producteur LLM diversity check (FR15) — promoted from
``features.agent_registry.domain.diversity`` for Story 4.1 (D84 point 2).

``features.workflow_engine`` cannot import ``features.agent_registry``
(``.import-linter`` Contract 1, ``independence``), so this shared, pure
comparison logic moves here — the one piece both features need. Framework-
free (dataclass only, no Pydantic/SQLAlchemy), same rationale as the
original module: testable in isolation without a DB.

``LLMSelection`` replaces the full ``agent_registry`` ``AgentConfig`` as the
input type: only ``model``/``temperature``/``max_tokens`` matter for this
comparison, and the richer VO must stay private to ``agent_registry``.
"""

from __future__ import annotations

from dataclasses import dataclass

INCOMPLETE_CONFIG_REASON = "controller or producer llm configuration incomplete"
SAME_CONFIG_REASON = "same llm_model and same llm_params"
DIVERSE_REASON = "different llm_model or llm_params"


@dataclass(frozen=True, slots=True)
class LLMSelection:
    """The three LLM knobs ``check_llm_diversity`` compares — nothing more."""

    model: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True, slots=True)
class DiversityCheckResult:
    """Outcome of :func:`check_llm_diversity`.

    ``is_diverse`` is ``bool | None`` (not just a ``bool``) : an incomplete
    LLM config (either side not yet configured) must not produce a
    false-positive ``True`` (falsely reassuring) nor a false-negative
    ``False`` (falsely blocking).
    """

    is_diverse: bool | None
    reason: str


def check_llm_diversity(
    controller: LLMSelection | None, producer: LLMSelection | None
) -> DiversityCheckResult:
    """Compare two LLM selections for diversity (FR15).

    ``is_diverse=False`` iff both ``model`` AND ``temperature`` AND
    ``max_tokens`` are identical between controller and producer.
    ``is_diverse=None`` if either side is ``None`` (LLM config incomplete).
    """
    if controller is None or producer is None:
        return DiversityCheckResult(is_diverse=None, reason=INCOMPLETE_CONFIG_REASON)

    same_config = (
        controller.model == producer.model
        and controller.temperature == producer.temperature
        and controller.max_tokens == producer.max_tokens
    )
    if same_config:
        return DiversityCheckResult(is_diverse=False, reason=SAME_CONFIG_REASON)
    return DiversityCheckResult(is_diverse=True, reason=DIVERSE_REASON)


__all__ = ["DiversityCheckResult", "LLMSelection", "check_llm_diversity"]
