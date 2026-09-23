"""Unit tests — declarative routing rules domain core (Story 4.3 T11.1)."""

from __future__ import annotations

import math

from agentive_backend.features.workflow_engine.domain.condition_dsl import parse
from agentive_backend.features.workflow_engine.domain.routing_rules import (
    RoutingContext,
    RoutingRule,
    RulePenalty,
    compute_confidence,
    evaluate_rules,
)

_NS = ("output", "context")


def _context(**overrides: object) -> RoutingContext:
    defaults: dict[str, object] = {
        "node_id": "a",
        "candidate_count": 2,
        "conditional_count": 2,
        "unconditional_count": 0,
        "has_parsable_output": True,
        "output_field_count": 1,
        "own_output": {"status": "done"},
    }
    defaults.update(overrides)
    return RoutingContext(**defaults)  # type: ignore[arg-type]


def _rule(
    rule_id: str,
    *,
    when: tuple[str, ...] = ("output.status == 'done'",),
    base_confidence: float = 0.9,
    penalties: tuple[RulePenalty, ...] = (),
    verdict: str = "terminate",
) -> RoutingRule:
    return RoutingRule(
        rule_id=rule_id,
        description=f"rule {rule_id}",
        when=tuple(parse(cond, allowed_namespaces=_NS) for cond in when),
        verdict=verdict,  # type: ignore[arg-type]
        base_confidence=base_confidence,
        penalties=penalties,
    )


def _penalty(condition: str, factor: float) -> RulePenalty:
    return RulePenalty(when=(parse(condition, allowed_namespaces=_NS),), factor=factor)


# ─── compute_confidence ──────────────────────────────────────────────────


def test_compute_confidence_base_only_when_no_penalties() -> None:
    rule = _rule("r1", base_confidence=0.9)
    assert compute_confidence(rule, _context()) == 0.9


def test_compute_confidence_applies_one_matching_penalty() -> None:
    rule = _rule(
        "r1",
        base_confidence=0.9,
        penalties=(_penalty("context.candidate_count >= 2", 0.5),),
    )
    assert compute_confidence(rule, _context(candidate_count=2)) == 0.45


def test_compute_confidence_applies_multiple_matching_penalties() -> None:
    rule = _rule(
        "r1",
        base_confidence=1.0,
        penalties=(
            _penalty("context.candidate_count >= 2", 0.5),
            _penalty("context.has_parsable_output == true", 0.5),
        ),
    )
    assert compute_confidence(rule, _context(candidate_count=2, has_parsable_output=True)) == 0.25


def test_compute_confidence_skips_penalty_whose_predicate_does_not_hold() -> None:
    rule = _rule(
        "r1",
        base_confidence=0.9,
        penalties=(_penalty("context.candidate_count >= 10", 0.1),),
    )
    assert compute_confidence(rule, _context(candidate_count=2)) == 0.9


def test_compute_confidence_penalty_with_partially_true_predicates_not_applied() -> None:
    """A penalty's `when` is a logical AND — only ONE predicate holding is
    not enough to apply the factor."""
    rule = RoutingRule(
        rule_id="r1",
        description="d",
        when=(parse("output.status == 'done'", allowed_namespaces=_NS),),
        verdict="terminate",
        base_confidence=0.9,
        penalties=(
            RulePenalty(
                when=(
                    parse("context.candidate_count >= 2", allowed_namespaces=_NS),
                    parse("context.has_parsable_output == false", allowed_namespaces=_NS),
                ),
                factor=0.1,
            ),
        ),
    )
    # candidate_count holds, has_parsable_output==false does NOT (has_parsable_output=True) —
    # the AND fails, so the penalty must not apply.
    assert compute_confidence(rule, _context(candidate_count=2, has_parsable_output=True)) == 0.9


def test_compute_confidence_clamps_to_one() -> None:
    rule = _rule("r1", base_confidence=1.0)
    assert compute_confidence(rule, _context()) == 1.0


def test_compute_confidence_clamps_to_zero_floor() -> None:
    rule = _rule("r1", base_confidence=0.0)
    assert compute_confidence(rule, _context()) == 0.0


# ─── evaluate_rules ───────────────────────────────────────────────────────


