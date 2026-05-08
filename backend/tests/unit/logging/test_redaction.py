"""PII + sensitive key-name redaction — Story 1.9 AC2 coverage.

Tests both the pure helpers (``redact_pii``) and the structlog processors
(``redact_pii_processor``, ``redact_sensitive_keys_processor``), plus
composition with the API-key processor inherited from Story 1.6.
"""

from __future__ import annotations

from agentive_backend.shared.llm.redaction import redact_api_keys_processor
from agentive_backend.shared.logging.redaction import (
    redact_pii,
    redact_pii_processor,
    redact_sensitive_keys_processor,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PII regex (helpers)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_redact_email_basic() -> None:
    out = redact_pii("contact me at john.doe@example.com")
    assert out == "contact me at [REDACTED_EMAIL]"


def test_redact_email_with_plus_alias() -> None:
    out = redact_pii("john.doe+filter@sub.example.co.uk sent it")
    assert "[REDACTED_EMAIL]" in out
    assert "@" not in out  # full email substring is masked


def test_redact_ipv4_basic() -> None:
    out = redact_pii("client 192.168.1.1 connected")
    assert out == "client [REDACTED_IP] connected"


def test_redact_ipv4_public_and_loopback() -> None:
    out = redact_pii("from 8.8.8.8 via 127.0.0.1")
    assert "[REDACTED_IP]" in out
    assert "8.8.8.8" not in out
    assert "127.0.0.1" not in out


def test_redact_ipv6_full_form() -> None:
    out = redact_pii("client 2001:0db8:85a3:0000:0000:8a2e:0370:7334 connected")
    assert "[REDACTED_IP]" in out
    assert "2001:0db8" not in out


def test_redact_ipv6_loopback() -> None:
    out = redact_pii("local ::1 only")
    assert "[REDACTED_IP]" in out
    assert "::1" not in out.replace("[REDACTED_IP]", "")


def test_redact_pii_idempotent() -> None:
    once = redact_pii("a@b.com from 10.0.0.1")
    twice = redact_pii(once)
    assert once == twice


def test_redact_pii_no_false_positive_on_redacted_sentinels() -> None:
    """Sentinels never re-match the patterns."""
    text = "[REDACTED_EMAIL] from [REDACTED_IP] via [REDACTED]"
    assert redact_pii(text) == text


def test_redact_pii_no_false_positive_on_iso_timestamps() -> None:
    """ISO 8601 timestamps (HH:MM:SS) must NOT be redacted as IPv6.

    Regression : early IPv6 regex with ``{2,7}`` colons matched ``18:36:41``
    inside ``2026-05-05T18:36:41.659172Z`` and corrupted JSON log lines.
    Fixed by tightening to ``{3,7}`` (require 4+ hex groups for the full
    form). See ``shared/logging/redaction.py`` for rationale.
    """
    text = "request at 2026-05-05T18:36:41.659172Z completed"
    assert redact_pii(text) == text  # no [REDACTED_IP] anywhere


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# redact_pii_processor (structlog) — recursion through containers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_pii_processor_top_level_email() -> None:
    event = {"event": "user_login", "email": "alice@example.com"}
    out = redact_pii_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["email"] == "[REDACTED_EMAIL]"
    assert out["event"] == "user_login"


def test_pii_processor_nested_ip() -> None:
    event = {"event": "req", "request": {"client": {"ip": "192.168.0.42"}}}
    out = redact_pii_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["request"]["client"]["ip"] == "[REDACTED_IP]"


def test_pii_processor_list_of_emails() -> None:
    event = {"recipients": ["a@x.com", "b@y.com", "no-email"]}
    out = redact_pii_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["recipients"] == ["[REDACTED_EMAIL]", "[REDACTED_EMAIL]", "no-email"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# redact_sensitive_keys_processor (structlog) — key-name heuristic
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_keys_processor_flat_password() -> None:
    event = {"user_password": "hunter2", "name": "John"}
    out = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["user_password"] == "[REDACTED]"
    assert out["name"] == "John"


def test_keys_processor_nested_token() -> None:
    event = {"auth": {"access_token": "xyz", "user": "john"}}
    out = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["auth"]["access_token"] == "[REDACTED]"
    assert out["auth"]["user"] == "john"


def test_keys_processor_case_insensitive() -> None:
    event = {
        "Authorization": "Bearer xyz",
        "AUTHORIZATION_HEADER": "Bearer abc",
        "X-Cookie": "session=abc",
    }
    out = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["Authorization"] == "[REDACTED]"
    assert out["AUTHORIZATION_HEADER"] == "[REDACTED]"
    assert out["X-Cookie"] == "[REDACTED]"


def test_keys_processor_no_false_positive_value_contains_token_word() -> None:
    """The heuristic targets KEYS, not VALUES."""
    event = {"description": "this contains the word token but should stay"}
    out = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["description"] == "this contains the word token but should stay"


def test_keys_processor_idempotent() -> None:
    event = {"api_key": "[REDACTED]", "user": "ok"}
    out = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert out == event


def test_keys_processor_handles_list_of_dicts() -> None:
    event = {"events": [{"password": "x", "id": 1}, {"password": "y", "id": 2}]}
    out = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["events"][0]["password"] == "[REDACTED]"
    assert out["events"][0]["id"] == 1
    assert out["events"][1]["password"] == "[REDACTED]"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Composition — full chain order from configure_logging()
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_chain_redact_keys_then_pii_then_api_keys() -> None:
    """The 3 processors compose without losing coverage on any pattern.

    Mirrors the order configured in ``configure_logging()``:
      keys → pii → api_keys.
    """
    event = {
        "api_key": "sk-ant-leaked1234567890abcdefghij1234567890",  # caught by keys (key match)
        "email_field": "user@example.com",  # caught by pii
        "body": "Authorization: Bearer sk-ant-otherkey1234567890abcdefghij12345",  # caught by api_keys
    }

    step1 = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    step2 = redact_pii_processor(None, "info", step1)  # type: ignore[arg-type]
    step3 = redact_api_keys_processor(None, "info", step2)  # type: ignore[arg-type]

    # api_key field — masked by step1 (key heuristic)
    assert step3["api_key"] == "[REDACTED]"
    # email_field — masked by step2 (pii regex)
    assert step3["email_field"] == "[REDACTED_EMAIL]"
    # body — caught by step3 (api keys regex on value, key not sensitive)
    assert "sk-ant-otherkey" not in step3["body"]
    assert "[REDACTED]" in step3["body"]


def test_no_regression_api_key_in_unmasked_key() -> None:
    """An API key under a non-sensitive key name is still caught by api_keys."""
    event = {"context": "leaked sk-ant-fakekey1234567890abcdefghij1234567890"}
    step1 = redact_sensitive_keys_processor(None, "info", event)  # type: ignore[arg-type]
    step2 = redact_pii_processor(None, "info", step1)  # type: ignore[arg-type]
    step3 = redact_api_keys_processor(None, "info", step2)  # type: ignore[arg-type]
    assert "sk-ant-fakekey" not in step3["context"]
    assert "[REDACTED]" in step3["context"]
