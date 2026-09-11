"""End-to-end integration test — crash + recovery (Story 4.2 T10.5, AC3).

Mirror of ``tests/spike/test_m3_resume.py`` (the M3 spike's proven
subprocess + SIGKILL strategy), replayed against the REAL production code
path (``WorkflowExecutionService``/``WorkflowRecoveryWorker``) instead of
the spike's own hand-rolled graph:

1. Launch ``_crash_run_subprocess.py`` — seeds a 2-node linear workflow +
   run, drives it via ``_drive_run`` with a mocked LLM, waits until node
   ``a``'s checkpoint is durably committed, then SIGKILLs itself.
2. Confirm ``returncode == -SIGKILL`` and inspect the LangGraph checkpoint
   directly — ``node_outputs.a`` present, ``node_outputs.b`` absent.
3. Instantiate a real ``WorkflowRecoveryWorker`` with ``stale_threshold_s=0``
   (force immediate staleness) and call ``run_once()`` — verifies the
   orphaned run is detected, resumed, and reaches ``completed`` WITHOUT
   re-executing node ``a`` (proof: the resume-time ``LLMRouter`` only has a
   completion queued for node ``b`` — if node ``a`` were replayed, either
   its output would come out wrong or node ``b``'s call would find the mock
   queue exhausted and the run would end in ``error``, not ``completed``).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.features.workflow_engine.recovery import WorkflowRecoveryWorker
from agentive_backend.features.workflow_engine.routing_catalog import load_routing_rules
from agentive_backend.features.workflow_engine.service import WorkflowExecutionService
from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import Completion
from agentive_backend.shared.repositories import AgentTemplateRepo, WorkflowRepo, WorkflowRunRepo

from .conftest import _workflow_checkpoint_dsn

pytestmark = pytest.mark.integration

_SUBPROCESS_TIMEOUT_S = 30.0
_RESUME_POLL_TIMEOUT_S = 15.0
_RESUME_POLL_INTERVAL_S = 0.1


def _python_executable() -> str:
    return sys.executable


def _completion(text_: str) -> Completion:
    return Completion(
        text=text_,
        model="mock-model",
        provider="mock",
        input_tokens=10,
        output_tokens=5,
        finish_reason="stop",
        latency_ms=5.0,
    )


@pytest.mark.asyncio
async def test_resume_after_sigkill_does_not_replay_node_a(
    migrated_db: str,
    app_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    ids_file = tmp_path / "crash-run-ids.json"
    script_path = Path(__file__).parent / "_crash_run_subprocess.py"

    env = os.environ.copy()  # `migrated_db` already pointed POSTGRES_* at the testcontainer

    try:
        proc = subprocess.run(
            [_python_executable(), str(script_path), str(ids_file)],
            env=env,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"crash subprocess exceeded {_SUBPROCESS_TIMEOUT_S}s timeout.\n"
            f"stdout: {(exc.stdout or b'')!r}\nstderr: {(exc.stderr or b'')!r}"
        )
    assert proc.returncode == -signal.SIGKILL, (
        f"expected SIGKILL exit (-9), got returncode={proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )

    ids = json.loads(ids_file.read_text())
    run_id = UUID(ids["run_id"])
    workflow_id = UUID(ids["workflow_id"])

    # ── 2. Inspect Postgres: node `a`'s checkpoint must be durable, `b` must not. ──
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    dsn = _workflow_checkpoint_dsn()
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        tup = await saver.aget_tuple({"configurable": {"thread_id": str(run_id)}})
        assert tup is not None, "no checkpoint persisted before the crash"
        node_outputs = (tup.checkpoint or {}).get("channel_values", {}).get("node_outputs", {})
        assert "a" in node_outputs, "node a must have committed before the crash"
        assert "b" not in node_outputs, "node b must NOT have run before the crash"

    async with app_session_factory() as session:
        result = await session.execute(
            text("SELECT status FROM workflow_runs WHERE id = :id"), {"id": str(run_id)}
        )
        assert result.scalar_one() == "running"

    # ── 3. Resume via a real WorkflowRecoveryWorker — node a must NOT replay. ──
    workflow_repo = WorkflowRepo(session_factory=app_session_factory)
    workflow_run_repo = WorkflowRunRepo(session_factory=app_session_factory)
    template_repo = AgentTemplateRepo(session_factory=app_session_factory)

    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        # Only node `b`'s completion is queued — if node `a` replayed, its
        # LLM call would wrongly consume this entry (or, if `a` somehow got
        # a fabricated output some other way, `b`'s real call would find the
        # queue exhausted) — either way the run would NOT reach `completed`
        # with node b's expected value.
        llm_router = LLMRouter(
            providers={"mock": MockProvider("mock", [_completion('{"step": "b"}')])},
            default_chain=["mock"],
        )
        execution_service = WorkflowExecutionService(
            workflow_repo=workflow_repo,
            workflow_run_repo=workflow_run_repo,
            template_repo=template_repo,
            llm_router=llm_router,
            checkpointer=checkpointer,
            routing_rules=load_routing_rules(),
        )
        worker = WorkflowRecoveryWorker(
            workflow_execution_service=execution_service,
            session_factory=app_session_factory,
            stale_threshold_s=0.0,
        )

        summary = await worker.run_once()
        assert summary.resume_triggered_count == 1
        assert summary.failed_count == 0

        deadline = asyncio.get_event_loop().time() + _RESUME_POLL_TIMEOUT_S
        row: dict[str, object] = {}
        while asyncio.get_event_loop().time() < deadline:
            async with app_session_factory() as session:
                result = await session.execute(
                    text("SELECT status, checkpoint FROM workflow_runs WHERE id = :id"),
                    {"id": str(run_id)},
                )
                row = dict(result.mappings().one())
            if row["status"] in {"completed", "error"}:
                break
            await asyncio.sleep(_RESUME_POLL_INTERVAL_S)
        else:
            pytest.fail(f"run {run_id} did not reach a terminal status: {row}")

    assert row["status"] == "completed", row
    checkpoint = row["checkpoint"]
    assert checkpoint["node_statuses"] == {"a": "success", "b": "success"}

    async with app_session_factory() as session:
        result = await session.execute(
            text(
                "SELECT COUNT(*) FROM outbox_events "
                "WHERE event_type = :t AND payload->>'run_id' = :r"
            ),
            {"t": "workflow_engine.workflow_run.resumed", "r": str(run_id)},
        )
        assert int(result.scalar_one()) == 1

    async with app_session_factory() as session:
        result = await session.execute(
            text("SELECT id FROM workflows WHERE id = :id"), {"id": str(workflow_id)}
        )
        assert result.scalar_one_or_none() is not None
