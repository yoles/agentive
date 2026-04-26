"""Spike M3 LangGraph — Story 1.2 gating critique #1.

Workflow minimal **2 agents (Producer + Reviewer) + 1 quality gate** orchestré via
LangGraph 1.1.8 pour valider :

1. ✅ **Checkpointing Postgres natif** via :class:`AsyncPostgresSaver`
   (package ``langgraph-checkpoint-postgres``).
2. ✅ **Human-in-the-loop natif** via ``interrupt`` / ``Command(resume=...)``.
3. ✅ **Reprise post-crash** : kill -9 pendant ``producer`` → relance sur le même
   ``thread_id`` reprend après le checkpoint sans ré-exécuter le producer.

Si l'un des 3 piliers échoue, un ADR ``docs/decisions/m3-spike-result.md`` documente
le pivot avant de débloquer le Sprint 1 (Architecture lignes 220-241, AR3 ligne 149,
PRD lignes 465-468).

Code volontairement isolé dans ``backend/spike/`` (NOT ``features/m3_workflow_engine/``) :
le code complet du moteur M3 est implémenté à l'**Epic 4** (Stories 4.1-4.7) en
réutilisant les patterns validés ici.

**Usage** :

.. code-block:: bash

    make spike-m3              # Mode mock (CI default)
    make spike-m3-real         # Avec Anthropic réel si ANTHROPIC_API_KEY set
    make spike-m3-crash        # Crash au milieu du producer (CRASH_AFTER=producer)
    make spike-m3-resume       # Relance sur THREAD_ID écrit par le crash run
    make spike-m3-inspect THREAD_ID=<uuid>   # Inspecte le checkpoint Postgres

**Connection DB** : utilise ``settings.database_url_owner`` (rôle ``agentive_owner``)
parce que :class:`AsyncPostgresSaver.setup` requiert CREATE TABLE sur le schéma
``public``. Acceptable pour ce spike isolé ; en Epic 4, le setup se fera en
migration Alembic dédiée.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from pathlib import Path
from typing import Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agentive_backend.shared.config import settings

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [spike] %(message)s")

# Limit retry loops in HITL reject path → guards against infinite producer/gate cycles.
# Set to 3 so the test can exercise: (1) producer + reject, (2) producer + approve.
# A 3rd reject would auto-approve to avoid runaway loops.
MAX_ITERATIONS = 3

_DEFAULT_THREAD_ID_FILE = ".spike-thread-id"


def _thread_id_file() -> Path:
    """Resolve the thread-id persistence path at *call* time.

    Reading the env var lazily (vs. at import) lets tests patch
    ``SPIKE_THREAD_ID_FILE`` (e.g. via ``monkeypatch.setenv`` or a
    ``tmp_path`` fixture) after :mod:`spike.m3_langgraph` has been imported.
    """
    return Path(os.environ.get("SPIKE_THREAD_ID_FILE", _DEFAULT_THREAD_ID_FILE))


def _write_thread_id_atomically(thread_id: str) -> None:
    """Persist the thread id with atomic write semantics.

    ``Path.write_text`` is *not* atomic — a concurrent reader (e.g. the
    Makefile picking up ``.spike-thread-id`` right after a SIGKILL) can
    observe a truncated or empty file. ``os.replace`` is atomic on POSIX
    and on Windows, so writing to a sibling ``.tmp`` then replacing the
    target eliminates the race entirely.
    """
    target = _thread_id_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(thread_id)
    tmp.replace(target)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Mock LLM — deterministic, zero-cost, used by default in CI / dev.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class MockLLM:
    """Deterministic stand-in for an LLM call. Same input ⇒ same output."""

    def __init__(self, role: str) -> None:
        self.role = role

    async def acomplete(self, prompt: str, *, feedback: str | None = None) -> str:
        # Trivial latency to make scatter-gather parallelism observable.
        await asyncio.sleep(0.05)
        snippet = prompt[:60].replace("\n", " ")
        suffix = f" [feedback={feedback[:40]}]" if feedback else ""
        return f"[mock-{self.role}] {snippet}{suffix}"


def _make_llm(role: str) -> MockLLM | ChatAnthropic:
    """Pick the LLM backend based on env: real Anthropic if key set, else mock."""
    api_key = settings.anthropic_api_key
    if api_key and api_key.get_secret_value() not in ("", "change_me"):
        return ChatAnthropic(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            timeout=30,
            stop=None,
        )
    return MockLLM(role)


async def _llm_call(
    llm: MockLLM | ChatAnthropic, prompt: str, *, feedback: str | None = None
) -> str:
    """Uniform async call across MockLLM and ChatAnthropic."""
    if isinstance(llm, MockLLM):
        return await llm.acomplete(prompt, feedback=feedback)
    # Wrap user input in defense-against-prompt-injection delimiters (rule #8).
    full_prompt = f"<user_input>{prompt}</user_input>"
    if feedback:
        full_prompt += f"\n<feedback>{feedback}</feedback>"
    response = await llm.ainvoke(full_prompt)
    return str(response.content)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Graph state
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class WorkflowState(TypedDict, total=False):
    """Spike workflow state.

    No reducers ⇒ default last-write-wins semantics. ``iterations`` is incremented
    explicitly by :func:`producer_node` (``state.iterations + 1``).
    """

    task_input: str
    producer_output: str | None
    reviewer_output: str | None
    iterations: int
    feedback: str | None
    approved: bool | None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Crash injection helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Field that must be visible in the checkpoint before we trigger SIGKILL.
# Maps each ``CRASH_AFTER`` value to the channel that proves the previous node
# has been flushed to Postgres. Without this verification, a SIGKILL can race
# the async checkpoint write and the resume test sees a stale (step-0) state.
_CRASH_SENTINEL_FIELDS = {
    "producer": "producer_output",
    "quality_gate": "approved",
}

# Hard ceiling for the commit-confirmation poll. Generous on purpose so the
# test fails loudly (= real LangGraph regression) rather than flakes silently.
_COMMIT_POLL_TIMEOUT_S = 5.0
_COMMIT_POLL_INTERVAL_S = 0.05


async def _wait_for_committed_state(
    dsn: str,
    thread_id: str,
    sentinel_field: str,
    timeout_s: float = _COMMIT_POLL_TIMEOUT_S,
) -> None:
    """Block until ``sentinel_field`` is non-null in the latest Postgres checkpoint.

    Opens its own short-lived AsyncPostgresSaver so it works even when the
    caller's saver is not in scope (e.g. inside a graph node). Raises if the
    field never appears within ``timeout_s`` — a genuine LangGraph commit
    regression rather than the previously masked timing race.
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    deadline = time.monotonic() + timeout_s
    # The ``async with`` here is the *short-lived poller* — distinct from the
    # outer saver opened by ``run_spike``. The ``return`` below exits the
    # context manager, which awaits ``__aexit__`` (closing the pool) BEFORE
    # control returns to the caller. So by the time ``_maybe_crash_after``
    # triggers SIGKILL, this poller has already released its connection. The
    # crash only affects the *outer* saver in ``run_spike`` — which is the
    # whole point of the test (simulate brutal loss with no cleanup).
    async with AsyncPostgresSaver.from_conn_string(dsn) as poller:
        while time.monotonic() < deadline:
            tup = await poller.aget_tuple(config)
            channel_values = (tup.checkpoint or {}).get("channel_values", {}) if tup else {}
            if channel_values.get(sentinel_field) is not None:
                return
            await asyncio.sleep(_COMMIT_POLL_INTERVAL_S)
    raise RuntimeError(
        f"Timeout: '{sentinel_field}' never committed within {timeout_s}s "
        f"on thread {thread_id} — likely a LangGraph commit regression."
    )


