"""Unit tests for the at-rest config cipher (audit M-09 / Story 9.2).

Covers the envelope round-trip, the deterministic dev-key derivation, the
legacy-plaintext passthrough that keeps pre-9.2 rows readable, and tamper
detection.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from agentive_backend.shared.security import crypto


def test_encrypt_decrypt_round_trip() -> None:
    config = {"command": "python", "args": ["-m", "srv"], "env": {"TOKEN": "s3cr3t"}}

    envelope = crypto.encrypt(config)
    assert crypto.decrypt(envelope) == config


def test_envelope_is_self_describing_and_hides_plaintext() -> None:
    envelope = crypto.encrypt({"env": {"API_KEY": "super-secret-value"}})

    assert crypto.is_encrypted_envelope(envelope)
    assert envelope["__enc__"] == "fernet"
    assert envelope["v"] == 1
    # The secret must not appear anywhere in the stored envelope.
    assert "super-secret-value" not in str(envelope)


def test_decrypt_passes_through_legacy_plaintext_rows() -> None:
    """A row written before Story 9.2 is a plain dict, not an envelope — it must
    be returned unchanged so old data stays readable during migration."""
    legacy = {"command": "python", "args": ["-m", "srv"]}

    assert not crypto.is_encrypted_envelope(legacy)
    assert crypto.decrypt(legacy) == legacy


def test_decrypt_rejects_tampered_ciphertext() -> None:
    envelope = crypto.encrypt({"url": "https://mcp.example"})
    envelope["ct"] = envelope["ct"][:-4] + "AAAA"  # corrupt the token tail

    with pytest.raises(InvalidToken):
        crypto.decrypt(envelope)


def test_dev_placeholder_key_still_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``change_me`` dev placeholder is not a valid Fernet key; the cipher
    must derive a usable key so local encryption works without a real key."""
    from agentive_backend.shared.config import settings

    monkeypatch.setattr(
        settings.agentive_encryption_key, "get_secret_value", lambda: "change_me"
    )
    crypto._fernet_for.cache_clear()

    config = {"headers": {"Authorization": "Bearer abc"}}
    assert crypto.decrypt(crypto.encrypt(config)) == config


def test_real_fernet_key_is_used_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentive_backend.shared.config import settings

    real_key = Fernet.generate_key().decode()
    monkeypatch.setattr(
        settings.agentive_encryption_key, "get_secret_value", lambda: real_key
    )
    crypto._fernet_for.cache_clear()

    envelope = crypto.encrypt({"env": {"K": "v"}})
    # A token minted with the same real key decrypts our envelope's ciphertext.
    assert Fernet(real_key.encode()).decrypt(envelope["ct"].encode())

    crypto._fernet_for.cache_clear()
