"""Concrete LLM provider adapters.

Public API
----------
:class:`AnthropicProvider`
    Wraps ``langchain-anthropic`` (``ChatAnthropic``).
:class:`OpenAIProvider`
    Wraps ``langchain-openai`` (``ChatOpenAI``) — handles the
    ``max_tokens`` vs ``max_completion_tokens`` divergence.

Embedding adapters (Voyage, FastEmbed) are added in Story 3.1 / 3.6
when the Embedding Router is built. The chat-completion abstraction
lives here Sprint 0.
"""

from __future__ import annotations

from agentive_backend.infra.llm.anthropic_adapter import AnthropicProvider
from agentive_backend.infra.llm.openai_adapter import OpenAIProvider

__all__ = ["AnthropicProvider", "OpenAIProvider"]
