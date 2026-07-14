"""Agent Registry aggregate root — Palier 2.

:meth:`AgentTemplate.revise` is the ONLY place the "system_prompt change ⇒
bump version + create a prompt revision" rule lives. It is pure Python,
testable in microseconds without a DB, a mocked session, or an event bus —
the concrete win the audit asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agentive_backend.features.agent_registry.domain.value_objects import (
    AgentConfig,
    Archetype,
    Version,
)


@dataclass(frozen=True, slots=True)
class PromptRevision:
    """A new ``prompts`` row to persist alongside a template version bump."""

    template_id: UUID
    version: Version
    content: str


@dataclass(slots=True)
class AgentTemplate:
    """Aggregate root — id, name, archetype, version, config.

    Mutable by design: :meth:`revise` mutates ``version``/``config`` in place,
    following the aggregate-root convention that the transaction boundary owns
    the object. It never touches SQLAlchemy or Pydantic.
    """

    id: UUID
    name: str
    archetype: Archetype
    version: Version
    config: AgentConfig

    def revise(self, new_config: AgentConfig) -> PromptRevision | None:
        """Apply ``new_config``; bump ``version`` and return a
        :class:`PromptRevision` **iff** ``new_config.system_prompt`` is
        non-``None`` and differs from the current ``system_prompt``.

        ``new_config`` MUST already be the fully-merged config (the caller
        applies :meth:`AgentConfig.merge_updates` beforehand). This method only
        decides the versioning consequence — it does not merge fields itself.

        Returns
        -------
        PromptRevision | None
            The new prompt row to persist, or ``None`` when no bump was
            triggered (the P-01 "identical system_prompt" guard).
        """
        system_prompt_changed = (
            new_config.system_prompt is not None
            and new_config.system_prompt != self.config.system_prompt
        )
        self.config = new_config
        if not system_prompt_changed:
            return None

        self.version = self.version.next()
        # `system_prompt_changed` guarantees `new_config.system_prompt` is not None.
        return PromptRevision(
            template_id=self.id,
            version=self.version,
            content=new_config.system_prompt,  # type: ignore[arg-type]  # guarded above
        )


__all__ = ["AgentTemplate", "PromptRevision"]
