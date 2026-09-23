"""One-shot re-encryption after an ``AGENTIVE_ENCRYPTION_KEY`` rotation (Story 9.2 AC3).

Why this script exists
-----------------------
``shared.security.crypto`` supports a rotation window via
``AGENTIVE_ENCRYPTION_KEY_PREVIOUS`` (see that module's docstring): during the
window, new writes use the current key but reads still accept the outgoing
one. That window must close — this script forces every ``EncryptedJSONB`` row
to be rewritten under the current key, after which ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS``
can be safely unset (and the old key destroyed).

Procedure
---------
1. Generate a new key::

       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

2. Set env vars: ``AGENTIVE_ENCRYPTION_KEY=<new>``, ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS=<old>``.
3. Restart the backend (or just run this script with the same env — it only
   needs DB connectivity, not the running app).
4. Run this script: ``uv run python -m scripts.rotate_encryption_key``
5. **Check the exit code.** ``0`` means every row was rewritten under the new
   key — only then may you unset ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS`` and
   destroy the old key. A non-zero exit means some rows could NOT be
   re-encrypted; their ids are listed in the error log, and retiring the
   previous key while they remain would make them permanently unreadable.

Scope: only ``tool_servers.connection_config`` is ``EncryptedJSONB`` today
(Story 9.2 T4). Add a sibling to ``_rotate_tool_servers()`` below, and call it
from ``main()``, if/when more encrypted columns are added (e.g. Story 9.3 LLM
API keys).

Concurrency: rows are locked ``FOR UPDATE`` for their read-modify-write
window, so a legitimate concurrent update is not silently overwritten with the
stale value this script read, and a concurrent delete is serialised rather
than failing the flush. The app may therefore keep serving traffic during a
rotation.

Idempotent / safe to re-run: Fernet tokens embed a fresh nonce and timestamp
on every ``encrypt()`` call, so rewriting an already-current-key row just
produces a different (still valid) ciphertext — not a bug, and safe to repeat
if the script is interrupted mid-run.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import attributes as sa_attributes

from agentive_backend.infra.db.models import ToolServer
from agentive_backend.infra.db.session import get_session_factory

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500


@dataclass
class RotationResult:
    """Outcome of a rotation run.

    ``failed_ids`` is the operationally critical field: while it is non-empty,
    those rows are still only readable with the PREVIOUS key, so retiring
    ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS`` would make them permanently
    unreadable.
    """

    rotated: int = 0
    skipped_deleted: int = 0
    failed_ids: list[uuid.UUID] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.failed_ids


async def _scan_ids(
    factory: async_sessionmaker[AsyncSession],
    after: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Return the next page of ``tool_servers`` ids, ordered by id.

    Only ids are selected: reading the encrypted column here would decrypt it
    during row materialization, and one undecryptable row would then abort the
    whole scan instead of being isolated by :func:`_rotate_one`.
    """
    async with factory() as session:
        stmt = select(ToolServer.id).order_by(ToolServer.id).limit(_BATCH_SIZE)
        if after is not None:
            stmt = stmt.where(ToolServer.id > after)
        return list((await session.execute(stmt)).scalars().all())


async def _rotate_one(
    factory: async_sessionmaker[AsyncSession],
    server_id: uuid.UUID,
) -> bool | None:
    """Re-encrypt one row under the current key.

    Returns ``True`` on success, ``None`` if the row disappeared (concurrent
    delete), and ``False`` if it could not be decrypted with any known key.

    The row is locked ``FOR UPDATE`` for the whole read-modify-write window:
    without it, a concurrent legitimate update landing between the read and
    the commit would be silently overwritten with the stale value we read.
    The lock also serialises a concurrent delete, which would otherwise fail
    the flush with ``StaleDataError``.
    """
    async with factory() as session:
        try:
            row = await session.get(ToolServer, server_id, with_for_update=True)
            if row is None:
                return None
            # Full reassignment (not an in-place dict mutation) so the ORM
            # attribute registers a history event and EncryptedJSONB
            # re-encrypts on flush. flag_modified is defensive belt-and-
            # suspenders documenting the intent for a JSONB-backed column.
            row.connection_config = dict(row.connection_config)
            sa_attributes.flag_modified(row, "connection_config")
            await session.commit()
        except Exception:
            # Isolated per row on purpose: a single undecryptable row (botched
            # earlier rotation, hand-edited ciphertext, malformed envelope)
            # must not abort the run and strand every row after it.
            await session.rollback()
            logger.exception("rotate.row_failed id=%s", server_id)
            return False
    return True


async def _rotate_tool_servers(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> RotationResult:
    """Re-encrypt every ``tool_servers.connection_config`` under the current key.

    Walks ids by keyset pagination, then rewrites each row individually so one
    bad row is isolated and reported rather than killing the run. Per-row
    transactions keep the lock window short; this is a one-shot maintenance
    script, so observability beats throughput.

    ``session_factory`` defaults to the app's real (production) session
    factory; tests pass a factory bound to the testcontainer instead, so this
    never touches the ``infra.db.session`` process-global engine cache.
    """
    factory = session_factory or get_session_factory()
    result = RotationResult()
    last_id: uuid.UUID | None = None

    while True:
        ids = await _scan_ids(factory, last_id)
        if not ids:
            break

        for server_id in ids:
            outcome = await _rotate_one(factory, server_id)
            if outcome is True:
                result.rotated += 1
            elif outcome is None:
                result.skipped_deleted += 1
            else:
                result.failed_ids.append(server_id)
            last_id = server_id

    return result


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    result = await _rotate_tool_servers()

    if result.is_complete:
        logger.info(
            "Re-encryption complete: %d tool_servers row(s) rewritten under the "
            "current AGENTIVE_ENCRYPTION_KEY (%d row(s) vanished mid-run). "
            "Safe to unset AGENTIVE_ENCRYPTION_KEY_PREVIOUS now.",
            result.rotated,
            result.skipped_deleted,
        )
        return

    # Non-zero exit: retiring the previous key now would permanently orphan
    # the rows listed here.
    logger.error(
        "Re-encryption INCOMPLETE: %d row(s) rewritten, %d row(s) FAILED (%s). "
        "Do NOT unset AGENTIVE_ENCRYPTION_KEY_PREVIOUS — investigate the "
        "failed rows first (see the tracebacks above).",
        result.rotated,
        len(result.failed_ids),
        ", ".join(str(i) for i in result.failed_ids),
    )
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
