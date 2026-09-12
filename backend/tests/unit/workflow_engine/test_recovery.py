"""Unit tests — :class:`WorkflowRecoveryWorker` (Story 4.2 T7.4)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agentive_backend.features.workflow_engine.domain.error_policy import MAX_RUNTIME_RETRIES
from agentive_backend.features.workflow_engine.engine.agent_node import NODE_TIMEOUT_S
from agentive_backend.features.workflow_engine.recovery import (
    _DEFAULT_RETRY_BASE_DELAY_S,
    _DEFAULT_RETRY_MAX_DELAY_S,
    _HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT,
    _MAX_PROVIDER_CHAIN_LEN,
    _ROUTING_ESCALATION_TIMEOUT_S_DEFAULT,
    DEFAULT_STALE_THRESHOLD_S,
    MAX_RECOVERY_ATTEMPTS,
    WorkflowRecoveryWorker,
    _worst_case_backoff_s,
    derive_stale_threshold_s,
)
from agentive_backend.shared.config import Settings
from agentive_backend.shared.exceptions import NotFoundError
from agentive_backend.shared.repositories.base import BaseRepo

#: What `_WORST_CASE_BACKOFF_S` used to be: the backoff a node accumulates
#: at the DEFAULT delay settings (1 + 2 + 4). Now computed rather than
#: written down, because the production value is computed too.
_DEFAULT_BACKOFF_S = _worst_case_backoff_s(
    base_delay_s=_DEFAULT_RETRY_BASE_DELAY_S, max_delay_s=_DEFAULT_RETRY_MAX_DELAY_S
)

#: Story 4.7 T5.6 — the handoff-summary term at its default, added once
#: (never multiplied by `1 + MAX_RUNTIME_RETRIES`: a node summarizes ONCE
#: regardless of how many times its own completion retried).
_DEFAULT_HANDOFF_S = _HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT * _MAX_PROVIDER_CHAIN_LEN


@pytest.fixture
def event_publish_mock(monkeypatch: pytest.MonkeyPatch) -> Iterator[AsyncMock]:
    pub_mock = AsyncMock(return_value=uuid4())
    notify_mock = AsyncMock(return_value=None)
    import agentive_backend.features.workflow_engine.recovery as recovery_module

    monkeypatch.setattr(recovery_module, "publish", pub_mock)
    monkeypatch.setattr(recovery_module, "notify_best_effort", notify_mock)
    yield pub_mock


def _stale_run(*, checkpoint: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        workflow_id=uuid4(),
        correlation_id=uuid4(),
        status="running",
        checkpoint=checkpoint,
        started_at=datetime.now(UTC),
        last_checkpoint_at=None,
    )


def _workflow() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), status="active", dag={"nodes": [], "edges": []})


def _make_worker(*, stale_runs: list[SimpleNamespace]) -> tuple[WorkflowRecoveryWorker, AsyncMock]:
    """Build a worker with all three internally-constructed repos replaced
    by mocks (assigned post-``__init__`` — simpler than monkeypatching the
    repo classes the worker imports at module scope)."""
    session_mock = AsyncMock()

    @asynccontextmanager
    async def _with_tenant(_tenant_id: Any) -> AsyncIterator[AsyncMock]:
        yield session_mock

    workflow_run_repo = AsyncMock()
    workflow_run_repo.with_tenant = _with_tenant
    workflow_run_repo.claim_stale_running = AsyncMock(return_value=stale_runs)

    workflow_repo = AsyncMock()
    workflow_repo.require_by_id = AsyncMock(return_value=_workflow())

    execution_service = MagicMock()
    execution_service._resume_run = AsyncMock()

    worker = WorkflowRecoveryWorker(
        workflow_execution_service=execution_service,
        session_factory=MagicMock(),
        stale_threshold_s=120.0,
    )
    worker._workflow_run_repo = workflow_run_repo
    worker._workflow_repo = workflow_repo
    worker._template_repo = AsyncMock()
    return worker, workflow_run_repo


@pytest.mark.asyncio
async def test_run_once_resumes_stale_run_and_publishes_event(
    event_publish_mock: AsyncMock,
) -> None:
    run = _stale_run(checkpoint={"last_node_id": "a"})
    worker, workflow_run_repo = _make_worker(stale_runs=[run])

    summary = await worker.run_once()
    await asyncio.sleep(0)  # let the fire-and-forget resume task get scheduled

    assert summary.resume_triggered_count == 1
    assert summary.failed_count == 0
    workflow_run_repo.claim_stale_running.assert_awaited_once()

    event_type = event_publish_mock.await_args.args[0]
    assert event_type == "workflow_engine.workflow_run.resumed"
    event_payload = event_publish_mock.await_args.args[1]
    assert event_payload.run_id == run.id
    assert event_payload.resumed_from_node_id == "a"

    worker._workflow_execution_service._resume_run.assert_called_once()


@pytest.mark.asyncio
async def test_run_once_no_stale_runs_resumes_nothing() -> None:
    """No `running` row past the staleness threshold — `claim_stale_running`'s
    SQL filter (T2.3) already excludes recent/terminal runs; this covers the
    empty-result path through the worker's own logic."""
    worker, workflow_run_repo = _make_worker(stale_runs=[])

    summary = await worker.run_once()

    assert summary.resume_triggered_count == 0
    assert summary.failed_count == 0
    workflow_run_repo.claim_stale_running.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_once_passes_stale_threshold_to_the_query() -> None:
    """The cutoff must be ``now() - stale_threshold_s`` — asserting only that
    the kwarg EXISTS would still pass with the sign inverted (which would
    claim every healthy run instead of the orphaned ones)."""
    worker, workflow_run_repo = _make_worker(stale_runs=[])

    before = datetime.now(UTC)
    await worker.run_once()
    after = datetime.now(UTC)

    older_than = workflow_run_repo.claim_stale_running.await_args.kwargs["older_than"]
    assert before - timedelta(seconds=120.0) <= older_than <= after - timedelta(seconds=120.0)


