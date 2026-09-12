"""Unit tests — the ``control_signal`` migration's DOWNGRADE (Story 4.6, review lot 6).

The repo has no migration harness: ``migrated_db`` is session-scoped and
shared by every integration test, so actually downgrading inside one would
tear the schema out from under all the others. What can be tested without a
database is the thing that was wrong — a downgrade that dropped the columns
and left behind rows the restored code cannot handle — because it is a
property of the statements issued and of their ORDER, not of the engine
executing them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "20260912_000001_workflow_runs_control_signal.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_control_signal_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOp:
    """Stands in for ``alembic.op``, keeping the call order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, statement: Any) -> None:
        self.calls.append(("execute", str(statement)))

    def drop_column(self, table: str, column: str) -> None:
        self.calls.append(("drop_column", f"{table}.{column}"))

    def add_column(self, table: str, column: Any) -> None:
        self.calls.append(("add_column", f"{table}.{column.name}"))


@pytest.fixture
def recorded_downgrade(monkeypatch: pytest.MonkeyPatch) -> _RecordingOp:
    module = _load()
    op = _RecordingOp()
    monkeypatch.setattr(module, "op", op)
    module.downgrade()
    return op


def test_downgrade_moves_paused_runs_back_to_running(recorded_downgrade: _RecordingOp) -> None:
    """The worst outcome the naive downgrade produced.

    `paused` is not terminal to the restored code, so no SSE stream closes
    on it; it is not `running`, so `claim_stale_running` never claims it;
    and the `resume` endpoint is gone with the story. The run would be
    immobile forever. Back on `running` it keeps its checkpoint and its NULL
    `ended_at`, so the restored recovery sweep re-drives it — the exact
    self-healing path the upgrade docstring already relies on.
    """
    statements = [sql for kind, sql in recorded_downgrade.calls if kind == "execute"]
    assert any("SET status = 'running'" in sql and "'paused'" in sql for sql in statements)


def test_downgrade_settles_cancelled_runs_on_a_status_the_old_code_knows(
    recorded_downgrade: _RecordingOp,
) -> None:
    """Lossy on purpose: `error` is the only terminal non-success status
    pre-4.6 has. Left as `cancelled`, the run is finished but nothing
    recognises it as such — its stream hangs to the one-hour ceiling."""
    statements = [sql for kind, sql in recorded_downgrade.calls if kind == "execute"]
    assert any("SET status = 'error'" in sql and "'cancelled'" in sql for sql in statements)


def test_downgrade_reconciles_before_it_drops(recorded_downgrade: _RecordingOp) -> None:
    """Order is the whole point, and it is the half a reader is most likely
    to "tidy" later: the UPDATEs read `status`, which the drops do not
    touch — but a future statement that reads `control_signal` to decide a
    row's fate would silently become a no-op once moved below them."""
    kinds = [kind for kind, _ in recorded_downgrade.calls]
    assert kinds.index("drop_column") > max(
        index for index, kind in enumerate(kinds) if kind == "execute"
    )


def test_downgrade_still_drops_both_columns(recorded_downgrade: _RecordingOp) -> None:
    dropped = [target for kind, target in recorded_downgrade.calls if kind == "drop_column"]
    assert dropped == [
        "workflow_runs.control_requested_at",
        "workflow_runs.control_signal",
    ]
