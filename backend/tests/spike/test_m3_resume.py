"""Test AC2 — reprise post-crash SIGKILL sans replay du producer.

Stratégie :

1. Lance le spike dans un **subprocess** avec ``CRASH_AFTER=producer`` →
   on attend ``returncode == -SIGKILL`` (= -9 sur Linux).
2. Lit le ``thread_id`` depuis ``backend/.spike-thread-id`` (écrit par le
   subprocess avant le crash).
3. Inspecte le checkpoint Postgres → confirme que ``producer_output`` est bien
   persisté (preuve : le producer a tourné, le checkpoint a été flushé).
4. Relance le spike avec ``SPIKE_RESUME_THREAD_ID=<uuid>`` en mode
   ``resume_existing=True`` → le workflow reprend après le checkpoint, traverse
   le quality_gate, le reviewer, et termine.
5. Vérifie que ``iterations == 1`` (= producer n'a PAS été ré-exécuté).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from spike.m3_langgraph import run_spike

pytestmark = pytest.mark.integration


def _python_executable() -> str:
    """Use the current interpreter so subprocesses inherit the venv."""
    return sys.executable


@pytest.fixture
def thread_id_file(tmp_path: Path) -> Path:
    return tmp_path / ".spike-thread-id"


async def test_resume_after_sigkill_does_not_replay_producer(
    checkpoint_dsn: str, thread_id_file: Path, force_mock_llm: None
) -> None:
    thread_id = str(uuid.uuid4())

    # ── 1. Subprocess that runs the spike up to producer then SIGKILLs itself. ──
    crash_script = textwrap.dedent(
        """
        import asyncio
        from spike.m3_langgraph import run_spike

        asyncio.run(run_spike(thread_id="{tid}"))
        """
    ).format(tid=thread_id)

    env = os.environ.copy()
    env["CRASH_AFTER"] = "producer"
    env["SPIKE_DATABASE_URL"] = checkpoint_dsn
    env["SPIKE_THREAD_ID_FILE"] = str(thread_id_file)
    env["ANTHROPIC_API_KEY"] = ""  # force MockLLM in subprocess too

    try:
        proc = subprocess.run(
            [_python_executable(), "-c", crash_script],
            env=env,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        # Without explicit handling pytest would surface a generic TimeoutExpired
        # with no spike output — useless for diagnosis. Surface stdout/stderr so
        # we know whether the subprocess hung pre-producer (DB connection, import)
        # or post-producer (the SIGKILL itself never fired).
        pytest.fail(
            f"crash subprocess exceeded 30s timeout — likely a spike regression.\n"
            f"stdout: {(exc.stdout or b'')!r}\nstderr: {(exc.stderr or b'')!r}"
        )
    assert proc.returncode == -signal.SIGKILL, (
        f"expected SIGKILL exit (-9), got returncode={proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    assert thread_id_file.read_text().strip() == thread_id, (
        "thread_id file should contain the same uuid the subprocess used"
    )

    # ── 2. Inspect Postgres: producer_output must be persisted at this point. ──
    async with AsyncPostgresSaver.from_conn_string(checkpoint_dsn) as saver:
        tuple_ = await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        assert tuple_ is not None, "no checkpoint persisted — producer did not flush before crash"
        channel_values = tuple_.checkpoint.get("channel_values", {})
        assert channel_values.get("producer_output"), (
            "producer_output absent from checkpoint — the producer may have crashed before its return"
        )
        assert channel_values.get("iterations") == 1
        assert not channel_values.get("reviewer_output"), (
            "reviewer_output should NOT be set yet — reviewer ran AFTER the crash point"
        )

    # ── 3. Resume from the same thread_id. Producer must NOT re-run. ──
    final = await run_spike(thread_id=thread_id, resume_existing=True)
    assert final.get("reviewer_output"), "reviewer should run after resume"
    assert final.get("iterations") == 1, (
        f"iterations must remain 1 after resume (no producer replay), got {final.get('iterations')}"
    )
    assert final.get("approved") is True
