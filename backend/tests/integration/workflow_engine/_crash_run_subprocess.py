"""Crash-injection subprocess — Story 4.2 T10.5.

NOT a pytest module (leading underscore — pytest's default ``test_*.py``
collection pattern skips it). Invoked by ``test_recovery_e2e.py`` as a
subprocess: seeds a 2-node linear workflow + run, drives it via the REAL
``WorkflowExecutionService._drive_run`` with a mocked (no-network) LLM,
waits until node ``a``'s checkpoint is durably committed to Postgres (not
just yielded in-process — mirror ``spike/m3_langgraph.py
::_wait_for_committed_state``, the same timing race applies here), then
SIGKILLs itself. Simulates a process crash mid-run for the recovery-worker
integration test (T7, AC3) to resume from.

Usage: ``python _crash_run_subprocess.py <ids_output_file>``
Requires ``POSTGRES_*`` env vars pointing at the target Postgres (the
caller's ``migrated_db`` fixture already sets these in ``os.environ``).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agentive_backend.features.workflow_engine.routing_catalog import load_routing_rules
from agentive_backend.features.workflow_engine.service import WorkflowExecutionService
from agentive_backend.infra.db.session import get_session_factory
from agentive_backend.shared.config import settings
from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import ChatMessage, Completion
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo, WorkflowRunRepo

_COMMIT_POLL_TIMEOUT_S = 10.0
_COMMIT_POLL_INTERVAL_S = 0.05

# Node `b`'s completion is deliberately delayed: with an instant mock LLM,
# node `b` could finish (and commit) before the crash-detection poller even
# notices node `a`'s commit, racing the SIGKILL. This guarantees the kill
# always lands while `b` is still in flight — b's own call is never allowed
# to return before the process is gone.
_NODE_B_DELAY_S = 3.0


class _DelayedSecondCallProvider:
    """Wraps :class:`MockProvider` — every call AFTER the first sleeps
    ``_NODE_B_DELAY_S`` before delegating (this workflow has exactly 2
    sequential nodes, so "second call" == node ``b``)."""

    def __init__(self, inner: MockProvider) -> None:
        self._inner = inner
        self._call_count = 0

    async def complete(self, messages: Sequence[ChatMessage], **kwargs: Any) -> Completion:
        self._call_count += 1
        if self._call_count > 1:
            await asyncio.sleep(_NODE_B_DELAY_S)
        return await self._inner.complete(messages, **kwargs)


def _checkpoint_dsn() -> str:
    return settings.psycopg_dsn


def _completion(text: str) -> Completion:
    return Completion(
        text=text,
        model="mock-model",
        provider="mock",
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
        latency_ms=5.0,
    )


async def _wait_for_node_committed(dsn: str, thread_id: str, node_id: str) -> None:
    """Block until ``node_id`` appears in the LATEST Postgres checkpoint's
    ``channel_values.node_outputs`` — deterministic proof of durability
    before triggering SIGKILL (mirror the spike's proven pattern)."""
    config = {"configurable": {"thread_id": thread_id}}
    deadline = time.monotonic() + _COMMIT_POLL_TIMEOUT_S
    async with AsyncPostgresSaver.from_conn_string(dsn) as poller:
        while time.monotonic() < deadline:
            tup = await poller.aget_tuple(config)
            channel_values = (tup.checkpoint or {}).get("channel_values", {}) if tup else {}
            node_outputs = channel_values.get("node_outputs") or {}
            if node_id in node_outputs:
                return
            await asyncio.sleep(_COMMIT_POLL_INTERVAL_S)
    raise RuntimeError(f"timeout waiting for node {node_id!r} checkpoint commit")


async def main() -> None:
    out_path = Path(sys.argv[1])
    session_factory = get_session_factory()

    workflow_repo = WorkflowRepo(session_factory=session_factory)
    workflow_run_repo = WorkflowRunRepo(session_factory=session_factory)
    template_repo = AgentTemplateRepo(session_factory=session_factory)

    tpl_a = await template_repo.create(
        name=f"crash-tpl-a-{uuid.uuid4()}", archetype="producteur", config={}
    )
    tpl_b = await template_repo.create(
        name=f"crash-tpl-b-{uuid.uuid4()}", archetype="producteur", config={}
    )
    dag_payload = {
        "nodes": [
            {"node_id": "a", "agent_template_id": str(tpl_a.id)},
            {"node_id": "b", "agent_template_id": str(tpl_b.id)},
        ],
        "edges": [{"from_node_id": "a", "to_node_id": "b", "condition": None}],
    }
    workflow = await workflow_repo.create(name=f"crash-workflow-{uuid.uuid4()}", dag=dag_payload)

    correlation_id = uuid.uuid4()
    run = await workflow_run_repo.create(workflow_id=workflow.id, correlation_id=correlation_id)

    # Written BEFORE the crash so the test (which polls for this file) can
    # pick up the ids even though the process is about to be killed.
    out_path.write_text(
        json.dumps(
            {
                "run_id": str(run.id),
                "workflow_id": str(workflow.id),
                "template_a_id": str(tpl_a.id),
                "template_b_id": str(tpl_b.id),
            }
        )
    )

    llm_router = LLMRouter(
        providers={
            "mock": _DelayedSecondCallProvider(
                MockProvider("mock", [_completion('{"step": "a"}'), _completion('{"step": "b"}')])
            )
        },
        default_chain=["mock"],
    )

    async with AsyncPostgresSaver.from_conn_string(_checkpoint_dsn()) as checkpointer:
        service = WorkflowExecutionService(
            workflow_repo=workflow_repo,
            workflow_run_repo=workflow_run_repo,
            template_repo=template_repo,
            llm_router=llm_router,
            checkpointer=checkpointer,
            routing_rules=load_routing_rules(),
        )
        templates = {"a": tpl_a, "b": tpl_b}
        # Reference kept (RUF006) so the task isn't GC'd mid-flight — never
        # awaited to completion, since the process is SIGKILLed right after
        # `_wait_for_node_committed` returns.
        drive_task = asyncio.create_task(
            service._drive_run(
                run.id, workflow, templates, {"question": "hi"}, correlation_id=correlation_id
            )
        )
        await _wait_for_node_committed(_checkpoint_dsn(), str(run.id), "a")
        assert not drive_task.cancelled()

    # SIGKILL bypasses `atexit`/`finally` — simulates a brutal crash (OOM,
    # hardware loss). Whatever wasn't already committed to Postgres is lost.
    os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    asyncio.run(main())
