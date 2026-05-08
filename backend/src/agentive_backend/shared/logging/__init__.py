"""Structured logging — structlog JSON + correlation_id + 3-stage redaction.

## Redaction order

The ``configure_logging()`` function wires three redaction processors in a
deliberate order BEFORE ``JSONRenderer`` :

1. ``redact_sensitive_keys_processor`` (``shared.logging.redaction``) —
   walks the event_dict and replaces values under keys whose lower-cased name
   contains ``password`` / ``secret`` / ``token`` / ``api_key`` / ``access_token``
   / ``authorization`` / ``cookie`` / ``private_key``. Most precise (key-based).
2. ``redact_pii_processor`` (``shared.logging.redaction``) — recursive regex
   on every string value : emails (NFR8 RGPD), IPv4, IPv6.
3. ``redact_api_keys_processor`` (``shared.llm.redaction`` — Story 1.6) —
   recursive regex on every string value : LLM API key patterns
   (``sk-ant-…``, ``sk-proj-…``, ``sk-…``, ``pa-…``).

The order matters : (1) is precise (key-based, no false positives on values),
(2) is the broad PII sweep, (3) is the LLM-specific safety net. All three
sentinels (``[REDACTED]``, ``[REDACTED_EMAIL]``, ``[REDACTED_IP]``) are
idempotent — applying twice yields the same dict.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict

from agentive_backend.shared.config import settings
from agentive_backend.shared.correlation import get_correlation_id
from agentive_backend.shared.logging.redaction import (
    redact_pii_processor,
    redact_sensitive_keys_processor,
)


def _add_correlation_id(_: Any, __: str, event_dict: EventDict) -> EventDict:
    """structlog processor — inject correlation_id if bound."""
    cid = get_correlation_id()
    if cid is not None:
        event_dict["correlation_id"] = cid
    return event_dict


def configure_logging() -> None:
    """Configure structlog + stdlib logging with JSON output."""
    # Import the LLM redaction processor lazily — at module-load time it would
    # create a cycle ``shared.logging`` → ``shared.llm.redaction`` →
    # ``shared.llm.__init__`` → ``shared.llm.router`` → ``shared.logging``.
    # Importing it inside ``configure_logging`` defers the resolution until the
    # full module graph is settled (Story 1.6 review fix-batch P-circ).
    from agentive_backend.shared.llm.redaction import redact_api_keys_processor

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=settings.log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_correlation_id,
            structlog.processors.dict_tracebacks,
            # NFR8/NFR9 — 3-stage redaction BEFORE JSONRenderer so secrets +
            # PII never reach stdout. Order documented in module docstring.
            redact_sensitive_keys_processor,
            redact_pii_processor,
            redact_api_keys_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a structlog BoundLogger for the given module name."""
    logger: structlog.BoundLogger = structlog.get_logger(name)
    return logger
