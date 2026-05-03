"""Unit tests for :mod:`agentive_backend.shared.auth` — Story 1.7."""

from __future__ import annotations

import re

from agentive_backend.shared.auth import generate_token, hash_token, verify_token

# ─── generate_token ───────────────────────────────────────────────────────────


def test_generate_token_length() -> None:
    # ``secrets.token_urlsafe(32)`` returns 43 chars (URL-safe base64 of 32
    # bytes, no padding). Asserting >= 43 catches any regression that drops
    # the entropy parameter (e.g. ``token_urlsafe(8)`` would still be > 32
    # chars but only carry 64 bits of entropy).
    token = generate_token()
    assert len(token) >= 43


def test_generate_token_uniqueness() -> None:
    assert generate_token() != generate_token()


def test_generate_token_url_safe_charset() -> None:
    # URL-safe base64: letters, digits, hyphens, underscores only (no + or /)
    token = generate_token()
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", token), f"Non-URL-safe chars in {token!r}"


# ─── hash_token ───────────────────────────────────────────────────────────────


def test_hash_token_bcrypt_prefix() -> None:
    h = hash_token("some-secret-token")
    assert h.startswith("$2b$"), f"Expected bcrypt hash, got {h!r}"


def test_hash_token_round_trip() -> None:
    raw = generate_token()
    assert verify_token(raw, hash_token(raw)) is True


# ─── verify_token — bcrypt mode ───────────────────────────────────────────────


def test_verify_token_bcrypt_correct() -> None:
    raw = "correct-secret"
    assert verify_token(raw, hash_token(raw)) is True


def test_verify_token_bcrypt_wrong() -> None:
    assert verify_token("wrong", hash_token("correct")) is False


def test_verify_token_bcrypt_malformed_hash() -> None:
    # Starts with $2b$ but is not a valid bcrypt hash — must return False,
    # not raise.
    assert verify_token("anything", "$2b$12$notavalidhash") is False


# ─── verify_token — plaintext fallback ────────────────────────────────────────


def test_verify_token_plaintext_match() -> None:
    assert verify_token("change_me", "change_me") is True


def test_verify_token_plaintext_mismatch() -> None:
    assert verify_token("other", "change_me") is False


# ─── verify_token — empty inputs (P4 — auth bypass guard) ────────────────────


def test_verify_token_empty_raw_rejected() -> None:
    """Empty Bearer token must NOT match an empty stored value."""
    assert verify_token("", "") is False


def test_verify_token_empty_raw_with_stored_rejected() -> None:
    assert verify_token("", "change_me") is False


def test_verify_token_with_empty_stored_rejected() -> None:
    assert verify_token("anything", "") is False


# ─── verify_token — bcrypt $2a$/$2y$ variants (P3 — prefix detection) ────────


def test_verify_token_bcrypt_2a_prefix() -> None:
    """$2a$ (legacy bcrypt) must be detected as bcrypt mode, not plaintext."""
    raw = "secret"
    # bcrypt.hashpw produces $2b$ — manually craft a $2a$ variant by swapping
    # the prefix. bcrypt.checkpw accepts both.
    import bcrypt

    h = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=4)).decode()
    h_2a = "$2a$" + h[len("$2b$") :]
    assert verify_token(raw, h_2a) is True
    # Wrong token must NOT match the hash STRING in plaintext mode (it would
    # if the prefix detection failed and we fell through to compare_digest).
    assert verify_token(h_2a, h_2a) is False


def test_verify_token_bcrypt_2y_prefix() -> None:
    """$2y$ (PHP-style bcrypt) must be detected as bcrypt mode."""
    raw = "secret"
    import bcrypt

    h = bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=4)).decode()
    h_2y = "$2y$" + h[len("$2b$") :]
    assert verify_token(raw, h_2y) is True
    # Critical check: the literal hash string must NOT authenticate as
    # plaintext (which would happen if prefix detection missed $2y$).
    assert verify_token(h_2y, h_2y) is False


def test_verify_token_bcrypt_random_dollar_string_treated_as_plaintext() -> None:
    """A string starting with $ but NOT matching the bcrypt regex stays plaintext."""
    # $1$ is MD5 crypt — not bcrypt. Must be treated as plaintext.
    stored = "$1$abc$xyz"
    assert verify_token(stored, stored) is True  # plaintext literal match
    assert verify_token("other", stored) is False
