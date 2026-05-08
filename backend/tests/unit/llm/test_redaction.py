"""Redaction patterns — coverage of every supported provider key prefix."""

from __future__ import annotations

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
