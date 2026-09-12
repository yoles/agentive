"""Unit tests — the ``request_fingerprint`` migration (Story 4.8, review P9).

The repo has no migration harness: ``migrated_db`` is session-scoped and
shared by every integration test, so actually running an upgrade/downgrade
inside one would tear the schema out from under all the others. The sibling
``test_control_signal_migration`` established what CAN be tested without a
database — the statements issued and their order — and this file applies the
same method to the trap this migration actually has.

That trap is the index PREDICATE. Story 4.8's own Debug Log calls
``WHERE request_fingerprint IS NOT NULL`` the migration's piège n°1: drop it
and, because ``NULLS NOT DISTINCT`` makes two NULLs EQUAL, the index build
fails on any base carrying two fingerprint-less rows. The story verified that
once, by hand, on a throwaway Postgres; nothing re-verifies it. A predicate is
a property of the statement, so it belongs here.

The second thing worth pinning is that the raw statement and the
``Index(...)`` declared on ``Workflow.__table_args__`` stay in agreement.
The declaration is what stops ``alembic revision --autogenerate`` from
emitting a ``DROP INDEX`` (review P4); the statement is what actually builds
the index. Nothing but a test keeps two hand-written copies in step.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from agentive_backend.infra.db.models import Workflow

_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "20260912_000002_workflows_request_fingerprint.py"
)

_INDEX_NAME = "uq_workflow_request_fingerprint"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_request_fingerprint_migration", _MIGRATION)
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

    def add_column(self, table: str, column: Any) -> None:
        self.calls.append(("add_column", f"{table}.{column.name}"))

    def drop_column(self, table: str, column: str) -> None:
        self.calls.append(("drop_column", f"{table}.{column}"))


def _record(direction: str, monkeypatch: pytest.MonkeyPatch) -> _RecordingOp:
    module = _load()
    op = _RecordingOp()
    monkeypatch.setattr(module, "op", op)
    getattr(module, direction)()
    return op


@pytest.fixture
def recorded_upgrade(monkeypatch: pytest.MonkeyPatch) -> _RecordingOp:
    return _record("upgrade", monkeypatch)


@pytest.fixture
def recorded_downgrade(monkeypatch: pytest.MonkeyPatch) -> _RecordingOp:
    return _record("downgrade", monkeypatch)


def _create_index_statement(op: _RecordingOp) -> str:
    statements = [sql for kind, sql in op.calls if kind == "execute"]
    matching = [sql for sql in statements if _INDEX_NAME in sql and "CREATE" in sql.upper()]
    assert matching, f"no CREATE INDEX statement found in {statements}"
    return " ".join(matching[0].split())


def test_upgrade_builds_a_partial_index(recorded_upgrade: _RecordingOp) -> None:
    """The piège n°1 of this migration, and the only part of it a base can
    fail on.

    Rows predating the story carry a NULL fingerprint and are never
    backfilled. Under `NULLS NOT DISTINCT` two NULLs are EQUAL, so a TOTAL
    unique index rejects the second such row and the migration dies with
    "could not create unique index / Duplicate keys exist" on any base
    carrying two of them. The predicate lifts them out of the index.
    """
    assert "WHERE request_fingerprint IS NOT NULL" in _create_index_statement(recorded_upgrade)


def test_upgrade_index_is_unique_and_nulls_not_distinct(recorded_upgrade: _RecordingOp) -> None:
    """`NULLS NOT DISTINCT` is what keeps the index operative at all in the
    single-tenant MVP, where `tenant_id IS NULL` on every row — without it
    Postgres treats every `(fp, NULL)` pair as distinct and the constraint
    never fires. This is the bug `20260508000000` had to fix after the fact
    on `uq_agent_template`; re-arming it here was deliberate."""
    statement = _create_index_statement(recorded_upgrade)

    assert "CREATE UNIQUE INDEX" in statement
    assert "NULLS NOT DISTINCT" in statement
    assert "(request_fingerprint, tenant_id)" in statement


def test_upgrade_adds_the_column_before_indexing_it(recorded_upgrade: _RecordingOp) -> None:
    """Order, the half a reader is most likely to "tidy" later: the index
    names a column that does not exist until `add_column` has run."""
    kinds = [kind for kind, _ in recorded_upgrade.calls]

    assert kinds.index("add_column") < kinds.index("execute")


def test_upgrade_column_is_nullable_and_sized_for_a_sha256_hex(
    recorded_upgrade: _RecordingOp,
) -> None:
    """Nullable because legacy rows have no fingerprint; 64 because the hash
    is never truncated (a shortened key manufactures collisions)."""
    module = _load()
    op = _RecordingOp()

    columns: list[Any] = []

    def _capture(table: str, column: Any) -> None:
        columns.append(column)

    op.add_column = _capture  # type: ignore[method-assign]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "op", op)
        module.upgrade()

    assert len(columns) == 1
    assert columns[0].name == "request_fingerprint"
    assert columns[0].nullable is True
    assert columns[0].type.length == 64


def test_upgrade_statement_matches_the_model_declaration() -> None:
    """Review P4 — the index is declared in TWO places on purpose, and the
    two must not drift.

    `Workflow.__table_args__` carries the declaration so that autogenerate,
    which diffs the database against `Base.metadata`, does not emit a
    `DROP INDEX` for an index it cannot see — that would silently switch
    idempotence off. This migration carries the statement that actually
    builds it. Contrary to what the story originally claimed, SQLAlchemy
    2.0.49 does express both halves, so the two forms are comparable
    character for character rather than merely "equivalent in spirit".
    """
    module = _load()
    op = _RecordingOp()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "op", op)
        module.upgrade()
    from_migration = _create_index_statement(op)

    index = next(i for i in Workflow.__table__.indexes if i.name == _INDEX_NAME)
    from_model = " ".join(
        str(CreateIndex(index).compile(dialect=postgresql.dialect())).split()
    ).strip()

    assert from_migration == from_model


def test_downgrade_drops_the_index_before_the_column(recorded_downgrade: _RecordingOp) -> None:
    """Postgres would cascade the index away with the column, but relying on
    that leaves the statement order meaningless to a reader — and the
    explicit `DROP INDEX IF EXISTS` is what makes a re-run safe."""
    kinds = [kind for kind, _ in recorded_downgrade.calls]

    assert kinds.index("execute") < kinds.index("drop_column")


def test_downgrade_performs_no_data_step(recorded_downgrade: _RecordingOp) -> None:
    """The asymmetry with `20260912_000001`, which DOES reconcile rows on the
    way down, is deliberate and documented: dropping this column strands no
    row. Pre-4.8 code never reads `request_fingerprint`; it simply loses the
    idempotence guarantee. An UPDATE appearing here later would mean someone
    mistook the asymmetry for an oversight.
    """
    statements = [sql for kind, sql in recorded_downgrade.calls if kind == "execute"]

    assert not [sql for sql in statements if re.search(r"\b(UPDATE|DELETE|INSERT)\b", sql.upper())]


def test_downgrade_removes_the_column(recorded_downgrade: _RecordingOp) -> None:
    dropped = [target for kind, target in recorded_downgrade.calls if kind == "drop_column"]

    assert dropped == ["workflows.request_fingerprint"]


def test_migration_chains_onto_the_previous_head() -> None:
    """A branched migration chain is silent until someone runs `upgrade head`
    and gets "Multiple head revisions are present"."""
    module = _load()

    assert module.revision == "20260912000002"
    assert module.down_revision == "20260912000001"