async def _maybe_crash_after(prev_node: str, *, thread_id: str | None = None) -> None:
    """If CRASH_AFTER=<prev_node>, simulate a brutal post-checkpoint crash.

    Called at the **start** of a downstream node : at that point LangGraph has
    already flushed the previous node's writes to Postgres (checkpoint commit
    happens between nodes), so the crash mimics a real-world failure where
    work-in-progress is lost but committed state is durable.

    To eliminate the timing race between the async commit and the SIGKILL, we
    poll Postgres for the previous node's sentinel field before killing —
    deterministic proof that the commit has landed (vs. the previous arbitrary
    ``sleep(0.2)`` which could flake on slow CI).

    SIGKILL bypasses Python's ``atexit`` / ``finally`` — the goal is to simulate
    a brutal crash (OOM, hardware loss). Any cleanup the program would normally
    do is skipped, so we only see what was *flushed to Postgres before the crash*.
    """
    crash_target = os.environ.get("CRASH_AFTER")
    if not crash_target or crash_target != prev_node:
        return

    sentinel = _CRASH_SENTINEL_FIELDS.get(prev_node)
    if sentinel and thread_id:
        await _wait_for_committed_state(_checkpoint_dsn(), thread_id, sentinel)

    logger.error(
        "CRASH_AFTER=%s reached at start of next node — sending SIGKILL to PID=%d",
        prev_node,
        os.getpid(),
    )
    # The thread_id file was already written at the start of run_spike().
    os.kill(os.getpid(), signal.SIGKILL)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Nodes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def producer_node(state: WorkflowState) -> dict[str, object]:
    """First agent: produces a draft from the task input + optional feedback."""
    task_input = state.get("task_input")
    if not task_input:
        raise ValueError("producer_node requires a non-empty 'task_input' in the initial state")

    iteration = state.get("iterations", 0)
    feedback = state.get("feedback")
    logger.info(
        "producer iteration=%d feedback=%s task_len=%d",
        iteration + 1,
        bool(feedback),
        len(task_input),
    )

    llm = _make_llm("producer")
    output = await _llm_call(llm, task_input, feedback=feedback)
    logger.info("producer output_len=%d", len(output))

    return {
        "producer_output": output,
        "iterations": iteration + 1,
        "feedback": None,  # consumed
        "approved": None,  # reset for the upcoming gate
    }


