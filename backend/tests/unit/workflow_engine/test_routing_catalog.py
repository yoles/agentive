"""Unit tests — routing rules catalog loader (Story 4.3 T11.3).

``test_load_production_catalog_is_valid`` loads the FILE SHIPPED IN
PRODUCTION, not a fixture copy — a typo in the real
``templates/routing-rules.yaml`` must fail CI, not sail through unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentive_backend.features.workflow_engine.routing_catalog import (
    DEFAULT_CATALOG_PATH,
    load_routing_rules,
)


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "routing-rules.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_production_catalog_is_valid() -> None:
    # No `len(rules) == 3`: the runbook promises "adding a rule is a YAML edit,
    # no code change", and a hard count breaks that promise for no gain — the
    # id list below already pins everything it did, and says WHICH rule
    # changed when it fails. It is a prefix check so appending a rule stays a
    # YAML-only edit while removing or reordering the three shipped ones does
    # not.
    rules = load_routing_rules(DEFAULT_CATALOG_PATH)
    ids = [rule.rule_id for rule in rules]
    assert ids[:3] == ["terminal-output-status", "failed-output-status", "no-parsable-output"]
    for rule in rules:
        assert rule.verdict == "terminate"
        assert 0.0 <= rule.base_confidence <= 1.0


def test_load_routing_rules_missing_file_raises_runtime_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="cannot read"):
        load_routing_rules(tmp_path / "does-not-exist.yaml")


def test_load_routing_rules_malformed_yaml_raises_runtime_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "rules: [this is not: valid: yaml:")
    with pytest.raises(RuntimeError, match="YAML parse error"):
        load_routing_rules(path)


def test_load_routing_rules_duplicate_rule_id_raises_runtime_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: dup
            description: first
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: 0.9
          - rule_id: dup
            description: second
            when: ["output.status == 'failed'"]
            verdict: terminate
            base_confidence: 0.9
        """,
    )
    with pytest.raises(RuntimeError, match="duplicate rule_id"):
        load_routing_rules(path)


def test_load_routing_rules_unknown_field_raises_runtime_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: 0.9
            not_a_real_field: 1
        """,
    )
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_routing_rules(path)


def test_load_routing_rules_rejects_nan_base_confidence(tmp_path: Path) -> None:
    """T3.3 — without `allow_inf_nan=False`, `.nan` passes `ge=0.0, le=1.0`
    silently (every NaN comparison is False)."""
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: .nan
        """,
    )
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_routing_rules(path)


def test_load_routing_rules_rejects_zero_penalty_factor(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: 0.9
            penalties:
              - when: ["context.candidate_count >= 3"]
                factor: 0
        """,
    )
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_routing_rules(path)


def test_load_routing_rules_rejects_out_of_range_base_confidence(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: 1.5
        """,
    )
    with pytest.raises(RuntimeError, match="schema validation error"):
        load_routing_rules(path)


def test_load_routing_rules_invalid_predicate_syntax_raises_at_load_time(tmp_path: Path) -> None:
    """Predicates fail the DSL grammar at LOAD time, never at decision time
    (mirror `_make_router`'s pre-parsing justification)."""
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["output.status == 'done' and output.count > 1"]
            verdict: terminate
            base_confidence: 0.9
        """,
    )
    with pytest.raises(RuntimeError, match="invalid predicate"):
        load_routing_rules(path)


@pytest.mark.parametrize(
    "predicate",
    [
        pytest.param("context.candidat_count >= 3", id="typo_in_field_name"),
        pytest.param("context.own_output != 1", id="real_attribute_but_not_a_predicate_field"),
        pytest.param("context.nope == 1", id="plainly_unknown"),
    ],
)
def test_load_routing_rules_unknown_context_field_raises_at_load_time(
    tmp_path: Path, predicate: str
) -> None:
    """`parse` validates the NAMESPACE; nothing validated the FIELD.

    `RoutingContext.resolve` answers `None` for a field it does not carry, and
    `evaluate` degrades an unresolved value to a false predicate — so a rule
    with a typo loaded cleanly at boot, never fired, and silently escalated to
    the LLM every decision it was written to catch. No error, no log, no
    metric. `own_output` is in the parametrize on purpose: it IS an attribute
    of `RoutingContext`, so a `getattr`-only check would let it through and
    compare a mapping against a literal.
    """
    path = _write(
        tmp_path,
        f"""
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["{predicate}"]
            verdict: terminate
            base_confidence: 0.9
        """,
    )
    with pytest.raises(RuntimeError, match="unknown context field"):
        load_routing_rules(path)


def test_load_routing_rules_unknown_context_field_in_a_penalty_also_raises(
    tmp_path: Path,
) -> None:
    """Penalties go through the same parser — a typo there silently disables
    the penalty instead of the rule, which is worse: the rule still fires, at
    a confidence that was never meant to apply."""
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: 0.9
            penalties:
              - when: ["context.candidate_kount >= 3"]
                factor: 0.7
        """,
    )
    with pytest.raises(RuntimeError, match="unknown context field"):
        load_routing_rules(path)


def test_load_routing_rules_known_context_fields_are_accepted(tmp_path: Path) -> None:
    """The guard must not reject the fields the production catalog uses."""
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: r1
            description: d
            when: ["context.has_parsable_output == false"]
            verdict: terminate
            base_confidence: 0.9
            penalties:
              - when: ["context.candidate_count >= 3"]
                factor: 0.7
        """,
    )
    assert len(load_routing_rules(path)) == 1


def test_load_routing_rules_preserves_declaration_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        archetype_version: 1
        rules:
          - rule_id: z-declared-first
            description: d
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: 0.9
          - rule_id: a-declared-second
            description: d
            when: ["output.status == 'done'"]
            verdict: terminate
            base_confidence: 0.9
        """,
    )
    rules = load_routing_rules(path)
    assert [rule.rule_id for rule in rules] == ["z-declared-first", "a-declared-second"]
