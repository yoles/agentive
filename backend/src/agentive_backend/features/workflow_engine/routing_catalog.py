"""Routing rules catalog loader — YAML → :class:`RoutingRule` (Story 4.3 T3).

Mirror structural of ``features/agent_registry/archetypes.py``: ``yaml.safe_load``
only, Pydantic ``extra="forbid"`` validation, immutable result (a ``tuple``
here — the ``MappingProxyType`` equivalent for an ORDER-SENSITIVE sequence,
since ``evaluate_rules`` tie-breaks on declaration order), ``RuntimeError`` on
any load/validation failure so a broken catalog fails BOOT, not a request.

Lives at the feature root, NOT in ``domain/`` — it imports Pydantic and
``yaml``, and ``domain/`` is framework-free by this package's own convention
(see ``domain/__init__.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from agentive_backend.features.workflow_engine.domain.condition_dsl import ParsedCondition, parse
from agentive_backend.features.workflow_engine.domain.routing_rules import (
    CONTEXT_FIELDS,
    RoutingRule,
    RulePenalty,
    RuleVerdict,
)
from agentive_backend.features.workflow_engine.domain.value_objects import DomainValidationError

DEFAULT_CATALOG_PATH: Final[Path] = Path(__file__).parent / "templates" / "routing-rules.yaml"

_ALLOWED_NAMESPACES: Final[tuple[str, ...]] = ("output", "context")


class _PenaltyModel(BaseModel):
    """One YAML penalty entry — validated before conversion to :class:`RulePenalty`."""

    model_config = ConfigDict(extra="forbid")

    when: list[str] = Field(min_length=1)
    # `allow_inf_nan=False` (Pydantic v2 default is `True`) — without it,
    # `factor: .nan` passes `gt=0.0, le=1.0` (every comparison with NaN is
    # False, so BOTH bounds silently pass) and contaminates the whole
    # scoring chain in silence (T3.3).
    factor: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)


class _RuleModel(BaseModel):
    """One YAML rule entry — validated before conversion to :class:`RoutingRule`."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)
    when: list[str] = Field(min_length=1)
    verdict: RuleVerdict
    base_confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    penalties: list[_PenaltyModel] = Field(default_factory=list)


class _RuleFile(BaseModel):
    """Top-level YAML wrapper — only allows ``archetype_version``/``rules``."""

    model_config = ConfigDict(extra="forbid")

    archetype_version: int = Field(ge=1)
    rules: list[_RuleModel] = Field(min_length=1)


def _parse_predicate(condition: str) -> ParsedCondition:
    """Parse ONE predicate at load time, field name included.

    Parsing once at load rather than per decision mirrors `_make_router`'s own
    justification (conditions are pre-parsed at graph-build time). The extra
    ``CONTEXT_FIELDS`` check is what makes that promise real for the SECOND
    namespace: `parse` validates the namespace, but a ``context.<field>``
    naming a field :class:`RoutingContext` does not carry resolves to ``None``
    at decision time, so the predicate never holds. The rule would load
    cleanly at boot, fire never, and silently escalate to the LLM every
    decision it was written to catch — a failure with no error, no log and no
    metric. Caught here, it is a boot failure naming the typo.
    """
    parsed = parse(condition, allowed_namespaces=_ALLOWED_NAMESPACES)
    if parsed.namespace == "context" and parsed.field not in CONTEXT_FIELDS:
        raise DomainValidationError(
            f"unknown context field {parsed.field!r} in predicate {condition!r} — "
            f"known fields: {sorted(CONTEXT_FIELDS)}"
        )
    return parsed


def _convert_rule(model: _RuleModel) -> RoutingRule:
    """Parse every predicate ONCE at load time (mirror `_make_router`'s own
    justification: conditions are pre-parsed at graph-build time, never
    re-parsed per decision)."""
    when = tuple(_parse_predicate(cond) for cond in model.when)
    penalties = tuple(
        RulePenalty(
            when=tuple(_parse_predicate(cond) for cond in penalty.when),
            factor=penalty.factor,
        )
        for penalty in model.penalties
    )
    return RoutingRule(
        rule_id=model.rule_id,
        description=model.description,
        when=when,
        verdict=model.verdict,
        base_confidence=model.base_confidence,
        penalties=penalties,
    )


def load_routing_rules(path: Path | None = None) -> tuple[RoutingRule, ...]:
    """Load and validate the routing rules catalog from YAML (Story 4.3 T3.4).

    Args:
        path: Override the default path (used by tests). Defaults to
            ``features/workflow_engine/templates/routing-rules.yaml``.

    Returns:
        An immutable, ORDER-PRESERVING sequence of :class:`RoutingRule` —
        declaration order is load-bearing (``evaluate_rules`` tie-break).

    Raises:
        RuntimeError: the file is missing/unreadable, malformed YAML, fails
            Pydantic validation (unknown field, out-of-range confidence,
            NaN, …), contains a duplicate ``rule_id``, or a predicate fails
            the branching-condition DSL grammar.
    """
    resolved_path = path or DEFAULT_CATALOG_PATH

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"routing rules catalog init failed: cannot read {resolved_path}: {exc}"
        ) from exc

    try:
        raw_data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"routing rules catalog init failed: YAML parse error in {resolved_path}: {exc}"
        ) from exc

    if not isinstance(raw_data, dict):
        raise RuntimeError(
            "routing rules catalog init failed: expected mapping at top level of "
            f"{resolved_path}, got {type(raw_data).__name__}"
        )

    try:
        parsed = _RuleFile.model_validate(raw_data)
    except PydanticValidationError as exc:
        raise RuntimeError(
            f"routing rules catalog init failed: schema validation error in {resolved_path}: {exc}"
        ) from exc

    seen_ids: set[str] = set()
    rules: list[RoutingRule] = []
    for model in parsed.rules:
        if model.rule_id in seen_ids:
            raise RuntimeError(
                f"routing rules catalog init failed: duplicate rule_id "
                f"'{model.rule_id}' in {resolved_path}"
            )
        seen_ids.add(model.rule_id)
        try:
            rules.append(_convert_rule(model))
        except DomainValidationError as exc:
            raise RuntimeError(
                f"routing rules catalog init failed: invalid predicate in rule "
                f"'{model.rule_id}' ({resolved_path}): {exc}"
            ) from exc

    return tuple(rules)


__all__ = ["DEFAULT_CATALOG_PATH", "load_routing_rules"]
