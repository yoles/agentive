"""OpenAIProvider — concrete :class:`LLMProvider` wrapping ``langchain-openai``.

The OpenAI / LangChain combo has one notable quirk: legacy chat models
(``gpt-3.5-*``, ``gpt-4``, ``gpt-4.1``) accept the canonical ``max_tokens``
kwarg, but the newer reasoning models (``o1``, ``o3``, ``o4``, ``gpt-5*``)
require ``max_completion_tokens``. We resolve which kwarg name to use up
front (:func:`_resolve_max_tokens_kwarg`) and pipe the unsupported one
through ``model_kwargs`` so the LangChain pydantic field validation does
not strip it.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, ClassVar, Final, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import SecretStr

from agentive_backend.shared.llm.exceptions import (
    LLMError,
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)
from agentive_backend.shared.llm.redaction import redact_secrets
from agentive_backend.shared.llm.types import ChatMessage, Completion, FinishReason

# Snapshot 2026-05 — verify against
# https://platform.openai.com/docs/pricing
# Tuple = (input USD per 1M tokens, output USD per 1M tokens).
MODEL_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-5": (Decimal("10.00"), Decimal("30.00")),
    "gpt-5-mini": (Decimal("1.50"), Decimal("6.00")),
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
    "o4-mini": (Decimal("3.00"), Decimal("12.00")),
}

# Reasoning + GPT-5 family — the API rejected `max_tokens` and demands
# `max_completion_tokens` instead.
_NEW_MAX_TOKENS_PREFIXES: tuple[str, ...] = ("o1", "o3", "o4", "gpt-5")

# Story 3.6 T6.1 — the "cloud" backend's canonical model, formerly a
# `MemoryManagerService`-local constant (`EMBEDDING_MODEL`, Story 3.1)
# hardcoded there because only one backend existed. Moved here, alongside
# `FastEmbedProvider.EMBEDDING_MODEL_NAME` / `VoyageProvider.EMBEDDING_MODEL_NAME`,
# now that `EmbeddingRouter` (T4) needs one canonical name per backend.
# Matches the partial HNSW index `chunk_embeddings_openai_hnsw` (Story 3.1).
EMBEDDING_MODEL_NAME: Final[str] = "text-embedding-3-small"

_FINISH_REASON_MAP: dict[str, FinishReason] = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "content_filter",
}


def _resolve_max_tokens_kwarg(model: str) -> str:
    """Return the kwarg name OpenAI expects for the given model family."""
    return "max_completion_tokens" if model.startswith(_NEW_MAX_TOKENS_PREFIXES) else "max_tokens"


def _to_lc_messages(messages: Sequence[ChatMessage]) -> tuple[list[BaseMessage], str | None]:
    """Map ChatMessage to LangChain types AND extract inline system messages.

    OpenAI accepts inline ``role="system"`` messages, but the symmetry
    with the Anthropic adapter (review fix-batch P5) requires us to
    extract them so the explicit ``system=`` kwarg is layered the same
    way in both adapters. The extracted system prompt is concatenated
    with ``\\n\\n`` like in :func:`anthropic_adapter._to_lc_messages`.
    """
    system_chunks: list[str] = []
    converted: list[BaseMessage] = []
    for m in messages:
        if m.role == "system":
            system_chunks.append(m.content)
        elif m.role == "user":
            converted.append(HumanMessage(content=m.content))
        elif m.role == "assistant":
            converted.append(AIMessage(content=m.content))
    system_prompt = "\n\n".join(system_chunks) if system_chunks else None
    return converted, system_prompt


def _merge_system(system_kwarg: str | None, extracted: str | None) -> str | None:
    """Same as Anthropic adapter — see fix-batch P5."""
    if system_kwarg is not None and extracted is not None:
        return f"{system_kwarg}\n\n{extracted}"
    return system_kwarg if system_kwarg is not None else extracted


def _extract_text(content: Any) -> str:
    """Flatten ``str`` or ``list[ContentBlock]`` to a string.

    The unknown-shape fallback runs through :func:`redact_secrets` to
    avoid leaking a key buried inside a dict ``__repr__`` (review fix-
    batch P13).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return redact_secrets(str(content))


def _compute_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        return None
    in_price, out_price = pricing
    cost = (
        Decimal(input_tokens) / Decimal(1_000_000) * in_price
        + Decimal(output_tokens) / Decimal(1_000_000) * out_price
    )
    return cost.quantize(Decimal("0.000001"))


def _classify_openai_exception(exc: Exception) -> LLMError | None:
    """Convert an SDK / HTTP exception into our domain hierarchy.

    Returns ``None`` for unknown shapes so the caller can re-raise raw
    (review fix-batch P12, mirrors :func:`anthropic_adapter._classify_anthropic_exception`).
    """
    import httpx

    detail = redact_secrets(str(exc))

    if isinstance(exc, httpx.TimeoutException):
        return LLMProviderTimeoutError(detail=detail)
    if isinstance(exc, httpx.ConnectError):
        return LLMProviderUnavailableError(detail=detail)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return LLMProviderAuthError(detail=detail)
        if code == 429:
            return LLMProviderRateLimitError(detail=detail)
        if 400 <= code < 500:
            return LLMProviderBadRequestError(detail=detail)
        if 500 <= code < 600:
            return LLMProviderUnavailableError(detail=detail)

    cls_name = exc.__class__.__name__
    if "Auth" in cls_name or "Permission" in cls_name:
        return LLMProviderAuthError(detail=detail)
    if "RateLimit" in cls_name:
        return LLMProviderRateLimitError(detail=detail)
    if "Timeout" in cls_name:
        return LLMProviderTimeoutError(detail=detail)
    if "BadRequest" in cls_name or "NotFound" in cls_name or "InvalidRequest" in cls_name:
        return LLMProviderBadRequestError(detail=detail)
    if "ServiceUnavailable" in cls_name or "APIConnectionError" in cls_name:
        return LLMProviderUnavailableError(detail=detail)
    return None


