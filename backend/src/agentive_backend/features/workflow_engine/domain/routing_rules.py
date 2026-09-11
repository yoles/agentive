"""Declarative routing rules — pure domain core (Story 4.3 AC1, T2).

Framework-free (dataclasses only, no Pydantic, no YAML, no I/O) — mirrors the
posture of :mod:`.value_objects`/:mod:`.condition_dsl`. The catalog loader
(``features/workflow_engine/routing_catalog.py``, T3) parses YAML into these
types; this module never reads a file.

Evaluation order (implemented one layer up, in ``engine/hybrid_router.py``
T5): the branching-condition DSL (4.1/4.2) decides first — if it produces at
least one target, this module is never consulted. Only when the DSL is
silent does :func:`evaluate_rules` run, and only when its best score stays
below the configured threshold does the engine escalate to an LLM. This
module knows nothing about that ordering or about the LLM path — it only
scores a fixed set of rules against a :class:`RoutingContext`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from agentive_backend.features.workflow_engine.domain.condition_dsl import (
    ParsedCondition,
    evaluate,
)

RuleVerdict = Literal["terminate", "fan_out_all"]


#: Why an escalation produced no decision. A CLOSED set — it is used as a
#: Prometheus label value (``ROUTING_ESCALATION_FAILURES_TOTAL``), so it must
#: stay bounded, like every label in this feature's metrics module.
EscalationFailureReason = Literal["llm_error", "unparsable", "target_rejected"]


class RoutingEscalationError(RuntimeError):
    """LLM escalation could not produce a usable decision (Story 4.3 AC2).

    A run-time execution failure, not a domain-validation error — deliberately
    NOT a :class:`~agentive_backend.features.workflow_engine.domain.value_objects.DomainValidationError`
    subclass. The service layer lets it propagate exactly like an
    :class:`~agentive_backend.shared.llm.exceptions.LLMError` raised by
    ``agent_node`` — uncaught here, the run transitions to ``status="error"``
    (Dev Notes § Dégradation vs échec explicite: a truncated run that reports
    ``completed`` is worse than one that fails loudly).

    ``failure_reason`` classifies the failure for
    ``ROUTING_ESCALATION_FAILURES_TOTAL`` (review IG2) — carried on the
    exception because the raise site is the only scope that knows which of
    the two it was, and the metric is recorded one frame up.
    """

    def __init__(self, message: str, *, failure_reason: EscalationFailureReason) -> None:
        super().__init__(message)
        self.failure_reason: EscalationFailureReason = failure_reason


@dataclass(frozen=True, slots=True)
class RoutingContext:
    """Structural facts about one routing decision point (Story 4.3 T2.2).

    Populated by the engine from the DAG's static shape (candidate/edge
    counts) plus the emitting node's own runtime output — never from
    upstream nodes' outputs, which stay out of rule predicates entirely.
    """

    node_id: str
    candidate_count: int
    conditional_count: int
    unconditional_count: int
    #: The node produced STRUCTURED output — a JSON object the engine could
    #: actually parse — as opposed to merely "produced something". The
    #: distinction is the whole point: ``execute_agent_node`` never returns
    #: ``None``, it falls back to a one-key ``{"_raw": <text>}`` envelope, so
    #: a flag meaning "output is not None" would be permanently ``True`` and
    #: any rule keyed on its negation permanently dead (code review BS1).
    #: Named ``has_parsable_output`` and not ``has_output`` for exactly that
    #: reason — the previous name is what made the wrong reading look right.
    has_parsable_output: bool
    output_field_count: int
    own_output: Mapping[str, Any] | None

    def resolve(self, namespace: str, field: str) -> Any:
        """Look up ``<namespace>.<field>`` for a rule predicate.

        ``output`` reads ``own_output`` (``None`` if it isn't a mapping or
        the field is absent — degrading exactly like
        :func:`condition_dsl.evaluate` does for a missing routing variable,
        never raising). ``context`` reads this object's own scalar
        attributes by name. Any other namespace, or an unknown ``context``
        field, resolves to ``None`` — the same "predicate does not hold"
        degradation, not an exception.
        """
        if namespace == "output":
            if isinstance(self.own_output, Mapping):
                return self.own_output.get(field)
            return None
        if namespace == "context":
            return getattr(self, field, None) if field in CONTEXT_FIELDS else None
        return None


#: The only field names a ``context.<field>`` predicate may name. PUBLIC
#: because the catalog loader validates against it (T3.4's "fail at load,
#: never at decision"): an unknown field resolves to ``None`` here, so a
#: typo like ``context.candidat_count`` would otherwise load cleanly, never
#: hold, and silently send every decision it was meant to catch to the LLM.
#: Kept next to :class:`RoutingContext` so the two cannot drift.
CONTEXT_FIELDS: frozenset[str] = frozenset(
    {
        "node_id",
        "candidate_count",
        "conditional_count",
        "unconditional_count",
        "has_parsable_output",
        "output_field_count",
    }
)


@dataclass(frozen=True, slots=True)
class RulePenalty:
    """A confidence multiplier applied when ALL of ``when`` hold (Story 4.3 T2.4)."""

    when: tuple[ParsedCondition, ...]
    factor: float


@dataclass(frozen=True, slots=True)
class RoutingRule:
    """One declarative rule from the catalog (Story 4.3 T2.4).

    ``rule_id`` is unique within a catalog (enforced by the loader, T3.4),
    used both for tie-breaking (declaration order, see :func:`evaluate_rules`)
    and for tracing (:class:`RoutingDecision.rule_id`).
    """

    rule_id: str
    description: str
    when: tuple[ParsedCondition, ...]
    verdict: RuleVerdict
    base_confidence: float
    penalties: tuple[RulePenalty, ...]


def _predicate_holds(conditions: Sequence[ParsedCondition], context: RoutingContext) -> bool:
    """A list of predicates is a logical AND — empty list holds vacuously."""
    return all(
        evaluate(condition, context.resolve(condition.namespace, condition.field))
        for condition in conditions
    )


def compute_confidence(rule: RoutingRule, context: RoutingContext) -> float:
    """``base x Π(factor of each penalty whose predicates ALL hold)``, clamped
    to ``[0.0, 1.0]`` (Story 4.3 AC1, T2.5).

    Pure and deterministic — no clock, no randomness. A penalty whose
    predicates do NOT all hold contributes nothing (not even a factor of
    ``1.0`` — it is simply skipped).
    """
    score = rule.base_confidence
    for penalty in rule.penalties:
        if _predicate_holds(penalty.when, context):
            score *= penalty.factor
    if score != score:  # NaN — the ONLY value that is not equal to itself
        # Fails CLOSED. `max(0.0, min(1.0, nan))` returns 1.0: every
        # comparison against NaN is false, so `min` keeps its first argument
        # and the clamp hands back MAXIMUM confidence — a garbage rule would
        # clear any threshold below 1.0 and decide routing deterministically.
        # The loader rejects NaN in YAML (T3.3), but `RoutingRule` is a public
        # exported dataclass built directly by tests and, by design, by the
        # future rule-learning component.
        return 0.0
    return max(0.0, min(1.0, score))


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """The winning rule of one :func:`evaluate_rules` call."""

    rule_id: str
    verdict: RuleVerdict
    confidence: float


def evaluate_rules(rules: Sequence[RoutingRule], context: RoutingContext) -> RuleMatch | None:
    """Score every rule whose ``when`` predicates ALL hold, return the best
    (Story 4.3 T2.6).

    ``None`` when no rule's predicates hold — this is NOT an error case, it
    is the normal trigger for LLM escalation (Dev Notes § Dégradation
    silencieuse vs échec explicite, "aucune règle ne tient").

    Tie-break on equal score: **declaration order in the catalog** — the
    first-declared matching rule wins. Never depend on a dict's iteration
    order built elsewhere; ``rules`` is iterated exactly as given.
    """
    best: RuleMatch | None = None
    best_score = -1.0
    for rule in rules:
        if not _predicate_holds(rule.when, context):
            continue
        score = compute_confidence(rule, context)
        if score > best_score:
            best_score = score
            best = RuleMatch(rule_id=rule.rule_id, verdict=rule.verdict, confidence=score)
    return best


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The outcome of one routing decision point (Story 4.3 T2.7).

    Serializable to JSON via :meth:`to_mapping` — it transits through the
    LangGraph state (``WorkflowState.routing_decisions``), hence through the
    checkpointer, so no non-JSON-native value may ever be stored here.
    """

    node_id: str
    mode: Literal["deterministic", "llm_escalated"]
    source: Literal["dsl", "rules", "llm"]
    targets: tuple[str, ...]
    #: ``None`` when the escalation path ran with NO rule matching at all —
    #: distinct from ``0.0``, which means a rule matched and scored zero.
    confidence: float | None
    rule_id: str | None
    reason: str
    llm_model: str | None
    llm_latency_ms: int | None
    #: What the escalation COST. ``None`` on both deterministic paths, which
    #: spend nothing. Carried on the decision rather than folded into
    #: ``node_metrics`` because the run's total must include it (it was
    #: silently missing from ``total_cost_usd``) AND it must stay separable:
    #: the whole claim of this feature is that routing deterministically is
    #: cheaper, which is unfalsifiable if routing spend is indistinguishable
    #: from node spend.
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None
    #: ``str``, never ``Decimal`` — this mapping transits the LangGraph state
    #: and lands in JSONB, whose serializer raises on a raw ``Decimal``
    #: (same conversion ``agent_node`` already applies to ``cost_usd``).
    llm_cost_usd: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "mode": self.mode,
            "source": self.source,
            "targets": list(self.targets),
            "confidence": self.confidence,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "llm_model": self.llm_model,
            "llm_latency_ms": self.llm_latency_ms,
            "llm_input_tokens": self.llm_input_tokens,
            "llm_output_tokens": self.llm_output_tokens,
            "llm_cost_usd": self.llm_cost_usd,
        }


__all__ = [
    "CONTEXT_FIELDS",
    "RoutingContext",
    "RoutingDecision",
    "RoutingEscalationError",
    "RoutingRule",
    "RuleMatch",
    "RulePenalty",
    "RuleVerdict",
    "compute_confidence",
    "evaluate_rules",
]