async def quality_gate_node(
    state: WorkflowState, config: RunnableConfig
) -> Command[Literal["producer", "reviewer"]]:
    """HITL quality gate: pause the graph and wait for human decision.

    The :func:`interrupt` function exposes a payload to the caller and returns
    the value passed to ``Command(resume=...)`` once the workflow is resumed.

    Auto-approve safety net: if MAX_ITERATIONS reached, force-route to reviewer
    so the test never loops infinitely on a stubborn rejection.
    """
    # Crash here if requested. ``_maybe_crash_after`` polls Postgres until the
    # producer's commit is durable before triggering SIGKILL — deterministic
    # vs. the prior arbitrary sleep that could flake on slow CI.
    thread_id = config.get("configurable", {}).get("thread_id")
    await _maybe_crash_after("producer", thread_id=thread_id)

    iteration = state.get("iterations", 0)
    logger.info("quality_gate iteration=%d (waiting for human decision)", iteration)

    if iteration >= MAX_ITERATIONS:
        logger.warning(
            "quality_gate auto-approving after MAX_ITERATIONS=%d (force route to reviewer)",
            MAX_ITERATIONS,
        )
        return Command(goto="reviewer", update={"approved": True})

    decision = interrupt(
        {
            "question": "Approve the producer draft?",
            "iteration": iteration,
            "draft": state.get("producer_output"),
        }
    )

    # The caller must resume with ``Command(resume={"approved": bool, "feedback"?: str})``.
    # Anything else (None, string, missing key) is a contract violation — fail loud
    # rather than silently routing to the producer (which would consume retries
    # until MAX_ITERATIONS without any operator-visible signal).
    if not isinstance(decision, dict) or "approved" not in decision:
        raise ValueError(
            f"quality_gate resume payload must be a dict with an 'approved' key, "
            f"got {type(decision).__name__}={decision!r}"
        )

    if decision["approved"]:
        return Command(goto="reviewer", update={"approved": True, "feedback": None})

    raw_feedback = decision.get("feedback")
    feedback = str(raw_feedback) if raw_feedback else "rework needed"
    logger.info("quality_gate REJECTED (feedback_len=%d) → re-route to producer", len(feedback))
    return Command(goto="producer", update={"feedback": feedback, "approved": False})