@pytest.mark.asyncio
async def test_run_once_resumed_from_node_id_none_when_checkpoint_missing(
    event_publish_mock: AsyncMock,
) -> None:
    """A run that crashed before its first checkpoint sync has no
    `last_node_id` — best-effort `None`, not an error."""
    run = _stale_run(checkpoint=None)
    worker, _workflow_run_repo = _make_worker(stale_runs=[run])

    await worker.run_once()

    event_payload = event_publish_mock.await_args.args[1]
    assert event_payload.resumed_from_node_id is None


@pytest.mark.asyncio
async def test_run_once_one_failing_run_does_not_block_the_rest(
    event_publish_mock: AsyncMock,
) -> None:
    """`MemoryArchivalWorker`-style per-item resilience — one run's
    resolution failure (e.g. its workflow vanished) must not prevent the
    rest of the sweep from resuming."""
    bad_run = _stale_run()
    good_run = _stale_run()
    worker, _workflow_run_repo = _make_worker(stale_runs=[bad_run, good_run])

    call_count = 0

    async def _require_by_id(_workflow_id: Any, **_kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("workflow lookup failed")
        return _workflow()

    worker._workflow_repo.require_by_id = AsyncMock(side_effect=_require_by_id)

    summary = await worker.run_once()

    assert summary.resume_triggered_count == 1
    assert summary.failed_count == 1


@pytest.mark.asyncio
async def test_start_twice_raises_runtime_error() -> None:
    worker, _repo = _make_worker(stale_runs=[])
    await worker.start()
    try:
        with pytest.raises(RuntimeError, match="called twice"):
            await worker.start()
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    worker, _repo = _make_worker(stale_runs=[])
    await worker.start()
    await worker.stop()
    await worker.stop()  # must not raise


# ─── Intent gap 1 — claim-based sweep, bounded attempts ────────────────


def test_stale_threshold_exceeds_the_worst_case_single_node_duration() -> None:
    """`NODE_TIMEOUT_S` is applied PER PROVIDER by `LLMRouter.complete`, so a
    healthy node on the 2-provider production chain can take
    `2 * NODE_TIMEOUT_S` before it checkpoints. A threshold at or below that
    classifies live runs as orphaned and duplicates them."""
    assert DEFAULT_STALE_THRESHOLD_S > NODE_TIMEOUT_S * 2


def test_stale_threshold_covers_a_decision_point_node_with_its_escalation() -> None:
    """Story 4.3 point 9 — the regression this story called its least visible.

    A routing decision point pays its own `NODE_TIMEOUT_S` AND an escalation
    call, each per provider on a 2-provider chain. The previous assertion
    (`> NODE_TIMEOUT_S * 2`, i.e. `> 120`) held just as well at 300.0 as at
    375.0, so nothing in the suite would have noticed the escalation term
    being dropped — while the symptom (healthy runs reclaimed in a loop by
    the recovery worker until `MAX_RECOVERY_ATTEMPTS`, then abandoned
    `failed`) looks like engine instability, not like a constant.
    """
    assert (
        pytest.approx(
            (
                (NODE_TIMEOUT_S + _ROUTING_ESCALATION_TIMEOUT_S_DEFAULT)
                * 2
                * (1 + MAX_RUNTIME_RETRIES)
                + _DEFAULT_BACKOFF_S
                + _DEFAULT_HANDOFF_S
            )
            * 2.5
        )
        == DEFAULT_STALE_THRESHOLD_S
    )


def test_stale_threshold_covers_a_node_that_exhausts_its_retry_budget() -> None:
    """Story 4.6 T8.4 — the regression this story would otherwise have
    INTRODUCED, and the most expensive one on the list.

    `error_policy.on_timeout = "retry_with_backoff"` makes a node re-run the
    whole provider chain up to `MAX_RUNTIME_RETRIES` times, writing no
    checkpoint throughout. The pre-4.6 formula assumed exactly one traversal,
    so a healthy node on its third retry became "orphaned" — claimed by the
    sweep and executed a SECOND time in parallel on the same `thread_id`.
    Duplicate billed LLM calls, interleaved checkpoints, duplicate events,
    and nothing in the logs saying why.

    Asserted as a strict lower bound rather than only through the exact
    formula above, so that changing the margin cannot quietly drop the
    retry term.
    """
    worst_case_node_s = (NODE_TIMEOUT_S + _ROUTING_ESCALATION_TIMEOUT_S_DEFAULT) * 2 * (
        1 + MAX_RUNTIME_RETRIES
    ) + _DEFAULT_BACKOFF_S
    assert worst_case_node_s < DEFAULT_STALE_THRESHOLD_S


def test_runtime_retry_cap_stays_below_the_schema_ceiling() -> None:
    """T8.5 — an ASSUMED divergence with Story 2.2's `ErrorPolicy.max_retries
    (le=10)`, which must stay assumed rather than drift into equality.

    Raising the runtime cap to the schema's 10 pushes the threshold above an
    hour, which is a crashed run sitting unexamined for an hour — the
    recovery worker's whole reason for existing, defeated by one template's
    configuration.
    """
    from agentive_backend.features.workflow_engine.domain.error_policy import (
        MAX_ERROR_POLICY_RETRIES,
    )

    assert MAX_RUNTIME_RETRIES < MAX_ERROR_POLICY_RETRIES
    # 30 minutes is the comfort bound this story committed to; "several
    # hours" was the stated line for capping retries instead.
    assert DEFAULT_STALE_THRESHOLD_S < 30 * 60


def test_recovery_escalation_default_matches_the_settings_default() -> None:
    """`recovery.py` hardcodes the escalation timeout instead of importing
    `settings` (a static fallback for the constructor default, like
    `NODE_TIMEOUT_S`). Duplicating a constant without a parity test is the
    exact thing the 4.2 review reproached `PlaygroundService` for, and the
    story's own trap #5 makes this test mandatory: if the two drift, the
    recovery worker sizes its window against a timeout nobody uses.
    """
    assert Settings().routing_escalation_timeout_s == _ROUTING_ESCALATION_TIMEOUT_S_DEFAULT


def test_recovery_handoff_summary_timeout_default_matches_the_settings_default() -> None:
    """Story 4.7 T5.6 — same parity requirement as the escalation timeout
    above, for the mirror this story adds."""
    assert Settings().workflow_handoff_summary_timeout_s == _HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT


def test_recovery_retry_delay_defaults_match_the_settings_defaults() -> None:
    """Same parity requirement as the escalation timeout above, extended to
    the two delays the derivation now reads (review lot 7, P-P). These sit
    in `recovery.py` only to size the STATIC fallback; drifting from the
    settings defaults would make that fallback describe a deployment nobody
    runs."""
    settings = Settings()
    assert settings.workflow_retry_base_delay_s == _DEFAULT_RETRY_BASE_DELAY_S
    assert settings.workflow_retry_max_delay_s == _DEFAULT_RETRY_MAX_DELAY_S


def test_the_module_default_is_the_derivation_at_the_default_settings() -> None:
    """The one place in the suite that pins the NUMBER, independently.

    This assertion used to call `derive_stale_threshold_s` with the very
    constants the production line feeds it and compare to the production
    line's result — `f(x) == f(x)`, which `return 0.0` satisfies (review lot
    10, T3). Every other threshold test re-derives its expectation through
    the same formula, so a change inside `_worst_case_backoff_s` or to the
    margin moved both sides together and nothing in the suite moved.

    A literal breaks that symmetry: ((60 + 15) x 2 x 4 + (1 + 2 + 4) + 20 x 2) x 2,5.
    Touch the formula, the margin, the chain length, the retry cap or the
    handoff-summary timeout (Story 4.7 T5.6), and a human has to come here
    and re-justify the number — which is the point, because this number is
    how long a genuinely crashed run stays unexamined.
    """
    assert DEFAULT_STALE_THRESHOLD_S == 1617.5


def test_the_threshold_grows_with_the_configured_retry_delay() -> None:
    """Review lot 7 (P-P), the half that did NOT break — recorded as such.

    At the two delays' `le` ceilings the old fixed window still covered the
    node (1020 s worst case inside 1517.5 s): the x2,5 margin absorbed them.
    So the delays never produced the double execution, and the claim that
    "the ceiling keeps the derivation true" was wrong about the mechanism
    rather than about the outcome — what held was the margin, eroded to
    x1,49 in the process.

    Story 4.7 T5.6 adds a FIXED term (the handoff-summary timeout, not scaled
    by `1 + MAX_RUNTIME_RETRIES`) to both sides of this comparison. Because
    the numerator's own x2,5 margin amplifies that fixed addition while the
    raw `worst_case_node_s` denominator does not, the erosion is now x1,53 —
    still comfortably below the x2,5 the formula nominally grants, but no
    longer x1,49. Asserting the margin, not just the ordering, is what would
    catch a future ceiling (or handoff timeout) raised past what it can absorb.
    """
    raised = derive_stale_threshold_s(
        base_delay_s=60.0,
        max_delay_s=300.0,
        escalation_timeout_s=15.0,
        handoff_summary_timeout_s=_HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT,
    )
    worst_case_node_s = (
        (NODE_TIMEOUT_S + 15.0) * 2 * (1 + MAX_RUNTIME_RETRIES)
        + (60.0 + 120.0 + 240.0)
        + _DEFAULT_HANDOFF_S
    )
    default_worst_case_node_s = (
        (NODE_TIMEOUT_S + _ROUTING_ESCALATION_TIMEOUT_S_DEFAULT) * 2 * (1 + MAX_RUNTIME_RETRIES)
        + _DEFAULT_BACKOFF_S
        + _DEFAULT_HANDOFF_S
    )
    assert raised > DEFAULT_STALE_THRESHOLD_S
    # The derived window restores the full margin the fixed one had spent.
    assert raised / worst_case_node_s == pytest.approx(
        DEFAULT_STALE_THRESHOLD_S / default_worst_case_node_s
    )
    assert DEFAULT_STALE_THRESHOLD_S / worst_case_node_s < 1.6


def test_the_threshold_grows_with_the_configured_escalation_timeout() -> None:
    """Review lot 7 (P-P), the half that DID break, and the only term with
    no ceiling at all.

    The derivation read a hardcoded 15.0 whatever
    `AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S` was set to, so raising it moved
    the node's real worst case and not the window watching it. Past ~130 s
    the window stopped covering the node: at 150 s a healthy node was
    classified orphaned, claimed by the sweep, and executed a SECOND time in
    parallel on the same `thread_id`.
    """
    worst_case_at_150 = (NODE_TIMEOUT_S + 150.0) * 2 * (
        1 + MAX_RUNTIME_RETRIES
    ) + _DEFAULT_BACKOFF_S
    assert worst_case_at_150 > DEFAULT_STALE_THRESHOLD_S  # the old, fixed window: not covering
    derived = derive_stale_threshold_s(
        base_delay_s=1.0,
        max_delay_s=30.0,
        escalation_timeout_s=150.0,
        handoff_summary_timeout_s=_HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT,
    )
    assert derived > worst_case_at_150  # the derived one: covering


def test_the_escalation_timeout_is_bounded_by_config() -> None:
    """A term multiplied by ~20 inside the threshold cannot be unbounded.
    Deriving the window makes any value SAFE, but not free: 600 s would put
    the sweep's window past three hours, so a crashed run sits unexamined
    that long. Both halves are needed — `le=60.0` is the second."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(AGENTIVE_ROUTING_ESCALATION_TIMEOUT_S=600.0)  # type: ignore[call-arg]
    # At the WORST legal combination of all three the window is ~57 min, not
    # the 30 the default commits to — and that is forced, not chosen: those
    # settings let a healthy node run ~23 min without checkpointing, so no
    # honest detector can be faster. The 30-minute promise is a property of
    # the DEFAULTS (asserted above on `DEFAULT_STALE_THRESHOLD_S`); what the
    # ceilings owe is that the worst case stays bounded and knowable.
    at_ceilings = derive_stale_threshold_s(
        base_delay_s=60.0,
        max_delay_s=300.0,
        escalation_timeout_s=60.0,
        handoff_summary_timeout_s=_HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT,
    )
    assert at_ceilings < 60 * 60


def test_worst_case_backoff_honours_the_max_delay_clamp() -> None:
    """`backoff_delay_s` clamps every wait to `max_s`; a derivation that
    summed the unclamped exponential would overshoot the threshold and let a
    genuinely crashed run sit far longer than it needs to."""
    clamped = _worst_case_backoff_s(base_delay_s=100.0, max_delay_s=10.0)
    assert clamped == 10.0 * MAX_RUNTIME_RETRIES


@pytest.mark.asyncio
async def test_run_once_abandons_a_run_past_the_attempt_cap(
    event_publish_mock: AsyncMock,
) -> None:
    """A run claimed more than `MAX_RECOVERY_ATTEMPTS` times with no node
    progress in between is poison — it must be marked `error`, not resumed
    again for another round of billed LLM calls."""
    run = _stale_run(checkpoint={"recovery_attempts": MAX_RECOVERY_ATTEMPTS + 1})
    worker, workflow_run_repo = _make_worker(stale_runs=[run])

    summary = await worker.run_once()

    assert summary.abandoned_count == 1
    assert summary.resume_triggered_count == 0
    worker._workflow_execution_service._resume_run.assert_not_called()

    status_kwargs = workflow_run_repo.update_status.await_args.kwargs
    assert status_kwargs["status"] == "error"

    event_type = event_publish_mock.await_args.args[0]
    assert event_type == "workflow_engine.workflow_run.failed"


@pytest.mark.asyncio
async def test_run_once_still_resumes_at_the_attempt_cap(
    event_publish_mock: AsyncMock,
) -> None:
    """Boundary — the cap is exclusive: attempt N == MAX is the last one that
    still gets a resume."""
    run = _stale_run(checkpoint={"recovery_attempts": MAX_RECOVERY_ATTEMPTS})
    worker, _repo = _make_worker(stale_runs=[run])

    summary = await worker.run_once()
    await asyncio.sleep(0)

    assert summary.resume_triggered_count == 1
    assert summary.abandoned_count == 0
    worker._workflow_execution_service._resume_run.assert_called_once()


@pytest.mark.asyncio
async def test_run_once_tolerates_a_non_numeric_attempt_counter(
    event_publish_mock: AsyncMock,
) -> None:
    """`checkpoint` is free-form JSONB — a garbage counter must degrade to 0
    (resume normally), never raise and abort the sweep."""
    run = _stale_run(checkpoint={"recovery_attempts": "lots"})
    worker, _repo = _make_worker(stale_runs=[run])

    summary = await worker.run_once()
    await asyncio.sleep(0)

    assert summary.resume_triggered_count == 1
    assert summary.failed_count == 0


# ─── Intent gap 2 — shutdown cancels instead of burying ────────────────


@pytest.mark.asyncio
async def test_stop_cancels_inflight_resume_tasks() -> None:
    """The lifespan closes the checkpointer right after `stop()`. Any resume
    still running at that point would raise on a closed connection and be
    marked terminal `error` — so `stop()` must cancel them first, leaving the
    row `running` for the next process's sweep."""
    worker, _repo = _make_worker(stale_runs=[])
    started = asyncio.Event()

    async def _never_ends() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(_never_ends())
    worker._resume_tasks.add(task)
    await started.wait()

    await worker.stop()

    assert task.cancelled()


# ─── Story 4.6 T12.7 — the recovery sweep must not fight run control ───
#
# The two tests that lived here asserted a property of the CLAIM QUERY, and
# neither reached it: one compiled a `WorkflowRun.status == "running"` clause
# it had built in its own body, the other decorated a `SimpleNamespace` with
# a `control_signal` that `recovery.py` never reads, while
# `claim_stale_running` was an `AsyncMock`. Both stayed green under the exact
# regressions their docstrings claimed to guard.
#
# The guarantee is a property of the STATEMENT, so it now lives where the
# statement is built — `tests/unit/repositories/test_workflow_repo.py`:
#
#   * `..._claim_stale_running_filters_on_running_only` — widening the filter
#     to `paused` fails it;
#   * `..._claim_stale_running_ignores_a_pending_control_signal` — adding a
#     `control_signal IS NULL` predicate fails it.
#
# Both verified against those two regressions before this note was written
# (review lot 11).


# ─── Revue du 2026-09-12 — P-15 : le paramètre handoff n'a plus de défaut ───


def test_the_handoff_timeout_is_a_required_argument_like_its_three_siblings() -> None:
    """It shipped with a default, and a default on an argument of THIS
    function is precisely the failure it exists to prevent: a future caller
    who forgets to wire it silently derives a window from 20.0 s instead of
    the deployed value, and a window that is too short is the double-execution
    bug this whole derivation was written for (Story 4.6 T8.4). The
    compatibility argument did not hold either — the only production caller
    (`app/lifespan.py`) was edited in the same change that introduced it."""
    with pytest.raises(TypeError, match="handoff_summary_timeout_s"):
        derive_stale_threshold_s(  # type: ignore[call-arg]
            base_delay_s=1.0, max_delay_s=30.0, escalation_timeout_s=15.0
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_delay_s": -1.0},
        {"max_delay_s": -1.0},
        {"escalation_timeout_s": -1.0},
        {"handoff_summary_timeout_s": -1000.0},
        {"handoff_summary_timeout_s": float("inf")},
        {"escalation_timeout_s": float("nan")},
    ],
)
def test_a_value_outside_the_sane_domain_is_refused(kwargs: dict[str, float]) -> None:
    """The invariant this function makes true by construction is only true
    over a sane domain. `Settings` carries `gt=0`/`allow_inf_nan=False` on all
    four inputs, but this is a PUBLIC function called directly (tests
    included), and a negative value yields a NEGATIVE threshold: every
    `running` run is instantly classified orphaned and re-claimed, i.e.
    generalised double execution — silently."""
    base: dict[str, float] = {
        "base_delay_s": 1.0,
        "max_delay_s": 30.0,
        "escalation_timeout_s": 15.0,
        "handoff_summary_timeout_s": _HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT,
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match="finite and >= 0"):
        derive_stale_threshold_s(**base)  # type: ignore[arg-type]


