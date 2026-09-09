"""Concrete LLM provider adapters.

Public API
----------
:class:`AnthropicProvider`
    Wraps ``langchain-anthropic`` (``ChatAnthropic``).
:class:`OpenAIProvider`
    Wraps ``langchain-openai`` (``ChatOpenAI`` for chat, ``OpenAIEmbeddings``
    for :meth:`~OpenAIProvider.embed` since Story 3.1) — handles the
    ``max_tokens`` vs ``max_completion_tokens`` divergence.
:class:`FastEmbedProvider`
    Local ``bge-small-en-v1.5`` ONNX model via ``fastembed`` — Story 3.6 AC2.
:class:`VoyageProvider`
    Wraps the official ``voyageai`` SDK — Story 3.6 AC3 (optional backend).

Multi-provider embedding routing lives in
:class:`agentive_backend.shared.llm.embedding_router.EmbeddingRouter`
(Story 3.6).
"""

from __future__ import annotations

from agentive_backend.infra.llm.anthropic_adapter import AnthropicProvider
from agentive_backend.infra.llm.fastembed_adapter import FastEmbedProvider
from agentive_backend.infra.llm.openai_adapter import OpenAIProvider
from agentive_backend.infra.llm.voyage_adapter import VoyageProvider

__all__ = ["AnthropicProvider", "FastEmbedProvider", "OpenAIProvider", "VoyageProvider"]
