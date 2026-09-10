"""Unit tests — branching-condition DSL parser (Story 4.1 T8.2, AC1)."""

from __future__ import annotations

import pytest

from agentive_backend.features.workflow_engine.domain.condition_dsl import evaluate, parse
from agentive_backend.features.workflow_engine.domain.value_objects import DomainValidationError

# ─── Valid parsing — each operator, each literal type ──────────────────


@pytest.mark.parametrize("operator", ["==", "!=", "<", "<=", ">", ">="])
def test_parse_accepts_each_operator(operator: str) -> None:
    parsed = parse(f"output.score {operator} 42")
    assert parsed.field == "score"
    assert parsed.operator == operator
    assert parsed.literal == 42


def test_parse_accepts_quoted_string_literal() -> None:
    parsed = parse("output.status == 'approved'")
    assert parsed.field == "status"
    assert parsed.operator == "=="
    assert parsed.literal == "approved"


def test_parse_accepts_double_quoted_string_literal() -> None:
    parsed = parse('output.status == "approved"')
    assert parsed.literal == "approved"


def test_parse_accepts_integer_literal() -> None:
    parsed = parse("output.count > 10")
    assert parsed.literal == 10


def test_parse_accepts_float_literal() -> None:
    parsed = parse("output.confidence >= 0.85")
    assert parsed.literal == 0.85


def test_parse_accepts_true_literal() -> None:
    parsed = parse("output.is_valid == true")
    assert parsed.literal is True


def test_parse_accepts_false_literal() -> None:
    parsed = parse("output.is_valid == false")
    assert parsed.literal is False


def test_parse_strips_surrounding_whitespace() -> None:
    parsed = parse("  output.score   >=   5  ")
    assert parsed.field == "score"
    assert parsed.operator == ">="
    assert parsed.literal == 5


def test_parse_of_literal_with_embedded_newline_reports_invalid_literal_not_bad_grammar() -> None:
    """A raw embedded newline inside the quoted literal (e.g. decoded from a
    JSON `\\n` escape) is not valid Python string-literal syntax either way
    (a real newline requires triple-quoting, which this grammar doesn't
    support) — still a 422. But `re.DOTALL` ensures the regex isolates the
    RHS correctly, so the error names the literal as the problem instead of
    misreporting the whole condition as not matching the grammar."""
    with pytest.raises(DomainValidationError, match="invalid literal"):
        parse("output.notes == 'line1\nline2'")


# ─── Rejections — grammar ───────────────────────────────────────────────


def test_parse_rejects_missing_output_prefix() -> None:
    with pytest.raises(DomainValidationError):
        parse("status == 'approved'")


def test_parse_rejects_composed_expression_with_and() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.status == 'approved' and output.count > 1")


def test_parse_rejects_composed_expression_with_or() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.status == 'approved' or output.status == 'rejected'")


def test_parse_rejects_unknown_operator() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.score === 5")


def test_parse_rejects_composite_list_literal() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.tags == ['a', 'b']")


def test_parse_rejects_composite_dict_literal() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.meta == {'a': 1}")


def test_parse_rejects_none_literal() -> None:
    """`None` is not in the grammar's literal set (string/number/bool only)."""
    with pytest.raises(DomainValidationError):
        parse("output.status == None")


def test_parse_rejects_bytes_literal() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.status == b'x'")


def test_parse_rejects_complex_literal() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.score == 5j")


# ─── Rejections — security (never eval()/exec()) ────────────────────────


def test_parse_rejects_function_call_literal_without_executing_it() -> None:
    """The canonical injection attempt: prove no code execution happens."""
    with pytest.raises(DomainValidationError):
        parse("output.status == __import__('os').system('echo pwned')")


def test_parse_rejects_attribute_access_literal() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.status == os.environ")


def test_parse_rejects_bare_name_literal() -> None:
    with pytest.raises(DomainValidationError):
        parse("output.status == some_variable")


# ─── evaluate() — Story 4.2 T3.1/T3.4 ───────────────────────────────────


