"""Mise en Place automatique — pure domain core (Story 4.5 AC1, T2).

Framework-free (dataclasses only, no Pydantic, no I/O) — mirrors the posture
of :mod:`.dry_run`. ``.import-linter`` Contract 2/3 compliance: no
``infra.*``/``sqlalchemy``/``pydantic`` imports here.

The four checks themselves (I/O-bound: MCP discovery, namespace lookup,
Dry Run reuse, API-key presence) live in the orchestrator,
:class:`~agentive_backend.features.workflow_engine.mise_en_place.MiseEnPlaceService`
(T3) — this module only assembles their already-computed :class:`CheckResult`
outcomes into a :class:`MiseEnPlaceReport`. The "bypasser ou pas" decision
also stays in the orchestrator (it needs ``force``/``reason`` from the HTTP
request, which this module never sees).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

#: The four pre-workflow checks (AC1) — a closed set, always all four
#: present in a report regardless of outcome (never a partial report).
CheckCode = Literal[
    "mcp_tools_reachable",
    "memory_namespaces_accessible",
    "budget_available",
    "llm_providers_configured",
]

#: The same four codes as a runtime value, in the canonical report order.
#: Single source of truth: the orchestrator gathers its checks in this order
#: and :func:`build_report` enforces it, so "always exactly four" is a
#: checked invariant rather than a comment (review P8).
CHECK_CODES: Final[tuple[CheckCode, ...]] = (
    "mcp_tools_reachable",
    "memory_namespaces_accessible",
    "budget_available",
    "llm_providers_configured",
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one pre-workflow check (AC1)."""

    code: CheckCode
    passed: bool
    detail: str
    #: Populated only when ``passed`` is ``False`` — a concrete, code-specific
    #: remediation (AC2) rather than a generic "fix your workflow" message.
    suggested_action: str | None = None
    #: Whether an identical retry could plausibly succeed WITHOUT anyone
    #: changing configuration (review BS5). An MCP server that is down or a
    #: check that timed out: yes. A missing API key, an absent namespace, a
    #: budget overrun, a tool that no longer exists: no — those need a human
    #: to change something first.
    #:
    #: This drives the HTTP status ``start_run`` refuses with: 503 (which
    #: tells clients, proxies and gateways to retry) only when EVERY failing
    #: check is retryable, otherwise 422. Emitting 503 for a permanently
    #: misconfigured workflow invited infinite automatic retry loops on a
    #: launch that could never succeed.
    #:
    #: Meaningless while ``passed`` is ``True``; left ``False`` there.
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class MiseEnPlaceReport:
    """The persisted/returned report (AC1) — always exactly four
    :class:`CheckResult`, one per :data:`CheckCode`, whatever the outcome."""

    checks: tuple[CheckResult, ...]
    #: ``True`` only when the caller explicitly bypassed one or more failing
    #: checks via ``force=true`` (AC3) — never set for a report where every
    #: check already passed (AC3's "no-op silencieux").
    bypassed: bool = False
    bypass_reason: str | None = None

    @property
    def all_passed(self) -> bool:
        return all(check.passed for check in self.checks)


def build_report(
    checks: Sequence[CheckResult],
    *,
    bypassed: bool = False,
    bypass_reason: str | None = None,
) -> MiseEnPlaceReport:
    """Pure construction — no decision logic (T2.4). Whether a failing report
    gets bypassed is decided by the orchestrator
    (:meth:`MiseEnPlaceService.run_checks`'s caller, ``WorkflowExecutionService.
    start_run``), which alone has access to the request's ``force``/``reason``.

    Raises:
        ValueError: ``checks`` is not exactly one entry per :data:`CHECK_CODES`
            (review P8). ``all_passed`` is an ``all()``, so it is vacuously
            ``True`` for an empty or partial report — a mis-wired orchestrator
            would otherwise clear every launch and persist a hollow audit
            record. Failing here makes that impossible to express.
    """
    checks = tuple(checks)
    codes = [check.code for check in checks]
    if sorted(codes) != sorted(CHECK_CODES):
        raise ValueError(
            f"a Mise en Place report must carry exactly one result per check code "
            f"{list(CHECK_CODES)}, got {codes}"
        )
    return MiseEnPlaceReport(checks=checks, bypassed=bypassed, bypass_reason=bypass_reason)


__all__ = ["CHECK_CODES", "CheckCode", "CheckResult", "MiseEnPlaceReport", "build_report"]
