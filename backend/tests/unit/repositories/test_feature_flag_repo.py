"""Unit tests — :class:`FeatureFlagRepo`."""

from __future__ import annotations

import pytest

from agentive_backend.shared.repositories import FeatureFlagRepo

from .conftest import make_session_factory_mock


@pytest.mark.asyncio
async def test_set_emits_insert_on_conflict_do_update() -> None:
    factory, session = make_session_factory_mock()
    repo = FeatureFlagRepo(session_factory=factory)
    await repo.set(name="experimental_router", enabled=True, rollout_percentage=10)
    sql_text = str(session.execute.await_args.args[0]).upper()
    assert "INSERT INTO" in sql_text
    assert "ON CONFLICT" in sql_text


@pytest.mark.asyncio
async def test_set_omitting_description_excludes_it_from_update_set() -> None:
    """Regression: ``description`` was unconditionally written, wiping
    existing values during partial updates."""
    factory, session = make_session_factory_mock()
    repo = FeatureFlagRepo(session_factory=factory)
    # description not passed → must not appear in ON CONFLICT DO UPDATE SET.
    await repo.set(name="foo", enabled=True)
    sql_text = str(session.execute.await_args.args[0]).lower()
    # The substring "description" must NOT appear on the right of "do update set".
    after_set = sql_text.split("do update set", 1)[-1] if "do update set" in sql_text else ""
    assert "description" not in after_set, (
        f"description should be excluded from update SET clause; got: {after_set}"
    )


@pytest.mark.asyncio
async def test_set_with_explicit_none_description_clears_value() -> None:
    factory, session = make_session_factory_mock()
    repo = FeatureFlagRepo(session_factory=factory)
    await repo.set(name="foo", enabled=True, description=None)
    sql_text = str(session.execute.await_args.args[0]).lower()
    after_set = sql_text.split("do update set", 1)[-1] if "do update set" in sql_text else ""
    assert "description" in after_set
