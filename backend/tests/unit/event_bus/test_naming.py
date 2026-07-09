"""Unit tests — naming.validate_event_type."""

from __future__ import annotations

import pytest
import structlog

from agentive_backend.shared.event_bus.exceptions import InvalidEventTypeError
from agentive_backend.shared.event_bus.naming import (
    KNOWN_MODULE_PREFIXES,
    validate_event_type,
)


@pytest.mark.parametrize(
    "event_type",
    [
        "system.app.started",
        "system.app.shutdown",
        "system.health.checked",
        "company_architect.company.bootstrapped",
        "workflow_engine.workflow.started",
        "workflow_engine.workflow.completed",
        "memory_manager.chunk.indexed",
        "trace.span.exported",
    ],
)
def test_valid_event_types_pass(event_type: str) -> None:
    validate_event_type(event_type)  # must not raise


@pytest.mark.parametrize(
    "event_type",
    [
        "",
        "system",
        "system.started.now.again",  # 4 segments
        "System.Started",  # uppercase
        "_system.started.x",  # leading underscore
        "system..started",  # empty middle segment
        "1system.started.x",  # leading digit
        "system.started.",  # trailing dot
        ".system.started",  # leading dot
    ],
)
def test_invalid_event_types_raise(event_type: str) -> None:
    with pytest.raises(InvalidEventTypeError):
        validate_event_type(event_type)


def test_unknown_prefix_warns_but_passes() -> None:
    """`mX` prefix not in KNOWN_MODULE_PREFIXES → warn, no raise (P13 — structlog)."""
    assert "m99" not in KNOWN_MODULE_PREFIXES
    with structlog.testing.capture_logs() as captured:
        validate_event_type("m99.foo.bar")
    matching = [r for r in captured if r.get("event") == "event_bus.unknown_module_prefix"]
    assert matching, f"warn line not captured ; got {[r.get('event') for r in captured]}"
    assert matching[0]["prefix"] == "m99"


def test_validate_rejects_non_string() -> None:
    with pytest.raises(InvalidEventTypeError):
        validate_event_type(42)  # type: ignore[arg-type]


def test_event_type_too_long_raises() -> None:
    """P11 — pathological length cap protects NOTIFY 8000-byte limit."""
    too_long = "system.app." + ("x" * 200)
    with pytest.raises(InvalidEventTypeError, match="exceeds"):
        validate_event_type(too_long)
