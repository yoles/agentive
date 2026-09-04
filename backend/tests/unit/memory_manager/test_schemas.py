"""Unit: input bounds on ``/api/v1/memory/*`` request schemas.

Added by the Story 3.1 code review (P1/P2/P3/P8). Each case below used to
be accepted by the schema and blow up further down as a raw 500: an int32
overflow at insert, an `OverflowError` in `timedelta`, a NUL byte Postgres
refuses, a lone surrogate that breaks `str.encode`, or a silently
chunk-and-averaged embedding for an oversized `content`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentive_backend.features.memory_manager.schemas import (
    CONTENT_MAX_CHARS,
    TTL_MAX_SECONDS,
    CreateMemoryChunkRequest,
)


def _body(**overrides: object) -> dict[str, object]:
    return {"content": "hello", "namespace": "ns", **overrides}


def test_accepts_a_well_formed_body() -> None:
    req = CreateMemoryChunkRequest(**_body(ttl=3600))  # type: ignore[arg-type]
    assert req.content == "hello"
    assert req.ttl == 3600


@pytest.mark.parametrize(
    "ttl",
    [
        pytest.param(3_000_000_000, id="overflows-int32-column"),
        pytest.param(300_000_000_000, id="overflows-datetime-max"),
        pytest.param(TTL_MAX_SECONDS + 1, id="just-past-the-cap"),
    ],
)
def test_rejects_ttl_above_the_cap(ttl: int) -> None:
    with pytest.raises(PydanticValidationError):
        CreateMemoryChunkRequest(**_body(ttl=ttl))  # type: ignore[arg-type]


def test_rejects_negative_ttl() -> None:
    with pytest.raises(PydanticValidationError):
        CreateMemoryChunkRequest(**_body(ttl=-1))  # type: ignore[arg-type]


def test_accepts_ttl_at_the_cap() -> None:
    assert CreateMemoryChunkRequest(**_body(ttl=TTL_MAX_SECONDS)).ttl == TTL_MAX_SECONDS  # type: ignore[arg-type]


def test_rejects_content_above_the_embedding_context_window() -> None:
    with pytest.raises(PydanticValidationError):
        CreateMemoryChunkRequest(**_body(content="x" * (CONTENT_MAX_CHARS + 1)))  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["content", "namespace"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("   ", id="whitespace-only"),
        pytest.param("\t\n ", id="tabs-and-newlines"),
        pytest.param("a\x00b", id="nul-byte"),
        pytest.param("\ud800", id="lone-surrogate"),
    ],
)
def test_rejects_unstorable_text(field: str, value: str) -> None:
    with pytest.raises(PydanticValidationError):
        CreateMemoryChunkRequest(**_body(**{field: value}))  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["content", "namespace"])
def test_strips_surrounding_whitespace(field: str) -> None:
    req = CreateMemoryChunkRequest(**_body(**{field: "  padded  "}))  # type: ignore[arg-type]
    assert getattr(req, field) == "padded"
