"""Inspect a LangGraph checkpoint stored by the M3 spike — Story 1.2 AC5.

Usage::

    docker compose run --rm backend uv run python -m spike.inspect_checkpoint <thread_id>

    # or via the Makefile wrapper:
    make spike-m3-inspect THREAD_ID=<uuid>

Prints the latest :class:`CheckpointTuple` for the given ``thread_id`` as
pretty-printed JSON. Useful to verify what state was persisted before / after a
SIGKILL crash and to confirm the workflow can be resumed from the checkpoint.

Connects via :data:`SPIKE_DATABASE_URL` if set, otherwise via
``settings.database_url_owner`` (the ``agentive_owner`` role — required for
``CREATE TABLE`` on the LangGraph internal tables).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agentive_backend.shared.config import settings


def _checkpoint_dsn() -> str:
    if override := os.environ.get("SPIKE_DATABASE_URL"):
        return override
    return str(settings.database_url_owner).replace("postgresql+psycopg://", "postgresql://", 1)


def _to_serializable(value: Any) -> Any:
    """Best-effort JSON-friendly conversion for opaque LangGraph payloads."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {str(k): _to_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_serializable(v) for v in value]
    return repr(value)


async def inspect(thread_id: str) -> int:
    config = {"configurable": {"thread_id": thread_id}}
    async with AsyncPostgresSaver.from_conn_string(_checkpoint_dsn()) as saver:
        tuple_ = await saver.aget_tuple(config)
        if tuple_ is None:
            print(f"❌ no checkpoint found for thread_id={thread_id}", file=sys.stderr)
            return 1

        payload = {
            "thread_id": thread_id,
            "checkpoint_id": tuple_.config["configurable"].get("checkpoint_id"),
            "checkpoint_values": _to_serializable(tuple_.checkpoint.get("channel_values")),
            "metadata": _to_serializable(tuple_.metadata),
            "parent_config": _to_serializable(tuple_.parent_config),
            "pending_writes_count": len(tuple_.pending_writes or []),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect a LangGraph spike checkpoint")
    parser.add_argument("thread_id", help="Workflow thread_id (UUID) to inspect")
    args = parser.parse_args()
    return asyncio.run(inspect(args.thread_id))


if __name__ == "__main__":
    raise SystemExit(main())
