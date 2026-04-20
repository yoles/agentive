"""Agentive exception hierarchy — RFC 7807 Problem Details ready.

All custom exceptions inherit from :class:`AgentiveError`. The FastAPI
exception handler in `app.main` converts these to `application/problem+json`
responses with `correlation_id`, `agent_id`, `module`, `tenant_id` context.
"""

from __future__ import annotations

from typing import Any


class AgentiveError(Exception):
    """Base exception for all Agentive application errors.

    Attributes
    ----------
    type : str
        URI identifying the problem type (RFC 7807 ``type``).
    title : str
        Short human-readable summary (RFC 7807 ``title``).
    status : int
        HTTP status code (RFC 7807 ``status``).
    detail : str | None
        Human-readable explanation specific to this occurrence.
    context : dict[str, Any]
        Additional structured context (correlation_id, agent_id, ...).
    """

    type: str = "about:blank"
    title: str = "Application error"
    status: int = 500

    def __init__(
        self,
        detail: str | None = None,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.context: dict[str, Any] = context or {}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Client errors (4xx)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ValidationError(AgentiveError):
    type = "/errors/validation"
    title = "Validation failed"
    status = 422


class NotFoundError(AgentiveError):
    type = "/errors/not-found"
    title = "Resource not found"
    status = 404


class AuthError(AgentiveError):
    type = "/errors/auth"
    title = "Authentication failed"
    status = 401


class ForbiddenError(AgentiveError):
    type = "/errors/forbidden"
    title = "Access denied"
    status = 403


class ConflictError(AgentiveError):
    type = "/errors/conflict"
    title = "Resource conflict"
    status = 409


class RateLimitError(AgentiveError):
    type = "/errors/rate-limit"
    title = "Rate limit exceeded"
    status = 429


class BusinessRuleError(AgentiveError):
    type = "/errors/business-rule"
    title = "Business rule violation"
    status = 422


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Server errors (5xx)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class InternalError(AgentiveError):
    type = "/errors/internal"
    title = "Internal server error"
    status = 500


class DependencyError(AgentiveError):
    """Downstream dependency failure (LLM provider, DB, MCP tool, ...)."""

    type = "/errors/dependency"
    title = "Downstream dependency unavailable"
    status = 503
