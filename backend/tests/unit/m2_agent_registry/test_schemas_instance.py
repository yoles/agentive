"""Unit tests — Pydantic schemas for agent instances (Story 2.4 T2.2).

Covers ``InstantiateTemplateRequest`` (extra=forbid + UUID nullable) and
``AgentInstanceDetailResponse`` (response shape, snapshot is free dict).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentive_backend.features.m2_agent_registry.schemas import (
    AgentInstanceDetailResponse,
    InstantiateTemplateRequest,
    InstantiateTemplateResponse,
)


def test_instantiate_request_accepts_empty_body() -> None:
    """Empty body is the canonical "instance hors workflow" shape."""
    req = InstantiateTemplateRequest()
    assert req.workflow_run_id is None


def test_instantiate_request_accepts_explicit_null_workflow_run_id() -> None:
    """Explicit null is equivalent to omitting the field."""
    req = InstantiateTemplateRequest.model_validate({"workflow_run_id": None})
    assert req.workflow_run_id is None


def test_instantiate_request_accepts_valid_uuid() -> None:
    run_id = uuid4()
    req = InstantiateTemplateRequest.model_validate({"workflow_run_id": str(run_id)})
    assert req.workflow_run_id == run_id


def test_instantiate_request_rejects_extra_field() -> None:
    """extra=forbid prevents callers from injecting snapshot/template_id/etc."""
    with pytest.raises(ValidationError, match="extra"):
        InstantiateTemplateRequest.model_validate(
            {"workflow_run_id": None, "snapshot": {"hack": True}}
        )


def test_instance_detail_response_round_trip() -> None:
    """The response carries all the fields the client needs to render."""
    instance_id = uuid4()
    template_id = uuid4()
    run_id = uuid4()
    payload = {
        "instance_id": str(instance_id),
        "template_id": str(template_id),
        "template_version": 7,
        "workflow_run_id": str(run_id),
        "snapshot": {
            "template_id": str(template_id),
            "template_version": 7,
            "name": "x",
            "archetype": "y",
            "config": {"system_prompt": "z"},
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    resp = AgentInstanceDetailResponse.model_validate(payload)
    assert resp.instance_id == instance_id
    assert resp.template_version == 7
    assert resp.workflow_run_id == run_id
    assert resp.snapshot["config"]["system_prompt"] == "z"


def test_instantiate_response_subclass_preserves_shape() -> None:
    """P-08 (CR 2026-05-10) — `InstantiateTemplateResponse` est désormais une
    SUBCLASS (vs alias plain) pour préserver son nom dans le composant OpenAPI.
    Le shape reste identique car la subclass n'ajoute aucun champ.
    """
    assert issubclass(InstantiateTemplateResponse, AgentInstanceDetailResponse)
    # Same fields exactly — subclass adds nothing.
    assert (
        set(InstantiateTemplateResponse.model_fields.keys())
        == set(AgentInstanceDetailResponse.model_fields.keys())
    )
    # Distinct __name__ for OpenAPI schema components.
    assert InstantiateTemplateResponse.__name__ == "InstantiateTemplateResponse"
    assert AgentInstanceDetailResponse.__name__ == "AgentInstanceDetailResponse"