def test_the_handoff_timeout_default_matches_the_settings_default() -> None:
    """P-16 — `.env.example` and `Settings` are the deployed truth; this
    module's static fallback must not drift from them."""
    assert Settings().workflow_handoff_summary_timeout_s == _HANDOFF_SUMMARY_TIMEOUT_S_DEFAULT


# ─── Story 4.8 AC3, extended by code review (BS1) — batch resolution ───


def _template_row(template_id: Any) -> SimpleNamespace:
    return SimpleNamespace(id=template_id, archetype="producteur", config={})


def _dag_with(*template_ids: Any) -> dict[str, Any]:
    return {
        "nodes": [
            {"node_id": f"n{index}", "agent_template_id": str(tid)}
            for index, tid in enumerate(template_ids)
        ],
        "edges": [],
    }


@pytest.mark.asyncio
async def test_load_templates_resolves_every_node_in_one_batch_query() -> None:
    """AC3's *When* — "les templates référencés sont résolus" — names no
    site, and this was the third resolver. It is also the one where N+1 hurt
    most: `run_once` sweeps runs SEQUENTIALLY, so a 100-node stale run spent
    100 round trips blocking every other run queued behind it.

    Asserted on `list_by_ids`, so a regression back to N `require_by_id`
    calls fails here rather than passing quietly at the same speed.
    """
    worker, _ = _make_worker(stale_runs=[])
    first, second = uuid4(), uuid4()
    worker._template_repo.list_by_ids = AsyncMock(
        return_value={first: _template_row(first), second: _template_row(second)}
    )

    templates = await worker._load_templates(_dag_with(first, second))

    worker._template_repo.list_by_ids.assert_awaited_once()
    assert list(worker._template_repo.list_by_ids.await_args.args[0]) == [first, second]
    assert set(templates) == {"n0", "n1"}
    worker._template_repo.require_by_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_templates_deduplicates_a_shared_template() -> None:
    """Two nodes on the same template resolve to the same row — the map is
    keyed by template id, not by node."""
    worker, _ = _make_worker(stale_runs=[])
    shared = uuid4()
    row = _template_row(shared)
    worker._template_repo.list_by_ids = AsyncMock(return_value={shared: row})

    templates = await worker._load_templates(_dag_with(shared, shared))

    assert templates["n0"] is row
    assert templates["n1"] is row


