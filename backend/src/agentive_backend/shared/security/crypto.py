"""At-rest encryption for credential-bearing config blobs (audit M-09 / Story 9.2).

Why this module exists
----------------------
Before this change, ``tool_servers.connection_config`` was stored **verbatim**
in a JSONB column (``infra/db/models.py``). That blob carries MCP credentials
(``env`` secrets for stdio, ``Authorization`` headers for sse), so a database
dump exposed **every** MCP credential in clear (audit 5.6b / M-09). Redaction
only masked the *read API* — the bytes on disk were plaintext.

This module provides the ONE envelope cipher used at the persistence boundary.
It is wired through :class:`agentive_backend.infra.db.types.EncryptedJSONB`, so
application code (services, repos, the MCP client) keeps seeing plaintext
``dict`` values — encryption happens transparently on the way to/from the DB.

Envelope format
---------------
Encrypted values are stored as a self-describing JSON object so the read path
can tell an encrypted blob from a legacy plaintext row::

    {"__enc__": "fernet", "v": 1, "ct": "<urlsafe-base64 Fernet token>"}

Legacy rows written before Story 9.2 are plain ``dict`` shapes; :func:`decrypt`
returns them untouched, so the migration is transparent (no backfill required
to keep reading old rows — they get re-encrypted on the next write).

Key material
------------
The key comes from ``settings.agentive_encryption_key`` (``AGENTIVE_ENCRYPTION_KEY``).
Production refuses to boot unless it is a valid 32-byte url-safe base64 Fernet
key (see ``shared/config.py:_reject_invalid_fernet_key_in_production``). In
dev/test the placeholder ``change_me`` is *not* a valid Fernet key, so we derive
a deterministic key from it via SHA-256 — encryption still round-trips locally
without forcing every developer to mint a real key.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from typing import Any, Final

from cryptography.fernet import Fernet

from agentive_backend.shared.config import settings

_ENVELOPE_MARKER: Final = "fernet"
_ENVELOPE_VERSION: Final = 1


@lru_cache(maxsize=4)
def _fernet_for(key_material: bytes) -> Fernet:
    """Build a :class:`Fernet` for the given key material.

    A valid Fernet key is used as-is; anything else (e.g. the dev ``change_me``
    placeholder) is deterministically derived via SHA-256 so local encryption
    still round-trips. Cached per key so test overrides get their own instance.
    """
    try:
        return Fernet(key_material)
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(key_material).digest())
        return Fernet(derived)


def _fernet() -> Fernet:
    return _fernet_for(settings.agentive_encryption_key.get_secret_value().encode())


def is_encrypted_envelope(value: Any) -> bool:
    """Return ``True`` if ``value`` is an encryption envelope produced by :func:`encrypt`."""
    return isinstance(value, dict) and value.get("__enc__") == _ENVELOPE_MARKER


def encrypt(config: dict[str, Any]) -> dict[str, Any]:
    """Encrypt a config ``dict`` into a JSON-storable envelope.

    The plaintext is serialized deterministically (sorted keys, compact
    separators) before encryption so equal configs are represented uniformly.
    """
    plaintext = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
    token = _fernet().encrypt(plaintext)
    return {"__enc__": _ENVELOPE_MARKER, "v": _ENVELOPE_VERSION, "ct": token.decode()}


def decrypt(value: dict[str, Any]) -> dict[str, Any]:
    """Reverse :func:`encrypt`.

    A legacy plaintext row (any ``dict`` that is not an envelope) is returned
    unchanged, so rows written before Story 9.2 stay readable.
    """
    if not is_encrypted_envelope(value):
        return value
    token = str(value["ct"]).encode()
    decrypted = _fernet().decrypt(token)
    result: dict[str, Any] = json.loads(decrypted.decode())
    return result