def test_evaluate_rules_returns_none_when_no_rule_matches() -> None:
    rule = _rule("r1", when=("output.status == 'nope'",))
    assert evaluate_rules([rule], _context()) is None


def test_evaluate_rules_returns_the_highest_scoring_match() -> None:
    low = _rule("low", base_confidence=0.5)
    high = _rule("high", base_confidence=0.95)
    match = evaluate_rules([low, high], _context())
    assert match is not None
    assert match.rule_id == "high"
    assert match.confidence == 0.95


def test_evaluate_rules_tie_break_is_declaration_order() -> None:
    """Equal score — the FIRST declared rule wins, regardless of list order
    otherwise implying anything about priority."""
    first = _rule("first", base_confidence=0.9)
    second = _rule("second", base_confidence=0.9)
    match = evaluate_rules([first, second], _context())
    assert match is not None
    assert match.rule_id == "first"


def test_evaluate_rules_fan_out_all_verdict_is_reachable() -> None:
    """`fan_out_all` is the innovation-#1 extension point (T2.3) — not
    exercised by the shipped catalog, but must not be dead code."""
    rule = _rule("r1", base_confidence=0.9, verdict="fan_out_all")
    match = evaluate_rules([rule], _context())
    assert match is not None
    assert match.verdict == "fan_out_all"


# ─── RoutingContext.resolve ───────────────────────────────────────────────


def test_resolve_output_namespace_reads_own_output_field() -> None:
    context = _context(own_output={"status": "done", "score": 3})
    assert context.resolve("output", "status") == "done"
    assert context.resolve("output", "score") == 3


def test_resolve_output_namespace_missing_field_is_none() -> None:
    context = _context(own_output={"status": "done"})
    assert context.resolve("output", "missing") is None


def test_resolve_output_namespace_non_mapping_own_output_is_none() -> None:
    context = _context(own_output=None)
    assert context.resolve("output", "status") is None


def test_resolve_context_namespace_reads_scalar_attribute() -> None:
    context = _context(candidate_count=5, has_parsable_output=False)
    assert context.resolve("context", "candidate_count") == 5
    assert context.resolve("context", "has_parsable_output") is False


def test_resolve_context_namespace_unknown_field_is_none() -> None:
    context = _context()
    assert context.resolve("context", "not_a_real_field") is None


def test_resolve_unknown_namespace_is_none() -> None:
    context = _context()
    assert context.resolve("upstream", "anything") is None


# ─── Lot 3 P19 — the clamp must fail CLOSED on NaN ───────────────────────


def test_compute_confidence_when_score_is_nan_should_be_zero_not_one() -> None:
    """`max(0.0, min(1.0, nan))` returns 1.0 — every comparison against NaN
    is false, so `min` keeps its first argument and the clamp hands back
    MAXIMUM confidence. A garbage rule would then clear any threshold below
    1.0 and decide routing deterministically, in silence.

    The loader rejects NaN in YAML (T3.3), but `RoutingRule` is a public
    exported dataclass built directly by tests and, by design, by the future
    rule-learning component — so the clamp itself has to hold the line.
    """
    rule = _rule("nan-rule", base_confidence=math.nan)
    assert compute_confidence(rule, _context()) == 0.0


def test_compute_confidence_when_a_penalty_factor_is_nan_should_be_zero() -> None:
    """Same hazard one multiplication downstream — a finite base times a NaN
    factor is NaN."""
    rule = _rule(
        "nan-penalty",
        base_confidence=0.95,
        penalties=(
            RulePenalty(
                when=(parse("context.candidate_count >= 1", allowed_namespaces=_NS),),
                factor=math.nan,
            ),
        ),
    )
    assert compute_confidence(rule, _context()) == 0.0


def test_evaluate_rules_does_not_select_a_nan_scored_rule_over_a_real_one() -> None:
    """The end-to-end consequence of the clamp: a NaN rule must not outrank a
    legitimate one."""
    nan_rule = _rule("nan-rule", base_confidence=math.nan)
    good_rule = _rule("good-rule", base_confidence=0.5)
    match = evaluate_rules([nan_rule, good_rule], _context())
    assert match is not None
    assert match.rule_id == "good-rule"
    assert match.confidence == 0.5
