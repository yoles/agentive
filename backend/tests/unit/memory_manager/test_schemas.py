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
    CreateNamespaceRequest,
    RetentionPolicyOverride,
    SearchMemoryRequest,
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


# ─── CreateNamespaceRequest (Story 3.2 AC1) ────────────────────────


def test_create_namespace_accepts_a_well_formed_body() -> None:
    req = CreateNamespaceRequest(name="dev-notes", type="metier", department="Dev")  # type: ignore[arg-type]
    assert req.name == "dev-notes"
    assert req.type == "metier"
    assert req.retention_policy is None


def test_create_namespace_rejects_invalid_type() -> None:
    with pytest.raises(PydanticValidationError):
        CreateNamespaceRequest(name="ns", type="banana")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("   ", id="whitespace-only"),
        pytest.param("a\x00b", id="nul-byte"),
        pytest.param("\ud800", id="lone-surrogate"),
    ],
)
def test_create_namespace_rejects_unstorable_name(value: str) -> None:
    with pytest.raises(PydanticValidationError):
        CreateNamespaceRequest(name=value, type="client")  # type: ignore[arg-type]


def test_create_namespace_rejects_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        CreateNamespaceRequest(name="ns", type="client", embedding_backend="local")  # type: ignore[call-arg]


def test_create_namespace_accepts_explicit_retention_policy_override() -> None:
    req = CreateNamespaceRequest(
        name="ns",
        type="client",  # type: ignore[arg-type]
        retention_policy=RetentionPolicyOverride(default_ttl_seconds=3600),
    )
    assert req.retention_policy is not None
    assert req.retention_policy.default_ttl_seconds == 3600


def test_retention_policy_override_rejects_negative_ttl() -> None:
    with pytest.raises(PydanticValidationError):
        RetentionPolicyOverride(default_ttl_seconds=-1)


def test_retention_policy_override_rejects_ttl_above_cap() -> None:
    with pytest.raises(PydanticValidationError):
        RetentionPolicyOverride(default_ttl_seconds=TTL_MAX_SECONDS + 1)


def test_retention_policy_override_rejects_empty_object() -> None:
    """`{}` used to silently void the type default (code review Story 3.2, BS1)."""
    with pytest.raises(PydanticValidationError):
        RetentionPolicyOverride()


def test_create_namespace_rejects_empty_retention_policy_object() -> None:
    with pytest.raises(PydanticValidationError):
        CreateNamespaceRequest(
            name="ns",
            type="client",  # type: ignore[arg-type]
            retention_policy={},  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("field", ["department", "project"])
def test_create_namespace_rejects_nul_byte_in_department_or_project(field: str) -> None:
    """Only `name` was guarded against unstorable text; a NUL byte in

    `department`/`project` used to reach `session.flush()` and 500
    instead of 422 (code review Story 3.2, P4).
    """
    with pytest.raises(PydanticValidationError):
        CreateNamespaceRequest(
            name="ns",
            type="client",  # type: ignore[arg-type]
            **{field: "a\x00b"},
        )


@pytest.mark.parametrize("field", ["department", "project"])
def test_create_namespace_rejects_blank_department_or_project(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        CreateNamespaceRequest(
            name="ns",
            type="client",  # type: ignore[arg-type]
            **{field: "   "},
        )


def test_create_namespace_accepts_omitted_department_and_project() -> None:
    req = CreateNamespaceRequest(name="ns", type="client")  # type: ignore[arg-type]
    assert req.department is None
    assert req.project is None


# ─── SearchMemoryRequest.include_archived (Story 3.3 AC2) ──────────


def test_search_memory_include_archived_defaults_to_false() -> None:
    req = SearchMemoryRequest(q="hello", namespace="ns")
    assert req.include_archived is False


def test_search_memory_accepts_explicit_include_archived_true() -> None:
    req = SearchMemoryRequest(q="hello", namespace="ns", include_archived=True)
    assert req.include_archived is True
