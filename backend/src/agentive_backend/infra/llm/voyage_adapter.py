"""VoyageProvider — concrete :class:`Embedder` wrapping the official
``voyageai`` SDK (Story 3.6 T3, AC3 optional backend).

Structural mirror of :class:`~agentive_backend.infra.llm.openai_adapter.OpenAIProvider`
— same "construct the client fresh per call, carrying ``timeout_s``" shape
— but via the vendor's own SDK rather than a LangChain wrapper: no
``langchain-voyageai`` integration exists in this project's dependency
graph, unlike OpenAI/Anthropic.

Deliberately lighter-weight than the other two adapters (see this story's
Dev Notes § Scope Voyage): the epic's own Given/When/Then coverage of the
Voyage backend is thin, so this file favors mirroring the established
OpenAI error-classification contract over inventing new machinery.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import ClassVar, Final, cast

from voyageai import error as voyage_error
from voyageai.client import Client as VoyageClient

from agentive_backend.shared.llm.embedder import EmbeddingPurpose
from agentive_backend.shared.llm.exceptions import (
    LLMError,
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from agentive_backend.shared.llm.redaction import redact_secrets

EMBEDDING_MODEL_NAME: Final[str] = "voyage-3-lite"
EMBEDDING_DIMENSIONS: Final[int] = 512


def _classify_voyage_exception(exc: Exception) -> LLMError | None:
    """Convert a ``voyageai`` SDK exception into our domain hierarchy.

    Mirror of :func:`agentive_backend.infra.llm.openai_adapter._classify_openai_exception`
    — same four-way split (auth / rate-limit / timeout / unavailable),
    same "return ``None`` for an unknown shape so the caller re-raises raw"
    contract (T3.3), so :class:`~agentive_backend.shared.llm.embedding_router.EmbeddingRouter`
    treats every adapter's failures uniformly.
    """
    detail = redact_secrets(str(exc))

    if isinstance(exc, voyage_error.AuthenticationError):
        return LLMProviderAuthError(detail=detail)
    if isinstance(exc, voyage_error.RateLimitError):
        return LLMProviderRateLimitError(detail=detail)
    if isinstance(exc, voyage_error.Timeout):
        return LLMProviderTimeoutError(detail=detail)
    if isinstance(
        exc,
        (voyage_error.ServiceUnavailableError, voyage_error.APIConnectionError),
    ):
        return LLMProviderUnavailableError(detail=detail)
    if isinstance(exc, (voyage_error.InvalidRequestError, voyage_error.MalformedRequestError)):
        return LLMProviderBadRequestError(detail=detail)
    return None


class VoyageProvider:
    """:class:`Embedder` adapter for Voyage AI via the official ``voyageai`` SDK."""

    provider_name: ClassVar[str] = "voyage"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        timeout_s: float = 30.0,
        purpose: EmbeddingPurpose | None = None,
    ) -> list[list[float]]:
        """Embed ``texts`` via ``voyageai.Client.embed`` (Story 3.6 T3.2).

        ``voyageai.Client`` is synchronous, and unlike ``fastembed`` this IS
        a real network call — so both the ``asyncio.to_thread`` wrapping
        AND ``timeout_s`` matter here (contrast with
        :meth:`~agentive_backend.infra.llm.fastembed_adapter.FastEmbedProvider.embed`,
        which wraps a synchronous call with no round-trip to bound).

        The SDK takes its timeout at ``Client`` construction, not per call
        (no per-request override in ``Client.embed``'s signature) — so,
        like :class:`~agentive_backend.infra.llm.openai_adapter.OpenAIProvider.embed`
        building a fresh ``OpenAIEmbeddings`` per call, a fresh ``Client``
        is built here per call carrying that call's ``timeout_s``.

        ``purpose`` maps directly onto Voyage's own ``input_type``
        parameter — the SDK already uses the literal strings ``"document"``
        / ``"query"`` (Story 3.6 code review, Décision John 2026-09-09), so
        no translation table is needed, only a pass-through. ``None`` is
        forwarded as-is: Voyage treats a missing ``input_type`` as a plain,
        purpose-agnostic embedding, same as omitting the argument entirely.
        """

        def _run() -> list[list[float]]:
            client = VoyageClient(api_key=self._api_key, timeout=timeout_s)
            result = client.embed(list(texts), model=model, input_type=purpose)
            # `EmbeddingsObject.embeddings` is typed `list[list[float]] |
            # list[list[int]]` (int only for a quantized `output_dtype` we
            # never request) — cast rather than let `warn_return_any`-style
            # ambiguity leak into this method's declared return type.
            return cast("list[list[float]]", result.embeddings)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:
            translated = _classify_voyage_exception(exc)
            if translated is None:
                raise
            raise translated from exc


__all__ = ["EMBEDDING_DIMENSIONS", "EMBEDDING_MODEL_NAME", "VoyageProvider"]