async def reviewer_node(state: WorkflowState, config: RunnableConfig) -> dict[str, object]:
    """Final agent: reviews the producer draft and produces a final output."""
    # Crash here if requested → quality_gate decision is already committed.
    thread_id = config.get("configurable", {}).get("thread_id")
    await _maybe_crash_after("quality_gate", thread_id=thread_id)

    logger.info("reviewer producer_output_len=%d", len(state.get("producer_output") or ""))
    llm = _make_llm("reviewer")
    output = await _llm_call(llm, state.get("producer_output") or "")
    logger.info("reviewer output_len=%d", len(output))

    return {"reviewer_output": output}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Graph construction
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def build_graph(checkpointer: AsyncPostgresSaver) -> object:
    """Compile the spike graph with the supplied checkpointer.

    Returns the compiled graph; type-erased to ``object`` to avoid leaking
    LangGraph's generics through the public API of this spike.
    """
    builder = StateGraph(WorkflowState)
    builder.add_node("producer", producer_node)
    builder.add_node("quality_gate", quality_gate_node)
    builder.add_node("reviewer", reviewer_node)

    builder.add_edge(START, "producer")
    builder.add_edge("producer", "quality_gate")
    # quality_gate uses Command(goto=...) for routing, so no static edge needed
    # from quality_gate. Both producer and reviewer are valid post-gate targets.
    builder.add_edge("reviewer", END)

    return builder.compile(checkpointer=checkpointer)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Postgres connection helper
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _checkpoint_dsn() -> str:
    """Return a plain ``postgresql://`` DSN for AsyncPostgresSaver.

    Settings exposes ``database_url_owner`` as ``postgresql+psycopg://...``
    (SQLAlchemy form). LangGraph / psycopg want the canonical scheme.
    Allow override via ``SPIKE_DATABASE_URL`` for testcontainers-driven tests.
    """
    if override := os.environ.get("SPIKE_DATABASE_URL"):
        return override
    return str(settings.database_url_owner).replace("postgresql+psycopg://", "postgresql://", 1)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def run_spike(
    thread_id: str | None = None,
    *,
    auto_approve: bool = True,
    resume_existing: bool = False,
    initial_input: dict[str, object] | None = None,
) -> dict[str, object]:
    """Run the spike workflow end-to-end and return the terminal state.

    Args:
        thread_id: existing thread to resume. Generated if ``None``.
        auto_approve: if True (default), transparently resume past every HITL
            interrupt by passing ``Command(resume={"approved": True})``. Tests
            exercising the rejection path drive the graph manually instead.
        resume_existing: if True, the function passes ``None`` as the first
            invoke input so LangGraph picks up from the existing checkpoint
            (used after a SIGKILL crash). If False, a fresh ``task_input`` is
            sent and the workflow starts from ``START``.
        initial_input: override the default task input.
    """
    thread_id = thread_id or str(uuid.uuid4())
    if not resume_existing:
        # On a fresh run we (over)write the thread id so the Makefile / test
        # harness can pick it up after a SIGKILL. On resume the file is already
        # authoritative — keep the original crashed-run trace intact.
        _write_thread_id_atomically(thread_id)
    logger.info(
        "spike start thread_id=%s resume_existing=%s auto_approve=%s",
        thread_id,
        resume_existing,
        auto_approve,
    )

    config = {"configurable": {"thread_id": thread_id}}
    started = time.monotonic()

    async with AsyncPostgresSaver.from_conn_string(_checkpoint_dsn()) as checkpointer:
        await checkpointer.setup()
        graph = build_graph(checkpointer)

        if resume_existing:
            # ``None`` ⇒ LangGraph resumes the saved state without overwriting it.
            # The producer is NOT re-executed — the durable proof is the resulting
            # ``iterations`` count (must remain at the value committed pre-crash).
            result = await graph.ainvoke(None, config)
            logger.info(
                "producer_replayed=False thread_id=%s (resumed from checkpoint)",
                thread_id,
            )
        else:
            first_input = initial_input or {"task_input": "Write a haiku about resilience."}
            result = await graph.ainvoke(first_input, config)

        # Resume past every interrupt until the graph reaches END.
        # Defensive iteration cap — MAX_ITERATIONS handles the in-graph retry
        # logic; the +2 absorbs interrupts emitted at boundaries. Past this,
        # a hung loop is a real LangGraph regression : fail loud rather than
        # silently spin until pytest's outer timeout.
        for _ in range(MAX_ITERATIONS + 2):
            if not result.get("__interrupt__"):
                break
            if not auto_approve:
                break
            result = await graph.ainvoke(Command(resume={"approved": True}), config)
        else:
            raise RuntimeError(
                f"run_spike: interrupt loop did not terminate after "
                f"{MAX_ITERATIONS + 2} resume cycles on thread {thread_id} — "
                "possible LangGraph regression on quality_gate auto-approve."
            )

    duration = time.monotonic() - started
    logger.info("spike complete thread_id=%s total_duration_s=%.3f", thread_id, duration)
    return dict(result)


def main() -> None:
    resume_thread = os.environ.get("SPIKE_RESUME_THREAD_ID")
    asyncio.run(run_spike(thread_id=resume_thread, resume_existing=bool(resume_thread)))


if __name__ == "__main__":
    main()
