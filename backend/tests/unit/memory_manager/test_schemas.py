"""Unit: input bounds on ``/api/v1/memory/*`` request schemas.

Added by the Story 3.1 code review (P1/P2/P3/P8). Each case below used to
be accepted by the schema and blow up further down as a raw 500: an int32
overflow at insert, an `OverflowError` in `timedelta`, a NUL byte Postgres
refuses, a lone surrogate that breaks `str.encode`, or a silently
chunk-and-averaged embedding for an oversized `content`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from agentive_backend.features.memory_manager.schemas import (
    CONTENT_MAX_CHARS,
    TTL_MAX_SECONDS,
    CreateMemoryChunkRequest,
    CreateNamespaceRequest,
    DecayPolicyOverride,
    MemorySearchResultView,
    RetentionPolicyOverride,
    SearchMemoryRequest,
    UpdateNamespaceRequest,
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


def test_rejects_a_zero_second_ttl() -> None:
    """A chunk that expires at the instant it is written is never the intent
    (code review Story 3.3, IG1). Rejected here so the domain floor cannot
    surface as a 500."""
    with pytest.raises(PydanticValidationError):
        CreateMemoryChunkRequest(**_body(ttl=0))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["default_ttl_seconds", "archive_after_seconds"],
)
def test_retention_override_rejects_zero_seconds(field: str) -> None:
    """`archive_after_seconds=0` would archive every chunk of the namespace on
    the next pass, `default_ttl_seconds=0` expires them at write time
    (code review Story 3.3, IG1)."""
    with pytest.raises(PydanticValidationError):
        RetentionPolicyOverride(**{field: 0})  # type: ignore[arg-type]


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
        CreateNamespaceRequest(name="ns", type="client", something_else="nope")  # type: ignore[call-arg]


def test_create_namespace_accepts_embedding_backend() -> None:
    """Story 3.6 AC2/AC3 — `embedding_backend` is now a real field (it used
    to be rejected by `extra="forbid"`, Story 3.1-3.5)."""
    req = CreateNamespaceRequest(name="ns", type="client", embedding_backend="local")  # type: ignore[arg-type]
    assert req.embedding_backend.value == "local"


def test_create_namespace_defaults_embedding_backend_to_cloud() -> None:
    req = CreateNamespaceRequest(name="ns", type="client")  # type: ignore[arg-type]
    assert req.embedding_backend.value == "cloud"


def test_create_namespace_rejects_unknown_embedding_backend() -> None:
    with pytest.raises(PydanticValidationError):
        CreateNamespaceRequest(name="ns", type="client", embedding_backend="quantum")  # type: ignore[arg-type]


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


# ─── Story 3.4 — rerank flag + DecayPolicyOverride ────────────────


def test_search_memory_rerank_defaults_to_true() -> None:
    assert SearchMemoryRequest(q="hello", namespace="ns").rerank is True


def test_search_memory_accepts_explicit_rerank_false() -> None:
    assert SearchMemoryRequest(q="hello", namespace="ns", rerank=False).rerank is False


@pytest.mark.parametrize(
    "payload",
    [
        {"function": "none"},
        {"function": "exponential", "half_life_seconds": 2_592_000},
        {"function": "linear", "horizon_seconds": 86_400},
        {"function": "step", "threshold_seconds": 3_600, "factor": 0.25},
    ],
)
def test_decay_policy_override_accepts_every_well_formed_function(
    payload: dict[str, object],
) -> None:
    assert DecayPolicyOverride(**payload).function == payload["function"]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "payload",
    [
        # missing the parameter its function requires
        {"function": "exponential"},
        {"function": "linear"},
        {"function": "step", "threshold_seconds": 60},
        {"function": "step", "factor": 0.5},
        # carrying a parameter that belongs to another function
        {"function": "exponential", "half_life_seconds": 60, "horizon_seconds": 60},
        {"function": "linear", "horizon_seconds": 60, "factor": 0.5},
        {"function": "step", "threshold_seconds": 60, "factor": 0.5, "half_life_seconds": 60},
        {"function": "none", "half_life_seconds": 60},
        # out-of-bounds values
        {"function": "exponential", "half_life_seconds": 0},
        {"function": "step", "threshold_seconds": 60, "factor": 1.5},
        {"function": "step", "threshold_seconds": 60, "factor": -0.1},
        # unknown function
        {"function": "sigmoid", "half_life_seconds": 60},
    ],
)
def test_decay_policy_override_rejects_incoherent_bodies(payload: dict[str, object]) -> None:
    """422 at the HTTP boundary, never a `DomainValidationError` escaping as

    a 500 further in — the reason these rules are duplicated from the
    domain VO on purpose.
    """
    with pytest.raises(PydanticValidationError):
        DecayPolicyOverride(**payload)  # type: ignore[arg-type]


def test_decay_policy_override_rejects_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        DecayPolicyOverride(function="none", lambda_=0.1)  # type: ignore[call-arg]


def test_create_namespace_decay_policy_is_optional() -> None:
    assert CreateNamespaceRequest(name="ns", type="metier").decay_policy is None


def test_create_namespace_accepts_a_decay_policy() -> None:
    body = CreateNamespaceRequest(
        name="ns",
        type="metier",
        decay_policy=DecayPolicyOverride(function="exponential", half_life_seconds=60),
    )
    assert body.decay_policy is not None
    assert body.decay_policy.half_life_seconds == 60


def test_search_result_view_carries_both_score_terms() -> None:
    view = MemorySearchResultView(
        chunk_id=uuid4(),
        content="x",
        score=0.45,
        similarity=0.9,
        decay_factor=0.5,
        namespace="ns",
        created_at=datetime.now(UTC),
    )
    assert view.similarity == 0.9
    assert view.decay_factor == 0.5
    assert view.score == 0.45


# ─── UpdateNamespaceRequest — Story 3.4, code review BS1 ──────────


def test_update_namespace_request_accepts_a_policy() -> None:
    body = UpdateNamespaceRequest.model_validate(
        {"decay_policy": {"function": "exponential", "half_life_seconds": 86_400}}
    )

    assert body.decay_policy is not None
    assert body.decay_policy.half_life_seconds == 86_400


def test_update_namespace_request_accepts_null_to_clear_the_policy() -> None:
    """`null` is meaningful here, not "unset": it is how an operator turns

    decay back off on a namespace that has it.
    """
    assert UpdateNamespaceRequest.model_validate({"decay_policy": None}).decay_policy is None


def test_update_namespace_request_requires_the_field() -> None:
    with pytest.raises(PydanticValidationError):
        UpdateNamespaceRequest.model_validate({})


def test_update_namespace_request_forbids_unknown_fields() -> None:
    """`retention_policy` is deliberately NOT mutable here — a typo must be a

    422, not a silently ignored field.
    """
    with pytest.raises(PydanticValidationError):
        UpdateNamespaceRequest.model_validate(
            {"decay_policy": None, "retention_policy": {"default_ttl_seconds": 60}}
        )


def test_update_namespace_request_rejects_an_incoherent_policy() -> None:
    with pytest.raises(PydanticValidationError):
        UpdateNamespaceRequest.model_validate(
            {"decay_policy": {"function": "exponential", "horizon_seconds": 60}}
        )


def test_update_namespace_request_forbids_embedding_backend() -> None:
    """Story 3.6 T9.2 — `embedding_backend` is immutable after creation
    (changing it on a namespace with existing chunks would make them
    invisible to search, § Symétrie write/read). `extra="forbid"` already
    rejects it with no dedicated field needed."""
    with pytest.raises(PydanticValidationError):
        UpdateNamespaceRequest.model_validate({"decay_policy": None, "embedding_backend": "local"})
