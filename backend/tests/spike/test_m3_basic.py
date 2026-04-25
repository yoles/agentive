"""Test AC1 — workflow basique se termine en < 30s avec MockLLM.

Lance le spike de bout en bout via :func:`spike.m3_langgraph.run_spike` (qui
auto-approuve l'interrupt HITL pour ce test) et vérifie :

- l'état terminal contient ``producer_output`` et ``reviewer_output`` non null ;
- ``iterations == 1`` (1 seule passe via le producer) ;
- la durée wall-clock est < 30s.
"""

from __future__ import annotations

import time

import pytest
from spike.m3_langgraph import run_spike

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("force_mock_llm", "checkpointer")]


async def test_basic_workflow_terminates_with_mock_llm(checkpoint_dsn: str) -> None:
    started = time.monotonic()
    result = await run_spike()
    duration = time.monotonic() - started

    assert duration < 30.0, f"workflow took {duration:.2f}s — exceeds AC1 limit of 30s"
    assert result.get("producer_output"), "producer_output absent — producer node didn't run"
    assert result.get("reviewer_output"), "reviewer_output absent — reviewer node didn't run"
    assert result.get("iterations") == 1, (
        f"expected 1 iteration on auto-approved happy path, got {result.get('iterations')}"
    )
    assert result.get("approved") is True, "approved flag should be True after auto-approve"
