"""Generic log redaction — PII + sensitive key-name heuristic.

This module is the **logging-layer counterpart** of ``shared.llm.redaction``
(Story 1.6, which targets LLM API key patterns). The two are complementary :

- ``shared.llm.redaction`` — domain LLM, regex on **values** for known API
  key shapes (``sk-ant-…``, ``sk-proj-…``, ``pa-…``).
- ``shared.logging.redaction`` *(this module)* — domain logging, two
  orthogonal strategies :

  1. **PII regex on values** : emails (NFR8 RGPD) + IPv4/IPv6 (RGPD).
  2. **Key-name heuristic on keys** : any JSON key whose lower-cased name
     contains a sensitive token (``password``, ``secret``, ``token``,
     ``api_key``, ``access_token``, ``authorization``, ``cookie``,
     ``private_key``) → entire value replaced by ``[REDACTED]``.

The two structlog processors exposed here (``redact_pii_processor`` and
``redact_sensitive_keys_processor``) are wired before
``redact_api_keys_processor`` in ``configure_logging()`` — see
``shared.logging.__init__`` for the canonical order.

Idempotence
-----------
All three sentinels (``[REDACTED]``, ``[REDACTED_EMAIL]``, ``[REDACTED_IP]``)
contain neither ``@`` nor IP-shaped digits/hex-with-colons, so applying any
processor twice yields the same dict.

False positives
---------------
- ``redact_pii_processor`` may match strings that *look like* IPs / emails
  but are actually content (e.g. version strings ``1.2.3.4`` collide with
  the IPv4 pattern). Trade-off accepted: security wins over log fidelity.
- ``redact_sensitive_keys_processor`` only fires on **keys**, never on
  values — a value containing the substring ``token`` is left untouched
  unless its enclosing key triggers the heuristic.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from structlog.typing import EventDict

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PII patterns — values
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Email — local-part allowing dot/underscore/percent/plus/dash, domain TLD ≥ 2.
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# IPv4 — four 1-3 digit groups dot-separated. Catches public + private + loopback.
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# IPv6 — pragmatic patterns. Requires 3+ colons (4+ groups) for the full form
# to avoid false positives on ISO 8601 time substrings like ``18:36:41`` (2 colons).
# The second alternation catches collapsed forms like ``::1`` or ``::abcd``.
# Trade-off accepted : partially-collapsed forms like ``2001:db8::1`` are caught
# only on their ``::1`` tail (Sprint 0 baseline — false negatives safer than
# false positives that would mangle every timestamp in stdout).
_IPV6_RE = re.compile(
    r"(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}"  # 4+ groups, excludes hh:mm:ss
    r"|::[0-9a-fA-F]{1,4}",  # ::1, ::abcd
)

_REDACTED_EMAIL = "[REDACTED_EMAIL]"
_REDACTED_IP = "[REDACTED_IP]"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Sensitive key-name tokens
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Any JSON key whose lower-cased name CONTAINS one of these substrings has
# its entire value replaced (recursive containers do NOT descend further —
# the whole value is opaque ``[REDACTED]``).
_SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "api_key",
    "access_token",
    "authorization",
    "cookie",
    "private_key",
)

_REDACTED = "[REDACTED]"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Public API — pure helpers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def redact_pii(text: str) -> str:
    """Replace email and IP substrings in ``text`` with sentinels.

    Idempotent: ``[REDACTED_EMAIL]`` / ``[REDACTED_IP]`` never re-match
    the regex patterns. Returns the input unchanged for non-string or empty
    inputs.
    """
    if not isinstance(text, str) or not text:
        return text
    out = _EMAIL_RE.sub(_REDACTED_EMAIL, text)
    out = _IPV4_RE.sub(_REDACTED_IP, out)
    out = _IPV6_RE.sub(_REDACTED_IP, out)
    return out


def _is_sensitive_key(key: Any) -> bool:
    """Return True if ``key`` (str-coerced, lower-cased) contains a sensitive token."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_KEY_TOKENS)


def _redact_pii_value(value: Any) -> Any:
    """Recurse containers and apply :func:`redact_pii` on terminal strings.

    Mirrors the container-handling pattern of ``shared.llm.redaction._redact_value``
    (Story 1.6) for consistency, but applies PII patterns instead of API key
    patterns.
    """
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, (bytes, bytearray)):
        return redact_pii(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        return {k: _redact_pii_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_pii_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_pii_value(v) for v in value)
    if hasattr(value, "__dict__"):
        try:
            attrs = vars(value)
        except TypeError:
            return value
        return {k: _redact_pii_value(v) for k, v in attrs.items()}
    return value


def _redact_by_key(node: Any) -> Any:
    """Walk a Mapping recursively. Values under sensitive keys → ``[REDACTED]``.

    Lists / tuples / objects with ``__dict__`` are traversed (their dict
    representation is walked). Non-container values pass through untouched —
    the heuristic targets keys, not values.
    """
    if isinstance(node, Mapping):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            if _is_sensitive_key(key):
                out[key] = _REDACTED
            else:
                out[key] = _redact_by_key(value)
        return out
    if isinstance(node, list):
        return [_redact_by_key(v) for v in node]
    if isinstance(node, tuple):
        return tuple(_redact_by_key(v) for v in node)
    if hasattr(node, "__dict__"):
        try:
            attrs = vars(node)
        except TypeError:
            return node
        return _redact_by_key(attrs)
    return node


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# structlog processors
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def redact_pii_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor — apply :func:`redact_pii` to every value (recursive).

    Place AFTER ``redact_sensitive_keys_processor`` (whose ``[REDACTED]``
    sentinels won't match the PII regexes) and BEFORE
    ``redact_api_keys_processor`` (whose API key patterns are orthogonal).
    """
    return {key: _redact_pii_value(value) for key, value in event_dict.items()}


def redact_sensitive_keys_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor — replace values under sensitive key names with ``[REDACTED]``.

    Walks the event_dict recursively. The check is on **keys** (case-insensitive
    substring match against ``_SENSITIVE_KEY_TOKENS``), not on values.

    Example::

        before: {"auth": {"access_token": "xyz", "user_email": "j@x.com"}}
        after:  {"auth": {"access_token": "[REDACTED]", "user_email": "j@x.com"}}

    The ``user_email`` value above is then handled by ``redact_pii_processor``
    in the chain (resulting in ``[REDACTED_EMAIL]``).
    """
    out: dict[str, Any] = {}
    for key, value in event_dict.items():
        if _is_sensitive_key(key):
            out[key] = _REDACTED
        else:
            out[key] = _redact_by_key(value)
    return out