class OpenAIProvider:
    """:class:`LLMProvider` adapter for OpenAI via ``langchain-openai``."""

    provider_name: ClassVar[str] = "openai"

    def __init__(
        self,
        api_key: SecretStr | str,
        *,
        max_retries: int = 0,
    ) -> None:
        if isinstance(api_key, str):
            api_key = SecretStr(api_key)
        self._api_key: SecretStr = api_key
        self._max_retries = max_retries

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float = 0.7,
        system: str | None = None,
        stop: Sequence[str] | None = None,
        timeout_s: float = 30.0,
    ) -> Completion:
        lc_messages, extracted_system = _to_lc_messages(messages)
        # P5 — combine explicit system kwarg with inline system messages
        # (no silent dropping AND no duplication — same contract as the
        # Anthropic adapter).
        effective_system = _merge_system(system, extracted_system)
        if effective_system is not None:
            lc_messages = [SystemMessage(content=effective_system), *lc_messages]

        kwargs_max_tokens_name = _resolve_max_tokens_kwarg(model)
        chat_kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "request_timeout": timeout_s,
            "openai_api_key": self._api_key,
            "max_retries": self._max_retries,
        }
        if stop:
            chat_kwargs["stop"] = list(stop)

        # `max_tokens` is a typed pydantic field on ChatOpenAI; for legacy
        # models we set it directly. For new models the field name is not
        # exposed, so we pipe it through `model_kwargs` which is forwarded
        # verbatim to the OpenAI API.
        if kwargs_max_tokens_name == "max_tokens":
            chat_kwargs["max_tokens"] = max_tokens
        else:
            chat_kwargs["model_kwargs"] = {"max_completion_tokens": max_tokens}

        chat = ChatOpenAI(**chat_kwargs)

        start = time.perf_counter()
        try:
            response = await chat.ainvoke(lc_messages)
        except Exception as exc:
            translated = _classify_openai_exception(exc)
            if translated is None:
                raise
            raise translated from exc
        latency_ms = (time.perf_counter() - start) * 1000.0

        return self._to_completion(response, model, latency_ms)

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        timeout_s: float = 30.0,
    ) -> list[list[float]]:
        """Embed ``texts`` via ``langchain_openai.OpenAIEmbeddings`` (Story 3.1 T1.2).

        Reuses :func:`_classify_openai_exception` — same error hierarchy as
        :meth:`complete`, so callers translate embedding failures the same
        way (auth/rate-limit/timeout/unavailable).
        """
        embeddings = OpenAIEmbeddings(
            model=model,
            openai_api_key=self._api_key,
            request_timeout=timeout_s,
            max_retries=self._max_retries,
        )
        try:
            return await embeddings.aembed_documents(list(texts))
        except Exception as exc:
            translated = _classify_openai_exception(exc)
            if translated is None:
                raise
            raise translated from exc

    async def raw_provider_call(self, **kwargs: Any) -> Any:
        kwargs.setdefault("openai_api_key", self._api_key)
        messages = kwargs.pop("messages", None)
        if messages is None:
            raise ValueError("raw_provider_call requires a 'messages' kwarg")
        chat = ChatOpenAI(**kwargs)
        try:
            return await chat.ainvoke(messages)
        except Exception as exc:
            translated = _classify_openai_exception(exc)
            if translated is None:
                raise
            raise translated from exc

    @staticmethod
    def _to_completion(
        response: BaseMessage, requested_model: str, latency_ms: float
    ) -> Completion:
        text = _extract_text(getattr(response, "content", ""))
        metadata = cast(dict[str, Any], getattr(response, "response_metadata", {}) or {})
        usage = cast(
            dict[str, Any],
            getattr(response, "usage_metadata", None)
            or metadata.get("token_usage", {})
            or metadata.get("usage", {})
            or {},
        )

        model_name = str(metadata.get("model_name") or metadata.get("model") or requested_model)

        # OpenAI's usage_metadata uses `input_tokens` / `output_tokens` (LangChain
        # standardizes), but legacy `token_usage` uses `prompt_tokens` /
        # `completion_tokens`.
        input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)))
        output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)))

        finish_raw = str(metadata.get("finish_reason") or metadata.get("stop_reason") or "")
        finish_reason: FinishReason = _FINISH_REASON_MAP.get(finish_raw, "error")

        provider_request_id = metadata.get("id")
        cost = _compute_cost(model_name, input_tokens, output_tokens)

        return Completion(
            text=text,
            model=model_name,
            provider=OpenAIProvider.provider_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=finish_reason,
            latency_ms=latency_ms,
            provider_request_id=str(provider_request_id) if provider_request_id else None,
            cost_estimate_usd=cost,
        )
