"""Redaction patterns — coverage of every supported provider key prefix."""

from __future__ import annotations

import pytest

from agentive_backend.shared.llm.redaction import (
    redact_api_keys_processor,
    redact_secrets,
)


def test_redacts_anthropic_key() -> None:
    raw = "Authorization: Bearer sk-ant-fake-test-key-1234567890abcdefghij1234567890"
    out = redact_secrets(raw)
    assert "sk-ant-fake" not in out
    assert "[REDACTED]" in out


def test_redacts_openai_legacy_key() -> None:
    raw = "Bearer sk-fakeopenai1234567890abcdefghijklmnop123456"
    out = redact_secrets(raw)
    assert "sk-fakeopenai" not in out
    assert "[REDACTED]" in out


def test_redacts_openai_project_key() -> None:
    raw = "x-api-key: sk-proj-fakeprojkey1234567890abcdefghijklmnop"
    out = redact_secrets(raw)
    assert "sk-proj-fakepro" not in out
    assert "[REDACTED]" in out


def test_redacts_voyage_key() -> None:
    raw = "key=pa-fakevoyage1234567890abcdefghijklmnopqrst"
    out = redact_secrets(raw)
    assert "pa-fakevoyage" not in out
    assert "[REDACTED]" in out


def test_text_without_pattern_unchanged() -> None:
    raw = "user said hello and asked about weather"
    assert redact_secrets(raw) == raw


def test_short_tokens_not_redacted() -> None:
    """A bare `sk-foo` (< 30 chars after prefix) is NOT a real key shape."""
    raw = "shorthand sk-foo and pa-bar should stay"
    assert redact_secrets(raw) == raw


def test_redacts_inside_url_query_string() -> None:
    raw = "https://api.openai.com/v1/chat?token=sk-fakekey1234567890abcdefghijklmnopqrstuv"
    out = redact_secrets(raw)
    assert "sk-fakekey" not in out
    assert "[REDACTED]" in out


def test_redacts_inside_nested_json() -> None:
    raw = '{"error":"401","headers":{"x-api-key":"sk-ant-leaked1234567890abcdefghij1234567890"}}'
    out = redact_secrets(raw)
    assert "sk-ant-leaked" not in out


def test_idempotent() -> None:
    raw = "sk-ant-fakekeyfakekeyfakekey1234567890abcd"
    once = redact_secrets(raw)
    twice = redact_secrets(once)
    assert once == twice


def test_processor_redacts_top_level_string() -> None:
    event = {"event": "boom", "body": "leak sk-ant-fakekey1234567890abcdefghij1234567890"}
    out = redact_api_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert "sk-ant-fakekey" not in out["body"]


def test_processor_redacts_nested_dict() -> None:
    event = {
        "event": "boom",
        "context": {"headers": {"auth": "Bearer sk-fakefakefake1234567890abcdefghijklm12345"}},
    }
    out = redact_api_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert "sk-fakefake" not in out["context"]["headers"]["auth"]


def test_processor_redacts_nested_list() -> None:
    event = {"event": "boom", "items": ["clean", "sk-ant-secretsecretsecretsecret1234567890"]}
    out = redact_api_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert "[REDACTED]" in out["items"][1]
    assert out["items"][0] == "clean"


def test_processor_passes_through_non_string_values() -> None:
    event = {"event": "x", "count": 42, "ratio": 0.5, "enabled": True, "missing": None}
    out = redact_api_keys_processor(None, "info", event)  # type: ignore[arg-type]
    assert out["count"] == 42
    assert out["ratio"] == 0.5
    assert out["enabled"] is True
    assert out["missing"] is None


# ─── URL credentials (Story 4.2 review, finding #13) ───────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "connection failed: postgresql://agentive_app:sup3r-s3cret@db:5432/agentive",
        "postgresql+psycopg://owner:p%40ss@localhost/agentive",
        "could not connect to https://user:hunter2@api.example.com/v1/chat",
    ],
)
def test_redacts_url_credentials(raw: str) -> None:
    """Prefix patterns alone missed the single most common secret shape in
    an exception message: a psycopg `OperationalError` embeds the full DSN,
    password included. Story 4.2 persists `str(exc)` in
    `workflow_runs.checkpoint.last_error` AND streams it to SSE clients."""
    out = redact_secrets(raw)
    assert "[REDACTED]" in out
    for leaked in ("sup3r-s3cret", "p%40ss", "hunter2"):
        assert leaked not in out


def test_url_credential_redaction_keeps_the_diagnostic_part() -> None:
    """Redaction must not destroy the reason the string was logged."""
    out = redact_secrets("postgresql://app:pw@db.internal:5432/agentive — timeout")
    assert "db.internal:5432/agentive" in out
    assert "timeout" in out


def test_url_credential_redaction_is_idempotent() -> None:
    once = redact_secrets("postgresql://app:pw@db:5432/agentive")
    assert redact_secrets(once) == once


def test_url_without_credentials_is_untouched() -> None:
    """No userinfo, nothing to redact — a plain URL must survive intact."""
    clean = "GET https://api.anthropic.com/v1/messages returned 503"
    assert redact_secrets(clean) == clean


def test_host_port_is_not_mistaken_for_credentials() -> None:
    """`db:5432` after `//` is host:port, not user:password — the `@`
    terminator is what distinguishes them."""
    clean = "cannot reach postgresql://db:5432/agentive"
    assert redact_secrets(clean) == clean
