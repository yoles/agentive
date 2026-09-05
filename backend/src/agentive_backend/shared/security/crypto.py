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
The primary key comes from ``settings.agentive_encryption_key`` (``AGENTIVE_ENCRYPTION_KEY``).
Production refuses to boot unless it is a valid 32-byte url-safe base64 Fernet
key (see ``shared/config.py:_reject_invalid_fernet_key_in_production``). In
dev/test the placeholder ``change_me`` is *not* a valid Fernet key, so we derive
a deterministic key from it via SHA-256 — encryption still round-trips locally
without forcing every developer to mint a real key.

Key rotation (Story 9.2 AC3)
-----------------------------
``settings.agentive_encryption_key_previous`` (``AGENTIVE_ENCRYPTION_KEY_PREVIOUS``),
when set, is combined with the primary key via :class:`cryptography.fernet.MultiFernet`:
encryption always uses the primary (current) key, decryption tries the primary
key first and falls back to the previous one. This is the standard library
mechanism for zero-downtime Fernet rotation — no custom envelope-versioning
scheme needed (the ``"v"`` field below stays for envelope-shape compat only).
Rotation procedure: set a new ``AGENTIVE_ENCRYPTION_KEY``, move the outgoing
key to ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS``, restart, run
``scripts/rotate_encryption_key.py`` to re-encrypt every row under the new key,
then unset ``AGENTIVE_ENCRYPTION_KEY_PREVIOUS``.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from typing import Any, Final

from cryptography.fernet import Fernet, MultiFernet

from agentive_backend.shared.config import settings

_ENVELOPE_MARKER: Final = "fernet"
_ENVELOPE_VERSION: Final = 1


def _derive_fernet(key_material: bytes) -> Fernet:
    """Build a :class:`Fernet` for the given key material.

    A valid Fernet key is used as-is; anything else (e.g. the dev ``change_me``
    placeholder) is deterministically derived via SHA-256 so local encryption
    still round-trips.
    """
    try:
        return Fernet(key_material)
    except ValueError, TypeError:
        derived = base64.urlsafe_b64encode(hashlib.sha256(key_material).digest())
        return Fernet(derived)


@lru_cache(maxsize=4)
def _multifernet_for(current: bytes, previous: bytes | None) -> MultiFernet:
    """Build the :class:`MultiFernet` used for encrypt/decrypt.

    Encryption always uses ``current`` (the first key in the list, per
    ``MultiFernet`` semantics). Decryption tries each key in order, so rows
    encrypted under a not-yet-retired ``previous`` key keep decrypting during
    a rotation window. Cached per key pair so test overrides get their own
    instance.
    """
    keys = [_derive_fernet(current)]
    if previous is not None:
        keys.append(_derive_fernet(previous))
    return MultiFernet(keys)


def _multifernet() -> MultiFernet:
    previous_secret = settings.agentive_encryption_key_previous
    previous_bytes = (
        previous_secret.get_secret_value().encode() if previous_secret is not None else None
    )
    return _multifernet_for(
        settings.agentive_encryption_key.get_secret_value().encode(), previous_bytes
    )


def is_encrypted_envelope(value: Any) -> bool:
    """Return ``True`` if ``value`` is an encryption envelope produced by :func:`encrypt`."""
    return isinstance(value, dict) and value.get("__enc__") == _ENVELOPE_MARKER


def encrypt(config: dict[str, Any]) -> dict[str, Any]:
    """Encrypt a config ``dict`` into a JSON-storable envelope.

    The plaintext is serialized deterministically (sorted keys, compact
    separators) before encryption so equal configs are represented uniformly.
    """
    plaintext = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
    token = _multifernet().encrypt(plaintext)
    return {"__enc__": _ENVELOPE_MARKER, "v": _ENVELOPE_VERSION, "ct": token.decode()}


def decrypt(value: dict[str, Any]) -> dict[str, Any]:
    """Reverse :func:`encrypt`.

    A legacy plaintext row (any ``dict`` that is not an envelope) is returned
    unchanged, so rows written before Story 9.2 stay readable.
    """
    if not is_encrypted_envelope(value):
        return value
    token = str(value["ct"]).encode()
    decrypted = _multifernet().decrypt(token)
    result: dict[str, Any] = json.loads(decrypted.decode())
    return result
