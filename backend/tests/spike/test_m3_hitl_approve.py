"""Test AC3 — HITL approve path : interrupt → Command(resume=True) → END."""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from spike.m3_langgraph import build_graph

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("force_mock_llm")]


async def test_hitl_approve_resumes_to_reviewer(checkpoint_dsn: str) -> None:
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn) as checkpointer:
        await checkpointer.setup()
        graph = build_graph(checkpointer)

        first = await graph.ainvoke({"task_input": "Approve me!"}, config)
        # The graph stops at the interrupt — __interrupt__ surfaces the gate payload.
        interrupts = first.get("__interrupt__")
        assert interrupts, "expected an interrupt at the quality_gate node"
        gate_payload = interrupts[0].value
        assert gate_payload["question"] == "Approve the producer draft?"
        assert gate_payload["draft"], "gate payload should expose the producer draft"

        final = await graph.ainvoke(Command(resume={"approved": True}), config)
        assert final.get("reviewer_output"), "reviewer_output must be set after approval"
        assert final.get("approved") is True
        assert final.get("iterations") == 1, (
            "exactly one iteration through producer expected on direct approval"
        )
