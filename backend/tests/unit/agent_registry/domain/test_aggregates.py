"""Unit tests — ``AgentTemplate.revise`` (Sprint 2 DDD Palier 2).

These 4 cases cover the exact versioning rule that used to live imperatively
inside ``AgentRegistryService.update_template`` (service.py:291-333), now
testable without a DB, a session mock, or the event bus.
"""

from __future__ import annotations

from uuid import uuid4

from agentive_backend.features.agent_registry.domain.aggregates import AgentTemplate
from agentive_backend.features.agent_registry.domain.value_objects import (
    AgentConfig,
    Archetype,
    LLMParams,
    Version,
)


def _template(*, system_prompt: str | None, version: int = 1) -> AgentTemplate:
    raw: dict = {"prompt_base": "base", "role": "producer"}
    if system_prompt is not None:
        raw["system_prompt"] = system_prompt
    return AgentTemplate(
        id=uuid4(),
        name="Code Producer",
        archetype=Archetype.PRODUCTEUR,
        version=Version(version),
        config=AgentConfig.from_mapping(raw),
    )


def test_revise_bumps_version_and_returns_revision_when_system_prompt_changes() -> None:
    template = _template(system_prompt="v1 prompt", version=1)
    new_config = template.config.merge_updates(system_prompt="v2 prompt")

    revision = template.revise(new_config)

    assert revision is not None
    assert template.version == Version(2)
    assert revision.version == Version(2)
    assert revision.content == "v2 prompt"
    assert revision.template_id == template.id
    assert template.config.system_prompt == "v2 prompt"


def test_revise_no_bump_when_system_prompt_identical() -> None:
    template = _template(system_prompt="same", version=3)
    new_config = template.config.merge_updates(
        system_prompt="same",
        llm_params=LLMParams(temperature=0.9, max_tokens=1024),
    )

    revision = template.revise(new_config)

    assert revision is None
    assert template.version == Version(3)  # no bump
    # Other fields still applied even without a bump.
    assert template.config.llm_params == LLMParams(temperature=0.9, max_tokens=1024)


def test_revise_no_bump_when_system_prompt_absent() -> None:
    template = _template(system_prompt=None, version=5)
    new_config = template.config.merge_updates(llm_model="claude-3-5-haiku-20241022")

    revision = template.revise(new_config)

    assert revision is None
    assert template.version == Version(5)
    assert template.config.llm_model == "claude-3-5-haiku-20241022"


def test_revise_applies_config_even_when_no_bump() -> None:
    template = _template(system_prompt=None, version=1)
    new_config = template.config.merge_updates(
        input_contract=None,
        llm_model="gpt-4o",
    )

    template.revise(new_config)

    # config reference swapped to the merged one regardless of the bump decision.
    assert template.config is new_config
    assert template.config.llm_model == "gpt-4o"