@pytest.mark.parametrize(
    ("condition", "actual", "expected"),
    [
        ("output.status == 'ok'", "ok", True),
        ("output.status == 'ok'", "nope", False),
        ("output.status != 'ok'", "nope", True),
        ("output.count > 10", 15, True),
        ("output.count > 10", 5, False),
        ("output.count >= 10", 10, True),
        ("output.count < 10", 5, True),
        ("output.count <= 10", 10, True),
        ("output.is_valid == true", True, True),
        ("output.is_valid == false", False, True),
        ("output.confidence >= 0.85", 0.9, True),
    ],
)
def test_evaluate_each_operator(condition: str, actual: object, expected: bool) -> None:
    parsed = parse(condition)
    assert evaluate(parsed, actual) is expected


def test_evaluate_missing_variable_degrades_to_false() -> None:
    """T3.1 — a variable absent at RUNTIME (actual=None) must never raise,
    even though Story 4.1 AC3 only validates exposure at design-time."""
    parsed = parse("output.status == 'ok'")
    assert evaluate(parsed, None) is False


def test_evaluate_missing_variable_degrades_to_false_for_not_equal_too() -> None:
    """Missing data degrades to 'branch not taken' regardless of operator —
    not just for `==`, where the natural Python semantics would coincidentally
    agree."""
    parsed = parse("output.status != 'ok'")
    assert evaluate(parsed, None) is False


def test_evaluate_incompatible_types_ordering_degrades_to_false() -> None:
    """T3.1 — `str` vs `int` under `<`/`<=`/`>`/`>=` raises `TypeError` in
    plain Python; `evaluate` must catch it and degrade to False, never let
    it propagate and crash the run."""
    parsed = parse("output.score > 10")
    assert evaluate(parsed, "not-a-number") is False


def test_evaluate_incompatible_types_equality_never_raises() -> None:
    """`==`/`!=` never raise across mismatched types in Python — the
    comparison is simply unequal. No TypeError-catching path is exercised
    here, distinguishing this from the ordering-operator case above."""
    parsed = parse("output.count == 5")
    assert evaluate(parsed, "5") is False


# ─── Lot 3 — findings #32, #33 ─────────────────────────────────────────


def test_parse_does_not_shadow_the_operator_module() -> None:
    """#32 — the local unpacking variable was named `operator`, shadowing the
    module imported at the top of `condition_dsl` for the whole function
    body. Nothing referenced `operator.eq` inside `parse` YET, so the bug was
    latent: the first one to do so would get `UnboundLocalError`."""
    import agentive_backend.features.workflow_engine.domain.condition_dsl as dsl

    assert dsl.operator.eq(1, 1) is True
    assert parse("output.status == 'ok'").operator == "=="


@pytest.mark.parametrize(
    ("condition", "actual", "expected"),
    [
        # `True == 1` is True in Python — but a node emitting {"flag": true}
        # must not satisfy a condition its author wrote about the number 1.
        ("output.flag == 1", True, False),
        ("output.flag != 1", True, True),
        ("output.flag == true", True, True),
        ("output.flag == false", True, False),
        # A numeric literal against a string actual is a shape mismatch.
        ("output.score > 10", "12", False),
        ("output.score == 10", "10", False),
        ("output.score != 10", "10", True),
    ],
)
def test_incompatible_types_never_silently_take_a_branch(
    condition: str, actual: object, expected: bool
) -> None:
    """#33 — a type mismatch used to be resolved by Python's own coercion
    rules rather than refused, silently routing the run down a branch nobody
    described."""
    assert evaluate(parse(condition), actual) is expected


def test_quoted_literals_still_compare_as_strings() -> None:
    """The flip side: a QUOTED literal is an explicit request for a string
    comparison, so `"9" > "10"` staying lexicographically true is correct,
    not a bug to fix."""
    assert evaluate(parse("output.v > '10'"), "9") is True
    assert evaluate(parse("output.v == 'ok'"), "ok") is True


def test_compatible_numeric_types_still_compare() -> None:
    """int/float mixing is legitimate and must keep working."""
    assert evaluate(parse("output.score >= 0.5"), 1) is True
    assert evaluate(parse("output.score < 10"), 9.5) is True
