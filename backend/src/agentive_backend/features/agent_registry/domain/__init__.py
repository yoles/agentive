"""Domain core for the Agent Registry bounded context.

Framework-free layer (dataclasses + :class:`enum.StrEnum`, no Pydantic, no
SQLAlchemy) introduced by the Sprint 2 technical audit ("domaine anémique").
It owns the business invariants that used to be scattered across the Pydantic
schemas, the imperative service code, and the ORM:

* :mod:`.value_objects` — Palier 1 : ``Version``, ``Archetype``, ``Contract``,
  ``LLMParams``, ``ErrorPolicy``, ``ProviderChain`` composed into
  ``AgentConfig`` with a single parse point (``from_mapping``) and a single
  serialize point (``to_mapping``).
* :mod:`.aggregates` — Palier 2 : ``AgentTemplate.revise()`` encapsulates the
  "system_prompt change ⇒ version bump + prompt revision" rule, unit-testable
  without a DB.
* :mod:`.diversity` — Story 2.8, FR15 : ``check_llm_diversity`` compares a
  Contrôleur and a Producteur ``AgentConfig`` and returns a three-state
  ``DiversityCheckResult`` (diverse / identical / config incomplete).

Wiring status
-------------
``AgentRegistryService.update_template`` rehydrates the aggregate from the
persisted row (built inline from primitive ORM attributes — no ``infra.db``
import needed, so the layering contract stays trivially satisfied) and drives
the update through ``AgentConfig.merge_updates`` + ``AgentTemplate.revise``.

Deferred to a later Palier-2 slice (see
``docs/decisions/agent-registry-domain-core.md``): persistence ``Protocol``
ports (audit A-11), a dedicated ORM↔domain ``mapping`` module, the service
class split (A-10) and the ``require_by_id`` lookup-or-404 helper (A-07).
"""

from __future__ import annotations

from agentive_backend.features.agent_registry.domain.aggregates import (
    AgentTemplate,
    PromptRevision,
)
from agentive_backend.features.agent_registry.domain.diversity import (
    DiversityCheckResult,
    check_llm_diversity,
)
from agentive_backend.features.agent_registry.domain.value_objects import (
    AgentConfig,
    Archetype,
    BackoffStrategy,
    Contract,
    DomainValidationError,
    ErrorPolicy,
    LLMParams,
    OnTimeoutPolicy,
    ProviderChain,
    ProviderId,
    Version,
)

__all__ = [
    "AgentConfig",
    "AgentTemplate",
    "Archetype",
    "BackoffStrategy",
    "Contract",
    "DiversityCheckResult",
    "DomainValidationError",
    "ErrorPolicy",
    "LLMParams",
    "OnTimeoutPolicy",
    "PromptRevision",
    "ProviderChain",
    "ProviderId",
    "Version",
    "check_llm_diversity",
]
