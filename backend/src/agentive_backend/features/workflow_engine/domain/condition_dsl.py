"""Branching-condition DSL parser (Story 4.1 AC1/AC3) — restricted, no ``eval``/``exec``.

Grammar: ``output.<field> <op> <literal>`` where ``<op>`` is one of
``==``, ``!=``, ``<``, ``<=``, ``>``, ``>=`` and ``<literal>`` is a quoted
string, a number, or ``true``/``false``. A single comparator per condition —
no ``and``/``or``, no parentheses, no function calls (lecture littérale de
"DSL léger" dans l'épic).

Security: the RHS literal is isolated by the regex below and parsed with
:func:`ast.literal_eval` ONLY — never the full expression, and never
``eval``/``exec``. ``literal_eval`` can only produce Python literals
(str/num/bool/None/tuple/list/dict/set of literals), so no code execution is
possible even if an attacker fully controls the input string (cf T8.2
security test).

Beyond the "no code execution" guarantee, the result of :func:`ast.literal_eval`
is further restricted to :data:`_ALLOWED_LITERAL_TYPES` (str/int/float/bool) —
the grammar promised by the epic and T3.3 ("chaîne quotée, un nombre, ou
true/false"). ``None``, ``bytes``, ``complex``, and every composite type
(``list``/``tuple``/``dict``/``set``) are rejected, not just composites.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentive_backend.features.workflow_engine.domain.value_objects import DomainValidationError
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

_CONDITION_RE = re.compile(
    r"^output\.([a-zA-Z_][a-zA-Z0-9_]*)\s*(==|!=|<=|>=|<|>)\s*(.+)$", re.DOTALL
)
_BOOLEAN_LITERALS: dict[str, bool] = {"true": True, "false": False}
_ALLOWED_LITERAL_TYPES = (str, int, float, bool)


@dataclass(frozen=True, slots=True)
class ParsedCondition:
    """A successfully-parsed branching condition — ``output.<field> <op> <literal>``."""

    field: str
    operator: str
    literal: Any


def parse(condition: str) -> ParsedCondition:
    """Parse ``condition`` or raise :class:`DomainValidationError`.

    Raises:
        DomainValidationError: the condition does not match the
            ``output.<field> <op> <literal>`` grammar, OR the RHS is not a
            scalar literal (composite literals, names, calls, arbitrary
            expressions are all rejected).
    """
    match = _CONDITION_RE.match(condition.strip())
    if match is None:
        raise DomainValidationError(
            f"invalid branching condition syntax: {condition!r} "
            "(expected 'output.<field> <op> <literal>')"
        )
    # NOT named `operator` — that shadowed the `operator` MODULE imported
    # above for the whole function body, so any future reference to
    # `operator.eq` inside `parse` would raise `UnboundLocalError`.
    field, comparator, raw_literal = match.groups()
    raw_literal = raw_literal.strip()

    if raw_literal in _BOOLEAN_LITERALS:
        literal: Any = _BOOLEAN_LITERALS[raw_literal]
    else:
        try:
            literal = ast.literal_eval(raw_literal)
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as exc:
            raise DomainValidationError(
                f"invalid literal in branching condition: {raw_literal!r}"
            ) from exc
        if not isinstance(literal, _ALLOWED_LITERAL_TYPES):
            raise DomainValidationError(
                "literal type not allowed in branching condition (expected a quoted "
                f"string, a number, or true/false): {raw_literal!r}"
            )

    return ParsedCondition(field=field, operator=comparator, literal=literal)


_COMPARATORS: dict[str, Callable[[Any, Any], bool]] = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def _type_compatible(actual: Any, literal: Any) -> bool:
    """Are these two values meaningfully comparable under this DSL?

    Python happily evaluates ``True == 1`` as ``True`` — so a node emitting
    ``{"flag": true}`` matched the condition ``output.flag == 1``, silently
    routing the run down a branch its author never described. ``bool`` is
    therefore only ever comparable with ``bool`` here, even though it is an
    ``int`` subclass to the language.

    Numbers compare with numbers, strings with strings. Two strings compare
    LEXICOGRAPHICALLY (so ``"9" > "10"`` is true) and that is intentional:
    the author wrote a quoted literal, which is an explicit request for a
    string comparison — the mismatch worth catching is ``str`` against a
    NUMERIC literal, which lands in the incompatible branch below.
    """
    if isinstance(actual, bool) or isinstance(literal, bool):
        return isinstance(actual, bool) and isinstance(literal, bool)
    if isinstance(actual, (int, float)) and isinstance(literal, (int, float)):
        return True
    return isinstance(actual, str) and isinstance(literal, str)


def evaluate(parsed: ParsedCondition, actual: Any) -> bool:
    """Compare ``actual`` (a node's runtime output value) to ``parsed.literal``.

    Story 4.2 T3.1. Two defensive-degradation cases — both return ``False``
    rather than raising, since a node routing decision must never crash the
    whole run over a data-shape mismatch (Dev Notes § Exécution d'un node):

    * ``actual`` is ``None`` — the variable is absent from the node's
      output at RUNTIME. Story 4.1 AC3 only validates ``output_contract.core``
      exposure at DESIGN time (``WorkflowService.create_workflow``); an LLM
      can still omit a promised field in a given run. Missing data degrades
      to "branch not taken", never an exception.
    * ``actual`` and the literal are not of comparable types (e.g. a ``str``
      actual against a numeric literal, or a ``bool`` against a number) —
      see :func:`_type_compatible`. ``!=`` holds (they ARE different), every
      other comparator degrades to ``False``, and the mismatch is logged
      rather than silently deciding a branch.
    * A ``TypeError`` still escapes a comparison the guard above admitted —
      kept as a belt-and-braces net rather than propagated.
    """
    if actual is None:
        return False
    if not _type_compatible(actual, parsed.literal):
        _log.warning(
            "workflow_engine.condition_type_mismatch",
            field=parsed.field,
            operator=parsed.operator,
            actual_type=type(actual).__name__,
            literal_type=type(parsed.literal).__name__,
        )
        # Values of incompatible types are, precisely, not equal — so `!=`
        # holds and every other comparator (including `==`) does not.
        return parsed.operator == "!="
    comparator = _COMPARATORS[parsed.operator]
    try:
        return bool(comparator(actual, parsed.literal))
    except TypeError:
        return False


__all__ = ["ParsedCondition", "evaluate", "parse"]
