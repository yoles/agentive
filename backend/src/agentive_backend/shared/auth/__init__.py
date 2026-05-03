"""Static-token auth utilities — MVP Sprint 0 (Story 1.7).

Three public helpers:
  * ``generate_token``  — produce a cryptographically-random bearer token
  * ``hash_token``      — bcrypt-hash a token for storage
  * ``verify_token``    — constant-time check; supports bcrypt hashes AND
                          plaintext (``hmac.compare_digest`` fallback for the
                          dev default ``AGENTIVE_API_TOKEN=change_me``)

Growth (Sprint 4): session-based auth (cookie HTTP-only + table ``sessions``)
will replace the static token for the browser UI.  This module stays as the
machine-to-machine API auth layer.
"""

from __future__ import annotations

import hmac
import re
import secrets

import bcrypt

# Match any standard bcrypt prefix (any cost factor):
#   $2a$NN$  — legacy OpenBSD bcrypt, still emitted by some libraries
#   $2b$NN$  — modern bcrypt (Python ``bcrypt`` library default)
#   $2y$NN$  — PHP-flavoured bcrypt, common in legacy secret stores
#
# Without matching all three variants, an operator pasting a $2y$ hash
# (perfectly valid bcrypt) would have it treated as PLAINTEXT — the literal
# hash string would become the comparison value, leaking the token-shape
# secret to anyone who guesses the hash text and silently downgrading
# authentication strength to a single-shot string match.
_BCRYPT_PREFIX_RE = re.compile(r"^\$2[aby]\$\d{2}\$")


def generate_token() -> str:
    """Return a URL-safe random token (~43 chars, 256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    """Return a bcrypt hash of ``raw`` (rounds=12, ``$2b$`` prefix).

    Always produce a bcrypt hash regardless of what ``raw`` looks like.
    Store the result in the secret store (env var / DB) rather than the
    plaintext token.
    """
    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_token(raw: str, stored: str) -> bool:
    """Return ``True`` if ``raw`` matches ``stored``.

    Dual-mode:
    * If ``stored`` matches the bcrypt prefix regex (``$2[aby]$NN$``) it is
      a bcrypt hash — use ``bcrypt.checkpw`` (constant-time, timing-safe).
    * Otherwise fall back to ``hmac.compare_digest`` for plaintext
      comparison.  This supports the dev default ``AGENTIVE_API_TOKEN=
      change_me`` without requiring operators to pre-hash it in .env.example.

    Production deployments MUST use a bcrypt hash (enforced in lifespan.py
    when ``settings.environment == "production"``).

    Empty values always return ``False`` — without this guard, an empty
    env var would cause ``compare_digest("", "")`` to return True, allowing
    a literal ``Authorization: Bearer `` (empty token) to authenticate.
    """
    # P4 — empty inputs are never a valid match. Guards both the empty-env
    # bypass case AND the empty-Bearer-header case.
    if not raw or not stored:
        return False
    if _BCRYPT_PREFIX_RE.match(stored):
        try:
            return bcrypt.checkpw(raw.encode(), stored.encode())
        except ValueError, TypeError:  # malformed hash, wrong version prefix, etc.
            return False
    # Plaintext fallback — constant-time to prevent timing attacks even in
    # dev mode.
    return hmac.compare_digest(raw, stored)
