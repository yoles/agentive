"""Universal archetypes registry — Story 2.1.

Loads the 8 universal archetypes from
``features/agent_registry/templates/archetype-schema.yaml`` into a
read-only in-process registry attached to ``app.state.archetype_registry``
during the FastAPI lifespan. The registry is *immutable* once loaded and
keyed by the slug ``id`` (ASCII, no accents — see story §"Pièges connus" #6).

The 8 IDs are : ``orchestrateur``, ``chercheur``, ``analyste``, ``producteur``,
``stratege``, ``controleur``, ``veilleur``, ``communicateur``.

Story 2.1 anti-scope: ``prompt_base``, ``input_contract``, and ``output_contract``
fields are *skeletons* — Story 2.2 will extend the template config with full
``system_prompt``, ``llm_model``, ``llm_params``, ``provider_chain``, and
``error_policy``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Constants
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

EXPECTED_ARCHETYPE_IDS: Final[frozenset[str]] = frozenset(
    {
        "orchestrateur",
        "chercheur",
        "analyste",
        "producteur",
        "stratege",
        "controleur",
        "veilleur",
        "communicateur",
    }
)

DEFAULT_SCHEMA_PATH: Final[Path] = Path(__file__).parent / "templates" / "archetype-schema.yaml"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Pydantic v2 — strict (extra="forbid") to reject malformed YAML at load
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ContractSkeleton(BaseModel):
    """Minimal skeleton ``{core, extras}`` for input/output contracts.

    Story 2.1 only requires the shape — Story 2.2 will extend ``core`` with
    typed fields and runtime Pydantic validation.
    """

    model_config = ConfigDict(extra="forbid")

    core: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)


class ArchetypeDefinition(BaseModel):
    """One of the 8 universal archetypes."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)
    icon_name: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=500)
    default_role: str = Field(min_length=1, max_length=50)
    prompt_base: str = Field(min_length=1)
    input_contract: ContractSkeleton
    output_contract: ContractSkeleton

    def to_template_config(self) -> dict[str, Any]:
        """Return the default ``agent_templates.config`` JSON for this archetype.

        Story 2.1 keeps ``config`` minimal — Story 2.2 will add
        ``system_prompt``, ``llm_model``, ``llm_params``, ``provider_chain``,
        and ``error_policy`` here.
        """
        return {
            "prompt_base": self.prompt_base,
            "input_contract": self.input_contract.model_dump(),
            "output_contract": self.output_contract.model_dump(),
            "role": self.default_role,
        }


class _RegistryFile(BaseModel):
    """Top-level YAML wrapper — only allows an ``archetypes`` list."""

    model_config = ConfigDict(extra="forbid")

    archetypes: list[ArchetypeDefinition]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Loader
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def load_registry(
    schema_path: Path | None = None,
) -> Mapping[str, ArchetypeDefinition]:
    """Load and validate the archetype registry from YAML.

    Args:
        schema_path: Override the default path (used by tests). Defaults to
            ``features/agent_registry/templates/archetype-schema.yaml``.

    Returns:
        An immutable mapping ``{archetype_id: ArchetypeDefinition}``.

    Raises:
        RuntimeError: If the YAML file is missing, malformed, or the resulting
            registry does not contain exactly the 8 expected archetypes.
    """
    path = schema_path or DEFAULT_SCHEMA_PATH

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"archetype registry init failed: cannot read {path}: {exc}") from exc

    try:
        # Story 2.1 §"Pièges connus" #7 — yaml.safe_load only.
        raw_data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"archetype registry init failed: YAML parse error in {path}: {exc}"
        ) from exc

    if not isinstance(raw_data, dict):
        raise RuntimeError(
            f"archetype registry init failed: expected mapping at top level of {path}, "
            f"got {type(raw_data).__name__}"
        )

    try:
        parsed = _RegistryFile.model_validate(raw_data)
    except PydanticValidationError as exc:
        # Story 2.1 P-XX (post-review) — narrow except to PydanticValidationError
        # so unrelated programming errors (NameError, ImportError) bubble
        # up cleanly with their own traceback instead of being masked as
        # "schema validation error".
        raise RuntimeError(
            f"archetype registry init failed: schema validation error in {path}: {exc}"
        ) from exc

    seen_ids: set[str] = set()
    registry: dict[str, ArchetypeDefinition] = {}
    for archetype in parsed.archetypes:
        if archetype.id in seen_ids:
            raise RuntimeError(
                f"archetype registry init failed: duplicate id '{archetype.id}' in {path}"
            )
        seen_ids.add(archetype.id)
        registry[archetype.id] = archetype

    if seen_ids != EXPECTED_ARCHETYPE_IDS:
        missing = sorted(EXPECTED_ARCHETYPE_IDS - seen_ids)
        unexpected = sorted(seen_ids - EXPECTED_ARCHETYPE_IDS)
        raise RuntimeError(
            "archetype registry init failed: archetype id set mismatch "
            f"(missing: {missing}, unexpected: {unexpected})"
        )

    # Story 2.1 P-12 — wrap in `MappingProxyType` so callers literally cannot
    # mutate the registry once loaded (`registry["x"] = ...` raises
    # `TypeError: 'mappingproxy' object does not support item assignment`).
    # Defense-in-depth against accidental mutation in long-lived `app.state`.
    return MappingProxyType(registry)


__all__ = [
    "DEFAULT_SCHEMA_PATH",
    "EXPECTED_ARCHETYPE_IDS",
    "ArchetypeDefinition",
    "ContractSkeleton",
    "load_registry",
]
