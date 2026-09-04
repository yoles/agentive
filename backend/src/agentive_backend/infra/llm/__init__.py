"""Concrete LLM provider adapters.

Public API
----------
:class:`AnthropicProvider`
    Wraps ``langchain-anthropic`` (``ChatAnthropic``).
:class:`OpenAIProvider`
    Wraps ``langchain-openai`` (``ChatOpenAI`` for chat, ``OpenAIEmbeddings``
    for :meth:`~OpenAIProvider.embed` since Story 3.1) — handles the
    ``max_tokens`` vs ``max_completion_tokens`` divergence.

Further embedding adapters (Voyage, FastEmbed) and multi-provider routing
arrive in Story 3.6 (Embedding Router hybride local/cloud).
"""

from __future__ import annotations

from agentive_backend.infra.llm.anthropic_adapter import AnthropicProvider
from agentive_backend.infra.llm.openai_adapter import OpenAIProvider

__all__ = ["AnthropicProvider", "OpenAIProvider"]
