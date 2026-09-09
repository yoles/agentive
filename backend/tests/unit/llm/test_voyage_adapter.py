"""VoyageProvider — Story 3.6 T13.1.

Mocks ``voyageai.Client`` throughout — no real network call, mirrors
``test_openai_adapter.py``'s convention of mocking the SDK boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from voyageai import error as voyage_error

from agentive_backend.infra.llm.voyage_adapter import (
    EMBEDDING_MODEL_NAME,
    VoyageProvider,
    _classify_voyage_exception,
)
from agentive_backend.shared.llm.exceptions import (
    LLMProviderAuthError,
    LLMProviderBadRequestError,
    LLMProviderRateLimitError,
    LLMProviderTimeoutError,
    LLMProviderUnavailableError,
)


def test_embedding_model_name() -> None:
    assert EMBEDDING_MODEL_NAME == "voyage-3-lite"


def test_provider_name_is_voyage() -> None:
    assert VoyageProvider.provider_name == "voyage"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (voyage_error.AuthenticationError("bad key"), LLMProviderAuthError),
        (voyage_error.RateLimitError("slow down"), LLMProviderRateLimitError),
        (voyage_error.Timeout("too slow"), LLMProviderTimeoutError),
        (voyage_error.ServiceUnavailableError("down"), LLMProviderUnavailableError),
        (voyage_error.APIConnectionError("unreachable"), LLMProviderUnavailableError),
        (voyage_error.InvalidRequestError("bad payload"), LLMProviderBadRequestError),
        (voyage_error.MalformedRequestError("malformed"), LLMProviderBadRequestError),
    ],
)
def test_classify_voyage_exception(exc: Exception, expected: type) -> None:
    translated = _classify_voyage_exception(exc)
    assert isinstance(translated, expected)


def test_classify_voyage_exception_unknown_shape_returns_none() -> None:
    assert _classify_voyage_exception(ValueError("unrelated")) is None


@pytest.mark.asyncio
async def test_embed_returns_vectors_in_input_order() -> None:
    fake_result = MagicMock()
    fake_result.embeddings = [[0.1, 0.2], [0.3, 0.4]]
    fake_client = MagicMock()
    fake_client.embed.return_value = fake_result

    with patch(
        "agentive_backend.infra.llm.voyage_adapter.VoyageClient", return_value=fake_client
    ) as client_cls:
        provider = VoyageProvider(api_key="vk-test")
        vectors = await provider.embed(["a", "b"], model=EMBEDDING_MODEL_NAME, timeout_s=12.0)

    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    client_cls.assert_called_once_with(api_key="vk-test", timeout=12.0)
    fake_client.embed.assert_called_once_with(["a", "b"], model=EMBEDDING_MODEL_NAME)


@pytest.mark.asyncio
async def test_embed_translates_voyage_exceptions() -> None:
    fake_client = MagicMock()
    fake_client.embed.side_effect = voyage_error.RateLimitError("slow down")

    with patch("agentive_backend.infra.llm.voyage_adapter.VoyageClient", return_value=fake_client):
        provider = VoyageProvider(api_key="vk-test")
        with pytest.raises(LLMProviderRateLimitError):
            await provider.embed(["a"], model=EMBEDDING_MODEL_NAME)


@pytest.mark.asyncio
async def test_embed_reraises_unclassified_exceptions_raw() -> None:
    fake_client = MagicMock()
    fake_client.embed.side_effect = ValueError("totally unexpected")

    with patch("agentive_backend.infra.llm.voyage_adapter.VoyageClient", return_value=fake_client):
        provider = VoyageProvider(api_key="vk-test")
        with pytest.raises(ValueError, match="totally unexpected"):
            await provider.embed(["a"], model=EMBEDDING_MODEL_NAME)
