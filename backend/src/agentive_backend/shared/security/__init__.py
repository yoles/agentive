"""Shared security primitives — key-based secret redaction (audit A-02) and
at-rest config encryption (audit M-09 / Story 9.2)."""

from agentive_backend.shared.security.crypto import (
    decrypt,
    encrypt,
    is_encrypted_envelope,
)
from agentive_backend.shared.security.redaction import (
    REDACTED,
    REDACTED_DEEP,
    SECRET_KEY_PATTERNS,
    is_secret_key,
    redact_recursive,
)

__all__ = [
    "REDACTED",
    "REDACTED_DEEP",
    "SECRET_KEY_PATTERNS",
    "decrypt",
    "encrypt",
    "is_encrypted_envelope",
    "is_secret_key",
    "redact_recursive",
]
