"""Structured logging — structlog JSON + correlation_id + redaction.

**Stub Sprint 0 — minimal bootstrap** — full implementation in Story 1.9.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict

from agentive_backend.shared.config import settings
from agentive_backend.shared.correlation import get_correlation_id


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
            # NFR9 — redact API keys BEFORE the JSON renderer so leaked keys
            # never reach stdout. Story 1.6.
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
