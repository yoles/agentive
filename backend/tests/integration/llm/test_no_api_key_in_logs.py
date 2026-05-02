"""NFR9 — no API key reaches stdout via the logging pipeline."""

from __future__ import annotations

import io
import json

import structlog

from agentive_backend.shared.llm.redaction import redact_api_keys_processor


def test_processor_strips_api_key_before_json_renderer() -> None:
    """Render a structured log and assert the redacted output."""
    buffer = io.StringIO()
    structlog.configure(
        processors=[
            redact_api_keys_processor,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )
    logger = structlog.get_logger("test")

    fake_key = "sk-ant-fake-leakkey-1234567890abcdefghij1234567890"
    logger.info("provider_error", body=f"401 from upstream auth={fake_key}")

    output = buffer.getvalue()
    assert "sk-ant-fake-leakkey" not in output
    assert "[REDACTED]" in output

    # The JSON record is still valid.
    payload = json.loads(output.strip())
    assert payload["event"] == "provider_error"
    assert "[REDACTED]" in payload["body"]
