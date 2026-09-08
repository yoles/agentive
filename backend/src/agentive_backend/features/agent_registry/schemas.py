"""Pydantic v2 schemas — request/response models for ``/api/v1/agents/*``.

All models use ``ConfigDict(extra="forbid")`` to reject unexpected fields
(Story 2.1 §"Pièges connus" #8 — anti prompt-injection on ``config``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentive_backend.features.agent_registry.domain import value_objects as vo

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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent template — update (Story 2.2)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


# Sprint 1 whitelist. Will grow when new providers are wired (Story 4.6).
LLMModel = Literal[
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "gpt-4o",
    "gpt-4o-mini",
]

# Provider chain Sprint 1 — runtime fallback not active (config-only, deferred Story 4.6).
ProviderId = Literal["anthropic", "openai"]


class LLMParams(BaseModel):
    """Hyperparamètres LLM — bounded to keep config sane."""

    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=200_000)

    def to_domain(self) -> vo.LLMParams:
        """Convert this HTTP DTO into the framework-free domain VO."""
        return vo.LLMParams(temperature=self.temperature, max_tokens=self.max_tokens)


class ContractDefinition(BaseModel):
    """Contrat élastique — ``core`` typé runtime, ``extras`` zone permissive (H8 architecture)."""

    model_config = ConfigDict(extra="forbid")

    core: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> vo.Contract:
        """Convert this HTTP DTO into the framework-free domain VO."""
        return vo.Contract(core=dict(self.core), extras=dict(self.extras))


class ErrorPolicy(BaseModel):
    """Politique d'erreur déclarative — NFR14 retry exponential backoff.

    Sprint 1 valide + persiste UNIQUEMENT le shape ; le dispatcher actif
    (qui APPLIQUE le retry au runtime) arrive Story 4.6.
    """

    model_config = ConfigDict(extra="forbid")

    on_timeout: Literal["retry_with_backoff", "fail_fast", "fallback_provider"] = (
        "retry_with_backoff"
    )
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_strategy: Literal["exponential", "linear", "constant"] = "exponential"

    def to_domain(self) -> vo.ErrorPolicy:
        """Convert this HTTP DTO into the framework-free domain VO."""
        return vo.ErrorPolicy(
            on_timeout=vo.OnTimeoutPolicy(self.on_timeout),
            max_retries=self.max_retries,
            backoff_strategy=vo.BackoffStrategy(self.backoff_strategy),
        )


class PushMemoryConfig(BaseModel):
    """Push Memory config d'un template — Story 3.5 AC2, FR20.

    ``namespace`` déclare le namespace mémoire à interroger avant chaque run ;
    ``optin`` lève l'opt-out par défaut des Contrôleurs (AC2).

    ⚠️ **Les deux champs sont requis** (revue Story 3.5, IG2), contrairement
    à ``LLMParams``/``ErrorPolicy`` dont cette classe est par ailleurs le
    mirror. Raison : ce bloc est remplacé EN ENTIER par ``merge_updates``
    (sémantique PATCH au premier niveau seulement), et ici les valeurs par
    défaut ne sont pas anodines : ce sont les valeurs "éteint". Avec des
    champs optionnels, ``PUT {"push_memory": {"optin": true}}`` sur un
    template déjà configuré effaçait silencieusement son ``namespace`` et
    désactivait la feature, en répondant 200. Les défauts de ``LLMParams``
    (``0.7``/``4096``) sont eux des valeurs de travail valides, d'où
    l'asymétrie assumée.

    Un remplacement doit donc énoncer l'intention complète :

    * activer          : ``{"namespace": "team-alpha", "optin": true}``
    * activer sans opt-in (archétypes non-Contrôleur, où ``optin`` est
      ignoré) : ``{"namespace": "team-alpha", "optin": false}``
    * éteindre         : ``{"namespace": null, "optin": false}``

    ``optin: true`` sans ``namespace`` est refusé en 422 : s'abonner à rien
    n'est pas un état exprimable, c'est la faute de frappe que IG2 décrit.
    """

    model_config = ConfigDict(extra="forbid")

    namespace: str | None = Field(min_length=1, max_length=255)
    optin: bool

    @model_validator(mode="after")
    def _optin_requires_a_namespace(self) -> PushMemoryConfig:
        if self.optin and self.namespace is None:
            raise ValueError("push_memory.namespace is required when push_memory.optin is true")
        return self

    def to_domain(self) -> vo.PushMemorySettings:
        """Convert this HTTP DTO into the framework-free domain VO."""
        return vo.PushMemorySettings(namespace=self.namespace, optin=self.optin)


class UpdateTemplateRequest(BaseModel):
    """Body of ``PUT /api/v1/agents/templates/{id}`` — PATCH-like sémantique.

    Tous les champs sont **optionnels** : seuls les champs présents écrasent
    la valeur précédente dans ``config``. Une nouvelle row ``prompts`` (bump
    version) n'est insérée QUE SI ``system_prompt`` est dans le payload **ET**
    diffère de la valeur courante (P-01 fix — comparaison côté service).

    ``extra="forbid"`` rejette tout champ inconnu (P-06 PII defense + scope).

    P-03 fix — un payload entièrement vide (`{}`) ou avec UNIQUEMENT des champs
    `null` est rejeté en 422 RFC 7807 plutôt que de produire un audit event
    spurious sur un UPDATE no-op.
    """

    model_config = ConfigDict(extra="forbid")

    system_prompt: str | None = Field(default=None, min_length=1, max_length=50_000)
    input_contract: ContractDefinition | None = None
    output_contract: ContractDefinition | None = None
    llm_model: LLMModel | None = None
    llm_params: LLMParams | None = None
    provider_chain: list[ProviderId] | None = Field(default=None, min_length=1, max_length=4)
    error_policy: ErrorPolicy | None = None
    push_memory: PushMemoryConfig | None = None

    @field_validator("provider_chain", mode="after")
    @classmethod
    def _no_duplicates_in_provider_chain(cls, value: list[str] | None) -> list[str] | None:
        """P-13 fix — un même provider présent deux fois dans la chain est
        une erreur de config (le runtime fallback Story 4.6 retry-erait sur
        le même provider, défaisant la stratégie de chain). Reject early.
        """
        if value is not None and len(value) != len(set(value)):
            raise ValueError("provider_chain must not contain duplicate providers")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateTemplateRequest:
        """P-03 fix — refuse les payloads complètement vides ; un PUT sans
        intention claire produit un audit event spurious. Au moins un champ
        non-None requis."""
        if all(
            getattr(self, field) is None
            for field in (
                "system_prompt",
                "input_contract",
                "output_contract",
                "llm_model",
                "llm_params",
                "provider_chain",
                "error_policy",
                "push_memory",
            )
        ):
            raise ValueError("at least one field must be provided in the update payload")
        return self


class TemplateDetailResponse(BaseModel):
    """Response of ``GET /api/v1/agents/templates/{id}`` (Story 2.2)."""

    model_config = ConfigDict(extra="forbid")

    template_id: UUID
    name: str
    archetype: str
    version: int
    config: dict[str, Any]
    created_at: datetime


class UpdateTemplateResponse(BaseModel):
    """Response of ``PUT /api/v1/agents/templates/{id}`` (200 OK)."""

    model_config = ConfigDict(extra="forbid")

    template_id: UUID
    name: str
    archetype: str
    version: int
    config: dict[str, Any]
    updated_at: datetime


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Agent instance — instantiate from template (Story 2.4)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class InstantiateTemplateRequest(BaseModel):
    """Body of ``POST /api/v1/agents/templates/{template_id}/instances`` (Story 2.4).

    Empty body (``{}``) or ``{"workflow_run_id": null}`` both create an
    instance that is **not** rattached to any workflow_run (Playground
    Story 2.7 + tests d'isolation use this).

    ``extra="forbid"`` rejects any unknown field — the snapshot capture
    is server-driven (the caller cannot inject ``snapshot``/``template_id``
    /``template_version`` ; those are derived from the URL ``template_id``
    + the current ``agent_templates`` row).
    """

    model_config = ConfigDict(extra="forbid")

    workflow_run_id: UUID | None = None


class AgentInstanceDetailResponse(BaseModel):
    """Response for ``GET /api/v1/agents/instances/{instance_id}`` (Story 2.4).

    The ``snapshot`` is a free-shape JSONB blob — see
    :class:`AgentRegistryService.instantiate_from_template` for the
    canonical Sprint 1 keys (``template_id``, ``template_version``,
    ``name``, ``archetype``, ``config``).

    P-05 (CR 2026-05-10) — Pas de ``extra="forbid"`` ici : pour un response
    model, c'est défensivement contre-productif. Si une évolution ORM ajoute
    un champ que le serveur sérialise par erreur, on préfère propager
    silencieusement (champ ignoré) plutôt que crash 500. ``extra="forbid"``
    reste sur les request models (anti prompt-injection).
    """

    model_config = ConfigDict(extra="ignore")

    instance_id: UUID
    template_id: UUID
    template_version: int
    workflow_run_id: UUID | None
    snapshot: dict[str, Any]
    created_at: datetime


class InstantiateTemplateResponse(AgentInstanceDetailResponse):
    """Response for ``POST /api/v1/agents/templates/{id}/instances`` (201).

    P-08 (CR 2026-05-10) — Wrapper class plutôt qu'alias plain. FastAPI
    génère le composant OpenAPI à partir du ``__name__`` Python : avec un
    alias ``InstantiateTemplateResponse = AgentInstanceDetailResponse``,
    les SDK generators perdaient le nom ``InstantiateTemplateResponse``
    (les 2 routes pointaient vers le même schema component). La classe
    wrapper préserve les 2 noms distincts dans ``components/schemas`` sans
    dupliquer le shape.
    """


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Story 2.5 — Tool assignment to agent_templates (junction table)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ReplaceAgentToolsRequest(BaseModel):
    """Body of ``POST /api/v1/agents/templates/{template_id}/tools`` (Story 2.5 AC2).

    REPLACE semantics : the ``tool_ids`` list REPLACES the current
    assignments for this template. Tools previously assigned but not
    listed here are unassigned. Tools not previously assigned but listed
    here are assigned. Both diffs happen atomically (single-tx).

    Defensive ``max_length=100`` — Sprint 1 we don't expect more than a
    handful of tools per template ; a 100-cap rejects pathological payloads
    early. Empty list is valid (clear all assignments).
    """

    model_config = ConfigDict(extra="forbid")

    tool_ids: list[UUID] = Field(default_factory=list, max_length=100)


class AssignedToolView(BaseModel):
    """A single tool currently assigned to a template, with its server
    metadata for UX grouping and its assignment timestamp.

    P-01 (CR 2026-05-10) — fields aligned with AC2 contract: includes
    ``input_schema``/``output_schema`` (denormalized from ``ToolView`` to
    avoid a cross-feature m5 schema import) and ``assigned_at`` (joined
    from the junction row by ``AgentTemplateToolRepo.list_by_template_in_session``).
    """

    model_config = ConfigDict(extra="ignore")

    tool_id: UUID
    name: str
    description: str
    server_id: UUID
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    assigned_at: datetime


class AgentToolsResponse(BaseModel):
    """Response for ``POST /api/v1/agents/templates/{id}/tools`` (200 OK)
    and ``GET /api/v1/agents/templates/{id}/tools`` (Story 2.5 AC2 + AC4).

    The ``assigned_tools`` list is flat ; the frontend groups by
    ``server_id`` for the UX (UX-DR §"Tool grouping").
    """

    model_config = ConfigDict(extra="ignore")

    template_id: UUID
    assigned_tools: list[AssignedToolView]


class DiversityCheckResponse(BaseModel):
    """Response for ``GET .../templates/{controller_template_id}/diversity-check``
    (Story 2.8 AC3, FR15).

    ``*_params`` reuse the existing :class:`LLMParams` shape read-only.
    ``is_diverse`` is a three-state field (``bool | None``). ``None`` means
    one of the two templates has an incomplete LLM config (cf
    ``domain.diversity.check_llm_diversity``), never a silent ``False``.

    ``*_archetype`` echo back the pair the verdict was computed on (Story 2.8
    I-01) : the controller side is enforced to be ``controleur`` by the
    service, the controlled side is free.
    """

    model_config = ConfigDict(extra="forbid")

    controller_archetype: str
    producer_archetype: str
    controller_model: str | None
    controller_params: LLMParams | None
    producer_model: str | None
    producer_params: LLMParams | None
    is_diverse: bool | None
    reason: str


__all__ = [
    "AgentInstanceDetailResponse",
    "AgentToolsResponse",
    "ArchetypeDetail",
    "ArchetypeSummary",
    "AssignedToolView",
    "ContractDefinition",
    "ContractSkeletonView",
    "CreateTemplateRequest",
    "CreateTemplateResponse",
    "DiversityCheckResponse",
    "ErrorPolicy",
    "InstantiateTemplateRequest",
    "InstantiateTemplateResponse",
    "LLMModel",
    "LLMParams",
    "ProviderId",
    "PushMemoryConfig",
    "ReplaceAgentToolsRequest",
    "TemplateDetailResponse",
    "UpdateTemplateRequest",
    "UpdateTemplateResponse",
]
