"""Unit tests — :class:`WorkflowRepo` and :class:`WorkflowRunRepo`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from agentive_backend.shared.exceptions import NotFoundError
from agentive_backend.shared.repositories import WorkflowRepo, WorkflowRunRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_workflow_create_adds_orm_row() -> None:
    factory, session = make_session_factory_mock()
    repo = WorkflowRepo(session_factory=factory)
    await repo.create(name="ingest", dag={"nodes": []})
    session.add.assert_called_once()
    workflow = session.add.call_args.args[0]
    assert workflow.name == "ingest"
    assert workflow.status == "active"


@pytest.mark.asyncio
async def test_workflow_create_in_session_adds_row_without_commit() -> None:
    """Story 4.1 T2.3 — create_in_session ajoute la row dans la session du
    caller (no commit), mirror AgentTemplateRepo.create_in_session."""
    _, session = make_session_factory_mock()
    repo = WorkflowRepo(session_factory=lambda: None)
    await repo.create_in_session(session, name="ingest", dag={"nodes": []})
    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    session.refresh.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    workflow = session.add.call_args.args[0]
    assert workflow.name == "ingest"
    assert workflow.status == "active"
    assert workflow.version == 1


@pytest.mark.asyncio
async def test_workflow_run_update_status_returns_int_from_rowcount() -> None:
    factory, session = make_session_factory_mock()
    # Make execute return a result whose rowcount is 1.
    result_mock = session.execute.return_value
    result_mock.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_status(uuid4(), status="completed")
    assert n == 1


@pytest.mark.asyncio
async def test_workflow_run_update_status_returns_zero_when_no_rows_match() -> None:
    """Silent failure path documented in the docstring (RLS or missing row)."""
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 0
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_status(uuid4(), status="completed")
    assert n == 0


# ─── Story 4.2 T2.1 — WorkflowRepo.require_by_id ───────────────────────


@pytest.mark.asyncio
async def test_workflow_require_by_id_returns_row_when_found() -> None:
    factory, session = make_session_factory_mock()
    workflow_id = uuid4()
    session.get.return_value = object()
    repo = WorkflowRepo(session_factory=factory)
    result = await repo.require_by_id(workflow_id)
    assert result is session.get.return_value


@pytest.mark.asyncio
async def test_workflow_require_by_id_raises_not_found_when_absent() -> None:
    """AC1 — ``workflow_id`` unknown ⇒ 404 (id is the URL's primary resource,
    unlike 4.1's create_workflow where it lives in the body)."""
    factory, session = make_session_factory_mock()
    session.get.return_value = None
    workflow_id = uuid4()
    repo = WorkflowRepo(session_factory=factory)
    with pytest.raises(NotFoundError) as exc_info:
        await repo.require_by_id(workflow_id)
    assert exc_info.value.context["workflow_id"] == str(workflow_id)


# ─── Story 4.2 T2.2 — WorkflowRunRepo.update_checkpoint ────────────────


@pytest.mark.asyncio
async def test_workflow_run_update_checkpoint_returns_int_from_rowcount() -> None:
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_checkpoint(
        uuid4(),
        checkpoint={"last_node_id": "a"},
        last_checkpoint_at=datetime.now(UTC),
    )
    assert n == 1


@pytest.mark.asyncio
async def test_workflow_run_update_checkpoint_returns_zero_when_no_rows_match() -> None:
    factory, session = make_session_factory_mock()
    result_mock = session.execute.return_value
    result_mock.rowcount = 0
    repo = WorkflowRunRepo(session_factory=factory)
    n = await repo.update_checkpoint(
        uuid4(),
        checkpoint={"last_node_id": "a"},
        last_checkpoint_at=datetime.now(UTC),
    )
    assert n == 0


# ─── Story 4.2 T2.3 — WorkflowRunRepo.claim_stale_running ──────────────


@pytest.mark.asyncio
async def test_workflow_run_claim_stale_running_returns_scalars() -> None:
    factory, session = make_session_factory_mock()
    stale_run = object()
    session.execute.return_value.scalars.return_value.all.return_value = [stale_run]
    repo = WorkflowRunRepo(session_factory=factory)
    result = await repo.claim_stale_running(older_than=datetime.now(UTC))
    assert result == [stale_run]


@pytest.mark.asyncio
async def test_workflow_run_claim_stale_running_uses_no_tenant_scoping() -> None:
    """T2.3 — the recovery worker sweeps every tenant, mirror
    ``MemoryArchivalWorker`` which also has no per-run tenant scoping."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    repo = WorkflowRunRepo(session_factory=factory)
    await repo.claim_stale_running(older_than=datetime.now(UTC))
    # `claim_stale_running` accepts no `tenant_id` kwarg at all — a call with
    # one would be a TypeError, enforced structurally by the signature
    # rather than re-asserted here on the mock.
    session.execute.assert_awaited_once()


# ─── Story 4.6 T12.7 — what the claim query must and must not filter on ──
#
# Moved here by the review (lot 11). The two tests these replace lived in
# `tests/unit/workflow_engine/test_recovery.py` and neither reached the
# production query: one built its OWN `WorkflowRun.status == "running"`
# clause in the test body and asserted SQLAlchemy renders it as `= 'running'`;
# the other set `control_signal` on a `SimpleNamespace` while
# `claim_stale_running` was an `AsyncMock`, and `recovery.py` never reads
# that attribute at all. Both stayed green under the exact regressions their
# docstrings claimed to guard. The guarantee is a property of the STATEMENT,
# so it belongs where the statement is built.


@pytest.mark.asyncio
async def test_workflow_run_claim_stale_running_filters_on_running_only() -> None:
    """The load-bearing consequence of carrying the control request in a
    COLUMN rather than in `status` (Story 4.6 preamble point 6).

    A `paused` run is excluded for free — no extra clause, no new case to
    remember — which is precisely why `pausing`/`cancelling` statuses were
    rejected: each of the four places comparing against `"running"` would
    have needed teaching about them. Widen this predicate and the sweep
    starts re-driving paused runs in parallel on the same `thread_id`.
    """
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    repo = WorkflowRunRepo(session_factory=factory)

    await repo.claim_stale_running(older_than=datetime.now(UTC))

    rendered = str(
        session.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "workflow_runs.status = 'running'" in rendered
    assert "'paused'" not in rendered
    assert "'cancelled'" not in rendered


@pytest.mark.asyncio
async def test_workflow_run_claim_stale_running_ignores_a_pending_control_signal() -> None:
    """DELIBERATE, and the opposite of what a reader expects to find.

    A run whose process died after a pause was requested but before it was
    observed stays `running` with a non-NULL `control_signal`. The sweep must
    claim it anyway: the driver's pre-loop control check then settles the
    signal without executing a single node. Teaching this query to skip such
    rows — the "tidy" change — would strand them forever, because nothing
    else ever looks at `running` rows.

    Asserted as the ABSENCE of a predicate, which is the only form this
    guarantee has: there is no line in `recovery.py` to point at.
    """
    factory, session = make_session_factory_mock()
    session.execute.return_value.scalars.return_value.all.return_value = []
    repo = WorkflowRunRepo(session_factory=factory)

    await repo.claim_stale_running(older_than=datetime.now(UTC))

    rendered = str(
        session.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    # `control_signal` appears nowhere in the predicate — neither as a NULL
    # check nor as a value comparison. Scoped to the WHERE clauses (the
    # UPDATE's and the subquery's): `RETURNING` legitimately lists every
    # column, this row's own `control_signal` included, and matching against
    # the whole statement would have caught that instead.
    predicate = rendered.split("RETURNING", 1)[0].split("WHERE", 1)[1]
    assert "control_signal" not in predicate
    # …and the scoping is honest: the clause we DO expect is in there.
    assert "status = 'running'" in predicate


# ─── Story 4.3 T10.1 — WorkflowRunRepo.aggregate_routing_modes ─────────
#
# The guard's BEHAVIOUR against legacy (no `routing` key), non-numeric and
# fractional rows is proven against real Postgres — see
# `tests/integration/workflow_engine/test_hybrid_routing_e2e.py` T11.9,
# which inserts each of those row shapes and asserts the endpoint still
# answers 200. These unit tests own the other half: that the statement the
# repo actually builds is the one that carries those guards. Asserting only
# that the mock returns what it was told to return (the previous shape) left
# every one of them free to be deleted — the filter, the COALESCE, the type
# guard, even the column order.


@pytest.mark.asyncio
async def test_aggregate_routing_modes_returns_counts_from_row() -> None:
    factory, session = make_session_factory_mock()
    session.execute.return_value.one.return_value = (5, 3, 2)
    repo = WorkflowRunRepo(session_factory=factory)
    result = await repo.aggregate_routing_modes(uuid4())
    assert result == (5, 3, 2)
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_aggregate_routing_modes_zero_when_no_runs() -> None:
    """No run at all for this workflow — the COALESCE floor, not a crash."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.one.return_value = (0, 0, 0)
    repo = WorkflowRunRepo(session_factory=factory)
    result = await repo.aggregate_routing_modes(uuid4())
    assert result == (0, 0, 0)


@pytest.mark.asyncio
async def test_aggregate_routing_modes_statement_carries_its_guards() -> None:
    """The compiled SQL must still scope to the workflow, floor through
    `numeric`, and gate on `jsonb_typeof`.

    `jsonb_typeof` alone is not the guard it looks like: it answers
    `'number'` for `1.5` exactly as for `1`, and `'1.5'::int` raises. The
    cast therefore goes via `numeric` + `floor`, and this assertion is what
    keeps a future simplification from putting `::int` back.
    """
    factory, session = make_session_factory_mock()
    session.execute.return_value.one.return_value = (0, 0, 0)
    repo = WorkflowRunRepo(session_factory=factory)
    workflow_id = uuid4()
    await repo.aggregate_routing_modes(workflow_id)

    # Compiled WITHOUT `literal_binds`: the JSONB path operands carry a
    # `JSONPathType`, which has no literal renderer. The bound parameters are
    # checked separately, below.
    compiled = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    sql = str(compiled).lower()

    assert "workflow_runs.workflow_id" in sql
    assert workflow_id in compiled.params.values()
    # Count the CALLS, not the substring — SQLAlchemy names the bound
    # parameter after the function, so a bare `count("jsonb_typeof")` also
    # counts `%(jsonb_typeof_1)s` and reads 4.
    assert sql.count("jsonb_typeof(") == 2
    assert sql.count("floor(") == 2
    assert "numeric" in sql
    assert "coalesce" in sql
    # Column order is load-bearing — the caller unpacks it positionally.
    # SQLAlchemy dedupes identical bind params, so each path is bound once
    # even though it appears in both the type guard and the cast.
    paths = [v for v in compiled.params.values() if isinstance(v, tuple)]
    assert paths == [("routing", "deterministic"), ("routing", "llm_escalated")]


# ─── Story 4.6 review lot 12 — the control predicates, as SQL ──────────
#
# `request_control_in_session` and `update_status_in_session` carry the
# entire 409 contract, and every test of them drove an `AsyncMock`: the
# assertions pinned the ARGUMENTS (`signal="cancel"`, `overrides=("pause",)`)
# while an `or_()` composed the wrong way, an `in_()` against a 1-tuple or a
# dropped clause would have left all of them green. These render the
# statement the repo actually builds.


def _rendered(session: Any) -> str:
    return str(
        session.execute.await_args.args[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.asyncio
async def test_request_control_refuses_to_overwrite_a_pending_signal_by_default() -> None:
    """ "Whoever asks first wins" is a SQL predicate, not a convention: a
    second request on a run already carrying one must match no row, so the
    service answers 409 instead of silently replacing it."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.rowcount = 0
    repo = WorkflowRunRepo(session_factory=factory)

    await repo.request_control(uuid4(), signal="pause")

    rendered = _rendered(session)
    assert "control_signal IS NULL" in rendered
    assert "status = 'running'" in rendered


@pytest.mark.asyncio
async def test_request_control_lets_cancel_replace_a_pending_pause() -> None:
    """`BS1`'s escalation, rendered. The guard widens to
    `control_signal IS NULL OR control_signal IN ('pause')` — and ONLY to
    that: a `pause` may never replace a pending `cancel`, because a cancel
    has already been confirmed to somebody."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)

    await repo.request_control(uuid4(), signal="cancel", overrides=("pause",))

    rendered = _rendered(session)
    assert "control_signal IS NULL" in rendered
    assert "control_signal IN ('pause')" in rendered
    assert "'cancel'" not in rendered.split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_update_status_can_guard_on_the_signal_it_read() -> None:
    """Review lot 9 (F2): the driver's settle write must not land if the
    signal changed under it — `status` stays `running` across that whole
    window, so it cannot be the guard."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)

    await repo.update_status(
        uuid4(), status="paused", only_if_status="running", only_if_control_signal="pause"
    )

    rendered = _rendered(session)
    assert "control_signal = 'pause'" in rendered
    assert "status = 'running'" in rendered


@pytest.mark.asyncio
async def test_update_status_clears_the_signal_in_the_same_statement() -> None:
    """T1.6 — "the run is paused" and "the request was consumed" commit
    together or not at all. Split in two, a crash between them leaves a
    paused run still carrying its `pause`, which its next resume would
    observe and act on, pausing it again before a single node ran."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)

    await repo.update_status(uuid4(), status="paused", clear_control=True)

    rendered = _rendered(session)
    set_clause = rendered.split("SET", 1)[1].split("WHERE", 1)[0]
    assert "control_signal=NULL" in set_clause.replace(" ", "")
    assert "control_requested_at=NULL" in set_clause.replace(" ", "")


@pytest.mark.asyncio
async def test_update_status_stamps_activity_when_asked_to() -> None:
    """P4 — without this stamp on `paused -> running`, `claim_stale_running`
    reclaims a long-paused run within one tick and drives it a SECOND time in
    parallel on the same `thread_id`."""
    factory, session = make_session_factory_mock()
    session.execute.return_value.rowcount = 1
    repo = WorkflowRunRepo(session_factory=factory)

    await repo.update_status(uuid4(), status="running", touch_last_checkpoint=True)

    set_clause = _rendered(session).split("SET", 1)[1].split("WHERE", 1)[0]
    assert "last_checkpoint_at=now()" in set_clause.replace(" ", "").lower()
