"""Unit tests — branching-condition DSL parser (Story 4.1 T8.2, AC1)."""

from __future__ import annotations

import pytest

from agentive_backend.features.workflow_engine.domain.condition_dsl import parse
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
