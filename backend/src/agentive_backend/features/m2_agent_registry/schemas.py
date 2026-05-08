"""Pydantic v2 schemas — request/response models for ``/api/v1/agents/*``.

All models use ``ConfigDict(extra="forbid")`` to reject unexpected fields
(Story 2.1 §"Pièges connus" #8 — anti prompt-injection on ``config``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Archetypes — read-only, sourced from the YAML registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ArchetypeSummary(BaseModel):
    """List view — payload kept lean for the ArchetypeSelector grid (UX-DR17).

    Drops ``prompt_base`` and contracts to avoid shipping ~2 KB per item on
    the list endpoint. Detail endpoint exposes them.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    icon_name: str
    description: str
    default_role: str


class ContractSkeletonView(BaseModel):
    """Mirror of ``archetypes.ContractSkeleton`` — separate type to keep the
    API layer decoupled from the loader's internals."""

    model_config = ConfigDict(extra="forbid")

    core: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)


class ArchetypeDetail(BaseModel):
    """Detail view — adds ``prompt_base`` and contract skeletons for the
    preview pane (UX-DR17 right panel)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    icon_name: str
    description: str
    default_role: str
    prompt_base: str
    input_contract: ContractSkeletonView
    output_contract: ContractSkeletonView


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent template — create
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class CreateTemplateRequest(BaseModel):
    """Body of ``POST /api/v1/agents/templates``.

    ``extra="forbid"`` rejects any caller-injected ``config`` / ``version`` /
    ``id`` field — those are derived server-side from the archetype.

    Story 2.1 P-05 — ``name`` is auto-trimmed and re-validated against
    ``min_length=1`` post-trim, so ``"   "`` is rejected as 422 instead of
    persisted as three spaces in the DB.
    """

    model_config = ConfigDict(extra="forbid")

    archetype: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name", mode="after")
    @classmethod
    def _strip_and_revalidate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            # Pydantic surfaces this as a `value_error` with a clear msg;
            # the global RFC 7807 422 handler relays it as `errors[*]`.
            raise ValueError("name must not be blank or whitespace-only")
        return stripped


class CreateTemplateResponse(BaseModel):
    """Response of ``POST /api/v1/agents/templates`` (201 Created)."""

    model_config = ConfigDict(extra="forbid")

    template_id: UUID
    name: str
    archetype: str
    version: int
    created_at: datetime


__all__ = [
    "ArchetypeDetail",
    "ArchetypeSummary",
    "ContractSkeletonView",
    "CreateTemplateRequest",
    "CreateTemplateResponse",
]
