"""Custom SQLAlchemy column types (audit M-09 / Story 9.2).

:class:`EncryptedJSONB` is a transparent at-rest encryption wrapper over the
Postgres ``JSONB`` type. It encrypts on write and decrypts on read via the
shared envelope cipher (:mod:`agentive_backend.shared.security.crypto`), so the
ORM attribute the rest of the app sees is always a plaintext ``dict`` — only
the bytes stored on disk are ciphertext. This is the correct boundary for
at-rest encryption: services, repositories and the MCP client stay unchanged.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

from agentive_backend.shared.security import crypto


class EncryptedJSONB(TypeDecorator[dict[str, Any]]):
    """A ``JSONB`` column whose value is Fernet-encrypted at rest.

    On the wire to Postgres the value is an encryption envelope (see
    :func:`crypto.encrypt`); on the way back it is decrypted to the original
    ``dict``. Legacy plaintext rows (written before Story 9.2) are read back
    unchanged by :func:`crypto.decrypt` and get re-encrypted on their next write.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect: Any,  # noqa: ARG002 — required by the SQLAlchemy override contract
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return crypto.encrypt(value)

    def process_result_value(
        self,
        value: dict[str, Any] | None,
        dialect: Any,  # noqa: ARG002 — required by the SQLAlchemy override contract
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        return crypto.decrypt(value)
