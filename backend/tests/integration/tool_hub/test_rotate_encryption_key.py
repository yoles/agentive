"""Integration test for the key-rotation script (Story 9.2 AC3).

Proves the full rotation cycle against a real Postgres testcontainer:
a row encrypted under an "old" key stays readable during the rotation
window (``AGENTIVE_ENCRYPTION_KEY_PREVIOUS``), the script rewrites it under
the "new" key, and — the actual proof the script did its job — the row
still decrypts once the old key is retired outright.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from scripts.rotate_encryption_key import _rotate_tool_servers
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.infra.db.models import ToolServer
from agentive_backend.shared.security import crypto


@pytest.mark.integration
async def test_rotate_encryption_key_migrates_rows_to_current_key(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``tool_servers`` isn't in the shared ``clean_repository_tables`` truncate
    list (other tool_hub e2e tests deliberately let rows accumulate — they
    only assert on rows they created themselves, under the one shared default
    key). This test uses two ad-hoc keys unrelated to that default key, so a
    table-wide rotation scan (as ``_rotate_tool_servers`` legitimately does in
    production) would trip over any leftover row from an earlier test that it
    can't decrypt. Delete first so this test only ever sees its own row.
    """
    from agentive_backend.shared.config import settings

    async with seed_session_factory() as session:
        await session.execute(text("DELETE FROM tool_servers"))
        await session.commit()

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    try:
        # 1. Write a row under the "old" key (pre-rotation state).
        monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: old_key)
        crypto._multifernet_for.cache_clear()

        secret = "rotate-me-secret"
        async with app_session_factory() as session:
            server = ToolServer(
                name="rotation-target",
                transport="stdio",
                connection_config={"command": "python", "env": {"TOKEN": secret}},
            )
            session.add(server)
            await session.commit()
            server_id = server.id

        # 2. Rotate: current key becomes new_key, previous carries old_key —
        #    the row must still decrypt through the ORM during the window.
        monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: new_key)
        monkeypatch.setattr(settings, "agentive_encryption_key_previous", SecretStr(old_key))
        crypto._multifernet_for.cache_clear()

        async with app_session_factory() as session:
            server = await session.get(ToolServer, server_id)
            assert server is not None
            assert server.connection_config["env"]["TOKEN"] == secret

        # 3. Run the rotation script against the same (testcontainer) DB.
        result = await _rotate_tool_servers(session_factory=app_session_factory)
        assert result.rotated >= 1
        assert result.is_complete, f"rows failed to rotate: {result.failed_ids}"

        # 4. Retire the old key outright — a row the script missed would now
        #    be unreadable. This is the actual proof the rewrite happened.
        monkeypatch.setattr(settings, "agentive_encryption_key_previous", None)
        crypto._multifernet_for.cache_clear()

        async with app_session_factory() as session:
            server = await session.get(ToolServer, server_id)
            assert server is not None
            assert server.connection_config["env"]["TOKEN"] == secret

        # 5. Belt-and-suspenders: the raw ciphertext in the DB no longer opens
        #    with the old key alone (proves the bytes on disk actually
        #    changed, not just that decrypt() has a stale in-memory fallback).
        async with seed_session_factory() as session:
            raw = await session.execute(
                text("SELECT connection_config->>'ct' FROM tool_servers WHERE id = :sid"),
                {"sid": server_id},
            )
            ciphertext = raw.scalar_one()
            with pytest.raises(InvalidToken):
                Fernet(old_key.encode()).decrypt(ciphertext.encode())
    finally:
        crypto._multifernet_for.cache_clear()
        # Leave the table empty for whatever test runs next — this row is
        # only decryptable with the ad-hoc keys generated above, which are
        # about to go out of scope with this test.
        async with seed_session_factory() as session:
            await session.execute(text("DELETE FROM tool_servers"))
            await session.commit()


@pytest.mark.integration
async def test_rotate_isolates_and_reports_undecryptable_rows(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row readable by no configured key must not abort the whole run.

    Such a row is reachable in production: a rotation that was interrupted, or
    run twice without retiring the intermediate key, leaves rows encrypted
    under a key nobody holds any more. The script has to rotate everything
    else, name the bad rows, and report the run as incomplete so the operator
    does not retire the previous key on a false success.
    """
    from agentive_backend.shared.config import settings

    async with seed_session_factory() as session:
        await session.execute(text("DELETE FROM tool_servers"))
        await session.commit()

    current_key = Fernet.generate_key().decode()
    orphan_key = Fernet.generate_key().decode()  # never configured at rotation time

    try:
        # A row encrypted under a key that will not be configured any more.
        monkeypatch.setattr(
            settings.agentive_encryption_key, "get_secret_value", lambda: orphan_key
        )
        crypto._multifernet_for.cache_clear()
        async with app_session_factory() as session:
            orphan = ToolServer(
                name="orphaned-row",
                transport="stdio",
                connection_config={"command": "python", "env": {"TOKEN": "unreachable"}},
            )
            session.add(orphan)
            await session.commit()
            orphan_id = orphan.id

        # A healthy row under the key the rotation will actually run with.
        monkeypatch.setattr(
            settings.agentive_encryption_key, "get_secret_value", lambda: current_key
        )
        monkeypatch.setattr(settings, "agentive_encryption_key_previous", None)
        crypto._multifernet_for.cache_clear()
        async with app_session_factory() as session:
            healthy = ToolServer(
                name="healthy-row",
                transport="stdio",
                connection_config={"command": "python", "env": {"TOKEN": "reachable"}},
            )
            session.add(healthy)
            await session.commit()
            healthy_id = healthy.id

        result = await _rotate_tool_servers(session_factory=app_session_factory)

        # The bad row is named, not swallowed — and it did not strand the good one.
        assert result.failed_ids == [orphan_id]
        assert result.rotated == 1
        assert not result.is_complete, "run must be reported incomplete so the old key stays"

        # The healthy row survived the run intact and still decrypts.
        async with app_session_factory() as session:
            server = await session.get(ToolServer, healthy_id)
            assert server is not None
            assert server.connection_config["env"]["TOKEN"] == "reachable"
    finally:
        crypto._multifernet_for.cache_clear()
        async with seed_session_factory() as session:
            await session.execute(text("DELETE FROM tool_servers"))
            await session.commit()
