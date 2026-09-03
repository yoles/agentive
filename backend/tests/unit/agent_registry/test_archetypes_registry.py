"""Unit tests — `features.agent_registry.archetypes` (Story 2.1 T1.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentive_backend.features.agent_registry.archetypes import (
    DEFAULT_SCHEMA_PATH,
    EXPECTED_ARCHETYPE_IDS,
    load_registry,
)


def test_load_default_registry_has_exactly_8_archetypes() -> None:
    """AC1 — registry contains exactly the 8 expected archetypes."""
    registry = load_registry()
    assert len(registry) == 8
    assert set(registry.keys()) == EXPECTED_ARCHETYPE_IDS


def test_load_default_registry_each_archetype_has_required_fields() -> None:
    """AC1 — every archetype exposes id, display_name, icon_name, description,
    prompt_base, input_contract, output_contract, default_role."""
    registry = load_registry()
    for archetype_id, archetype in registry.items():
        assert archetype.id == archetype_id
        assert archetype.display_name
        assert archetype.icon_name
        assert archetype.description
        assert archetype.default_role
        assert archetype.prompt_base
        assert archetype.input_contract is not None
        assert archetype.output_contract is not None


def test_load_default_registry_to_template_config_skeleton() -> None:
    """AC3 — to_template_config exposes prompt_base, input_contract,
    output_contract, role (the 4 keys Story 2.1 stores in agent_templates.config)."""
    registry = load_registry()
    producteur = registry["producteur"]
    config = producteur.to_template_config()
    assert set(config.keys()) == {"prompt_base", "input_contract", "output_contract", "role"}
    assert config["role"] == producteur.default_role
    assert config["prompt_base"] == producteur.prompt_base


def test_controleur_output_contract_documents_review_comment_shape() -> None:
    """Story 2.8 AC2 — the `controleur` archetype documents the `ReviewComment`
    shape in `output_contract.core.comments`.

    Without this assertion the enriched description is protected by nothing: the
    other registry tests only check that `output_contract` is not None, so a
    silent revert of the YAML value would keep the whole suite green.
    """
    controleur = load_registry()["controleur"]
    comments = controleur.output_contract.core["comments"]

    assert isinstance(comments, str)
    assert "ReviewComment" in comments
    for field in ("location", "severity", "message", "suggested_fix"):
        assert field in comments
    for severity in ("blocking", "suggestion", "question"):
        assert severity in comments


def test_load_registry_missing_file_raises_runtime_error(tmp_path: Path) -> None:
    """AC1 — missing YAML file → fail-fast RuntimeError with path context."""
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(RuntimeError, match=r"archetype registry init failed.*cannot read"):
        load_registry(schema_path=missing)


def test_load_registry_malformed_yaml_raises_runtime_error(tmp_path: Path) -> None:
    """AC1 — malformed YAML → fail-fast RuntimeError mentioning parse error."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("archetypes: [not closed", encoding="utf-8")
    with pytest.raises(RuntimeError, match="YAML parse error"):
        load_registry(schema_path=bad)


def test_load_registry_wrong_top_level_raises_runtime_error(tmp_path: Path) -> None:
    """AC1 — non-mapping top-level → fail-fast."""
    bad = tmp_path / "list.yaml"
    bad.write_text("- not a dict\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="expected mapping at top level"):
        load_registry(schema_path=bad)


def test_load_registry_missing_archetype_raises_runtime_error(tmp_path: Path) -> None:
    """AC1 — incomplete set (< 8) → fail-fast with `missing` list."""
    bad = tmp_path / "partial.yaml"
    bad.write_text(
        """archetypes:
  - id: producteur
    display_name: Producteur
    icon_name: wrench
    description: dummy
    default_role: producer
    prompt_base: Tu es un producteur.
    input_contract: {core: {}, extras: {}}
    output_contract: {core: {}, extras: {}}
""",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match=r"archetype id set mismatch.*missing"):
        load_registry(schema_path=bad)


def test_load_registry_extra_archetype_raises_runtime_error(tmp_path: Path) -> None:
    """AC1 — unknown id (not in EXPECTED_ARCHETYPE_IDS) → fail-fast with `unexpected`."""
    bad_yaml_lines = ["archetypes:"]
    for archetype_id in [*EXPECTED_ARCHETYPE_IDS, "extraterrestre"]:
        bad_yaml_lines.append(f"  - id: {archetype_id}")
        bad_yaml_lines.append(f"    display_name: {archetype_id.title()}")
        bad_yaml_lines.append("    icon_name: bot")
        bad_yaml_lines.append("    description: test")
        bad_yaml_lines.append("    default_role: test")
        bad_yaml_lines.append("    prompt_base: test")
        bad_yaml_lines.append("    input_contract: {core: {}, extras: {}}")
        bad_yaml_lines.append("    output_contract: {core: {}, extras: {}}")
    bad = tmp_path / "extra.yaml"
    bad.write_text("\n".join(bad_yaml_lines) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"archetype id set mismatch.*unexpected"):
        load_registry(schema_path=bad)


def test_default_schema_path_exists() -> None:
    """The packaged default YAML file exists."""
    assert DEFAULT_SCHEMA_PATH.is_file()
