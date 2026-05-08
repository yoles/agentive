"""Test AC3 — HITL reject path : feedback re-route vers producer + 2ème passage approuvé."""

from __future__ import annotations

import uuid

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from spike.m3_langgraph import MAX_ITERATIONS, build_graph

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("force_mock_llm")]


async def test_hitl_reject_then_approve_loops_through_producer(checkpoint_dsn: str) -> None:
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn) as checkpointer:
        await checkpointer.setup()
        graph = build_graph(checkpointer)

        first = await graph.ainvoke({"task_input": "Iterate me twice"}, config)
        assert first.get("__interrupt__"), "expected first interrupt at quality_gate"

        # First gate REJECT with feedback ⇒ should re-route to producer for a 2nd pass.
        rejected = await graph.ainvoke(
            Command(resume={"approved": False, "feedback": "needs more rhythm"}),
            config,
        )
        assert rejected.get("__interrupt__"), (
            "after rejection, the producer should run again and the gate should re-trigger"
        )

        # 2nd gate: approve. iterations == 2, reviewer eventually runs.
        final = await graph.ainvoke(Command(resume={"approved": True}), config)
        assert final.get("reviewer_output"), "reviewer must run after 2nd approval"
        assert final.get("iterations") == 2, (
            f"expected 2 producer iterations after 1 reject + 1 approve, got {final.get('iterations')}"
        )
        assert MAX_ITERATIONS >= 2, (
            "test assumes spike allows at least 2 iterations before auto-approve"
        )