@pytest.mark.asyncio
async def test_load_templates_still_raises_not_found_on_a_missing_template() -> None:
    """The divergence this copy carries on PURPOSE is preserved.

    The shared `service._load_templates` raises `InternalError` (500) for the
    same condition; this one raises `NotFoundError`, because a recovery
    worker has no HTTP caller to mislead — a decision Story 4.6 made and 4.8
    deliberately did not revisit. Batching and FOLDING the three copies were
    separable, and only the first was in AC3's scope: this test is what keeps
    the second from happening by accident.
    """
    worker, _ = _make_worker(stale_runs=[])
    missing = uuid4()
    worker._template_repo.list_by_ids = AsyncMock(return_value={})

    with pytest.raises(NotFoundError) as exc_info:
        await worker._load_templates(_dag_with(missing))

    assert exc_info.value.context["template_id"] == str(missing)


@pytest.mark.asyncio
async def test_load_templates_reports_the_first_missing_in_declaration_order() -> None:
    """Which node is reported must not depend on Postgres' return order."""
    worker, _ = _make_worker(stale_runs=[])
    present, first_missing, second_missing = uuid4(), uuid4(), uuid4()
    worker._template_repo.list_by_ids = AsyncMock(return_value={present: _template_row(present)})

    with pytest.raises(NotFoundError) as exc_info:
        await worker._load_templates(_dag_with(present, first_missing, second_missing))

    assert exc_info.value.context["template_id"] == str(first_missing)


@pytest.mark.asyncio
async def test_load_templates_not_found_matches_the_canonical_lookup_or_404_shape() -> None:
    """The batch rewrite re-spells, in feature code, the message
    `require_by_id` used to produce through `BaseRepo._require_found` — audit
    A-07's single spelling of lookup-or-404. Re-spelling beats reaching into
    a private repo helper from a feature module, but only if the two cannot
    drift, which is what this compares.
    """
    worker, _ = _make_worker(stale_runs=[])
    missing = uuid4()
    worker._template_repo.list_by_ids = AsyncMock(return_value={})

    with pytest.raises(NotFoundError) as actual:
        await worker._load_templates(_dag_with(missing))

    with pytest.raises(NotFoundError) as canonical:
        BaseRepo._require_found(
            None, label="Agent template", entity_id=missing, context_key="template_id"
        )

    assert actual.value.detail == canonical.value.detail
    assert actual.value.context == canonical.value.context
    assert actual.value.status == canonical.value.status
