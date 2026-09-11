"""Unit tests — Mise en Place automatique, pure domain core (Story 4.5 T2.5).

No DB, no network — construction/aggregation of the report only. The four
I/O-bound checks themselves are covered in
``tests/unit/workflow_engine/test_mise_en_place_service.py``.
"""

from __future__ import annotations

import pytest

from agentive_backend.features.workflow_engine.domain.mise_en_place import (
    CHECK_CODES,
    CheckResult,
    build_report,
)


def _check(code: str, *, passed: bool) -> CheckResult:
    return CheckResult(code=code, passed=passed, detail=f"{code} detail")  # type: ignore[arg-type]


def test_build_report_all_passing_is_all_passed() -> None:
    report = build_report(
        [
            _check("mcp_tools_reachable", passed=True),
            _check("memory_namespaces_accessible", passed=True),
            _check("budget_available", passed=True),
            _check("llm_providers_configured", passed=True),
        ]
    )
    assert report.all_passed is True
    assert len(report.checks) == 4


def test_build_report_one_failing_check_is_not_all_passed() -> None:
    report = build_report(
        [
            _check("mcp_tools_reachable", passed=False),
            _check("memory_namespaces_accessible", passed=True),
            _check("budget_available", passed=True),
            _check("llm_providers_configured", passed=True),
        ]
    )
    assert report.all_passed is False


def test_build_report_multiple_failing_checks_is_not_all_passed() -> None:
    report = build_report(
        [
            _check("mcp_tools_reachable", passed=False),
            _check("memory_namespaces_accessible", passed=False),
            _check("budget_available", passed=True),
            _check("llm_providers_configured", passed=False),
        ]
    )
    assert report.all_passed is False
    assert sum(1 for c in report.checks if not c.passed) == 3


def _four(*failing: str) -> list[CheckResult]:
    """One result per check code — the only cardinality `build_report`
    accepts (review P8)."""
    return [_check(code, passed=code not in failing) for code in CHECK_CODES]


def test_build_report_defaults_to_not_bypassed() -> None:
    report = build_report(_four())
    assert report.bypassed is False
    assert report.bypass_reason is None


def test_build_report_propagates_bypass_fields_unchanged() -> None:
    report = build_report(
        _four("mcp_tools_reachable"),
        bypassed=True,
        bypass_reason="incident P1, deadline serrée",
    )
    assert report.bypassed is True
    assert report.bypass_reason == "incident P1, deadline serrée"
    # `build_report` is pure assembly — it never inspects `checks` to decide
    # `bypassed`, so an all-failing report can be marked bypassed and an
    # all-passing one can too (the decision belongs to the orchestrator).
    assert report.all_passed is False


def test_check_result_suggested_action_defaults_to_none() -> None:
    result = CheckResult(code="budget_available", passed=True, detail="within cap")
    assert result.suggested_action is None


# ─── cardinality is enforced, not merely documented (review P8) ──────────


def test_build_report_when_a_check_is_missing_should_raise() -> None:
    """`all_passed` is an `all()`, so a partial report is vacuously True —
    a mis-wired orchestrator would clear every launch and persist a hollow
    audit record."""
    with pytest.raises(ValueError, match="exactly one result per check code"):
        build_report([_check("budget_available", passed=True)])


def test_build_report_when_report_is_empty_should_raise() -> None:
    with pytest.raises(ValueError, match="exactly one result per check code"):
        build_report([])


def test_build_report_when_a_code_is_duplicated_should_raise() -> None:
    duplicated = [
        _check("budget_available", passed=True),
        _check("budget_available", passed=True),
        _check("mcp_tools_reachable", passed=True),
        _check("memory_namespaces_accessible", passed=True),
    ]
    with pytest.raises(ValueError, match="exactly one result per check code"):
        build_report(duplicated)
