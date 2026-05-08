"""API key redaction — defense-in-depth against NFR9 leaks.

Why two layers
--------------
1. ``redact_secrets(text)`` — call site safety net for exception messages,
   span attributes, and any string going into events / outbox payloads.
2. structlog processor ``redact_api_keys_processor`` — runs on every log
   record before ``JSONRenderer``, so accidental ``logger.info(..., body=...)``
   that leaks a key never reaches stdout.

The patterns target the well-known prefixes and a generous suffix length
(≥ 30 chars). False positives on user content are accepted as a
necessary cost — security wins over precision in logs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from structlog.typing import EventDict

# 30+ chars after the prefix is the floor for genuine API keys (typical
# length is 80-120). Tightening the lower bound avoids redacting random
# short tokens while still catching real keys.
_API_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{30,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{30,}"),
    re.compile(r"sk-(?!ant-)(?!proj-)[A-Za-z0-9_\-]{30,}"),
    re.compile(r"\bpa-[A-Za-z0-9_\-]{30,}"),
)

_REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Replace any API key-shaped substring with ``[REDACTED]``.

    Idempotent: applying twice yields the same string. Safe on ``None``-ish
    inputs (returns the input unchanged) — callers may pass arbitrary
    str(exception) outputs.
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern in _API_KEY_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


def _redact_value(value: Any) -> Any:
    """Recurse through containers, redact terminal strings.

    Handled containers
    ------------------
    * ``str`` — pattern-redacted directly.
    * ``bytes`` / ``bytearray`` — decoded leniently, redacted, returned as
      ``str``. Prevents leaks via ``log.info(..., body=httpx_response.content)``.
    * ``Mapping`` — recurse on values.
    * ``list`` — recurse, return ``list``.
    * ``tuple`` — recurse, return a plain ``tuple``. NamedTuple subclasses
      lose their type (acceptable — we never preserved field semantics in logs).
    * Other objects with ``__dict__`` (Pydantic models, dataclasses, regular
      classes) — recurse on their attribute dict and return the redacted
      representation as a string ``repr``-prefixed dict so JSONRenderer can
      serialize without invoking the original ``__repr__`` (which might leak).
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, (bytes, bytearray)):
        return redact_secrets(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, Mapping):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        # P21 — drop NamedTuple subclass type; tuple(...) always works whereas
        # `type(named_tuple)(list)` raises (NamedTuple expects positional args).
        return tuple(_redact_value(v) for v in value)
    if hasattr(value, "__dict__"):
        # P14 — Pydantic models / dataclasses / arbitrary classes. Walk
        # __dict__ so embedded strings (e.g. a field containing an API key)
        # are redacted before JSONRenderer falls back to ``default=str``.
        try:
            attrs = vars(value)
        except TypeError:
            return value
        return {k: _redact_value(v) for k, v in attrs.items()}
    return value


def redact_api_keys_processor(_logger: Any, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor — apply :func:`redact_secrets` to every value.

    Insert this BEFORE ``JSONRenderer`` so the redacted dict is what
    finally serializes to stdout.
    """
    return {key: _redact_value(value) for key, value in event_dict.items()}
