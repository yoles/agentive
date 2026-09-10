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
import re
from dataclasses import dataclass
from typing import Any

from agentive_backend.features.workflow_engine.domain.value_objects import DomainValidationError

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
    field, operator, raw_literal = match.groups()
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

    return ParsedCondition(field=field, operator=operator, literal=literal)


__all__ = ["ParsedCondition", "parse"]
