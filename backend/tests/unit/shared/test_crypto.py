"""Unit tests for the at-rest config cipher (audit M-09 / Story 9.2).

Covers the envelope round-trip, the deterministic dev-key derivation, the
legacy-plaintext passthrough that keeps pre-9.2 rows readable, and tamper
detection.
"""

from __future__ import annotations

import random
import string
from typing import Any

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

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

    monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: "change_me")
    crypto._multifernet_for.cache_clear()

    config = {"headers": {"Authorization": "Bearer abc"}}
    assert crypto.decrypt(crypto.encrypt(config)) == config


def test_real_fernet_key_is_used_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentive_backend.shared.config import settings

    real_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: real_key)
    crypto._multifernet_for.cache_clear()

    envelope = crypto.encrypt({"env": {"K": "v"}})
    # A token minted with the same real key decrypts our envelope's ciphertext.
    assert Fernet(real_key.encode()).decrypt(envelope["ct"].encode())

    crypto._multifernet_for.cache_clear()


def _random_payload(rng: random.Random, depth: int = 0) -> Any:
    """Generate a structurally varied JSON-serializable value for round-trip fuzzing."""
    if depth >= 3:
        choices = ("str", "int", "bool", "none")
    else:
        choices = ("str", "int", "float", "bool", "none", "list", "dict", "empty_str", "unicode")

    kind = rng.choice(choices)
    if kind == "str":
        return "".join(
            rng.choices(string.ascii_letters + string.digits + "-_", k=rng.randint(1, 40))
        )
    if kind == "unicode":
        return "".join(rng.choice("éàü中文🔑😀 secret") for _ in range(rng.randint(1, 20)))
    if kind == "empty_str":
        return ""
    if kind == "int":
        return rng.randint(-(10**9), 10**9)
    if kind == "float":
        return rng.uniform(-1e6, 1e6)
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "none":
        return None
    if kind == "list":
        return [_random_payload(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    # dict
    return {
        f"key_{i}_{rng.choice(['env', 'headers', 'args', 'x'])}": _random_payload(rng, depth + 1)
        for i in range(rng.randint(0, 5))
    }


def test_encrypt_decrypt_round_trip_at_scale() -> None:
    """AC2: round-trip holds across 1000+ structurally varied payloads.

    Payloads are generated from a fixed seed so a failure is reproducible.
    The generator covers dict/list nesting, unicode, empty strings, and
    numeric edge cases. Fernet round-trips are cheap (the full 1000 runs in
    well under a second), so the AC's volume is honoured as written rather
    than negotiated down.
    """
    rng = random.Random(20260831)
    for _ in range(1000):
        payload = {f"key_{i}": _random_payload(rng) for i in range(rng.randint(0, 8))}
        assert crypto.decrypt(crypto.encrypt(payload)) == payload


def test_decrypt_falls_back_to_previous_key_during_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3: a row encrypted under the outgoing key still decrypts once
    AGENTIVE_ENCRYPTION_KEY_PREVIOUS carries it during the rotation window."""
    from agentive_backend.shared.config import settings

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    # Encrypt under the (about to be outgoing) key.
    monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: old_key)
    crypto._multifernet_for.cache_clear()
    config = {"env": {"MCP_TOKEN": "pre-rotation-secret"}}
    envelope = crypto.encrypt(config)

    # Rotate: current key becomes new_key, previous key carries the old one.
    monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: new_key)
    monkeypatch.setattr(settings, "agentive_encryption_key_previous", SecretStr(old_key))
    crypto._multifernet_for.cache_clear()

    assert crypto.decrypt(envelope) == config

    crypto._multifernet_for.cache_clear()


def test_encrypt_always_uses_current_key_not_previous(monkeypatch: pytest.MonkeyPatch) -> None:
    """New writes during a rotation window must use the current key only —
    otherwise the previous key could never be safely retired."""
    from agentive_backend.shared.config import settings

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: new_key)
    monkeypatch.setattr(settings, "agentive_encryption_key_previous", SecretStr(old_key))
    crypto._multifernet_for.cache_clear()

    envelope = crypto.encrypt({"env": {"K": "v"}})

    # Only the new (current) key can open a freshly-encrypted envelope.
    assert Fernet(new_key.encode()).decrypt(envelope["ct"].encode())
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(envelope["ct"].encode())

    crypto._multifernet_for.cache_clear()


def test_decrypt_fails_once_previous_key_is_retired(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row never re-encrypted by the rotation script becomes unreadable once
    AGENTIVE_ENCRYPTION_KEY_PREVIOUS is unset — this is why
    ``scripts/rotate_encryption_key.py`` must run before retiring the old key."""
    from agentive_backend.shared.config import settings

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: old_key)
    crypto._multifernet_for.cache_clear()
    envelope = crypto.encrypt({"env": {"MCP_TOKEN": "never-rotated"}})

    # Retire the old key outright (no AGENTIVE_ENCRYPTION_KEY_PREVIOUS at all).
    monkeypatch.setattr(settings.agentive_encryption_key, "get_secret_value", lambda: new_key)
    monkeypatch.setattr(settings, "agentive_encryption_key_previous", None)
    crypto._multifernet_for.cache_clear()

    with pytest.raises(InvalidToken):
        crypto.decrypt(envelope)

    crypto._multifernet_for.cache_clear()
