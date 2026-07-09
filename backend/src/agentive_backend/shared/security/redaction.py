"""Key-based secret redaction — single source of truth (audit A-02).

Why this module exists
----------------------
Before the 2026-07 audit, TWO key-based redaction implementations lived in
``m5_tool_hub/service.py`` with DIVERGENT coverage: ``_redact_connection_config``
matched an exact closed set of 6 key names while ``_redact_arguments`` matched
12 case-insensitive substring patterns. A ``connection_config`` holding a
secret under ``bearer`` / ``credential`` / ``passphrase`` / ``client_secret`` /
``access_key`` was therefore echoed IN CLEAR by ``GET /tools/servers/{id}``
while the same key would have been masked in tool arguments (audit 5.6).

This module hosts the ONE predicate (:func:`is_secret_key`) and the ONE
recursive walker (:func:`redact_recursive`) that every key-based redaction
path must use, so coverage can never diverge again. The cross-consistency
test ``tests/unit/shared/test_redaction_consistency.py`` enforces this.

Scope note — text-based redaction (``shared/llm/redaction.py``) is a
DIFFERENT concern: it masks API-key-shaped substrings (``sk-ant-…``) inside
free text at the logging layer. It stays where it is; do not merge the two.
"""

from __future__ import annotations

from typing import Any, Final

#: Case-insensitive substring patterns identifying a secret-bearing key.
#: The canonical list — every key-based redaction path derives from it.
SECRET_KEY_PATTERNS: Final[tuple[str, ...]] = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "_key",  # covers openai_key, private_key, access_key, signing_key, etc.
    "key_",  # covers key_id, key_secret, etc.
    "auth",  # matches authorization, auth_header, bearer_auth, etc.
    "bearer",
    "credential",  # credentials, aws_credential, etc.
    "passphrase",
    "client_secret",
)

#: Replacement value for a masked secret.
REDACTED: Final = "<redacted>"

#: Replacement value past the recursion depth bound.
REDACTED_DEEP: Final = "<redacted-deep>"


def is_secret_key(key: str) -> bool:
    """True if ``key`` (case-insensitive) contains any secret-name pattern."""
    lkey = key.lower()
    return any(pat in lkey for pat in SECRET_KEY_PATTERNS)


def redact_recursive(value: Any, *, depth: int = 0, max_depth: int = 8) -> Any:
    """Recursively redact secret-bearing values in arbitrary dicts/lists.

    Walks dicts + lists ; masks values whose KEY (in a parent dict) matches
    :func:`is_secret_key`. Non-dict / non-list values are returned as-is.
    Depth-bounded (default 8 levels, defensive against pathological input).
    """
    if depth > max_depth:
        return REDACTED_DEEP
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if is_secret_key(key_str):
                result[key_str] = REDACTED
            else:
                result[key_str] = redact_recursive(v, depth=depth + 1, max_depth=max_depth)
        return result
    if isinstance(value, list):
        return [redact_recursive(item, depth=depth + 1, max_depth=max_depth) for item in value]
    return value


__all__ = [
    "REDACTED",
    "REDACTED_DEEP",
    "SECRET_KEY_PATTERNS",
    "is_secret_key",
    "redact_recursive",
]
