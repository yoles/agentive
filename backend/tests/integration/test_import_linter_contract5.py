"""AC11 — Contract 5 forbids LangChain LLM SDKs from features and api.

Mirror of ``test_import_linter_contract3.py`` (Story 1.5). Static parse
check: validates the ``.import-linter`` stanza shape rather than running
``lint-imports`` in-process. The CI workflow already runs
``lint-imports`` end-to-end on every PR, so this test stays fast.
"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

CONTRACT_SECTION = "importlinter:contract:no-direct-llm-sdk-from-features"


def _find_import_linter_config() -> Path:
    """Walk up from this test file to the repo root and return ``.import-linter``."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / ".import-linter"
        if candidate.exists():
            return candidate
        # Inside the container the config is mounted at the FS root.
        root_mount = Path("/.import-linter")
        if root_mount.exists():
            return root_mount
    raise AssertionError(".import-linter not found above test file or at /.import-linter")


def test_contract_section_present() -> None:
    parser = ConfigParser()
    parser.read(_find_import_linter_config())
    assert CONTRACT_SECTION in parser.sections(), (
        f"Expected {CONTRACT_SECTION} in .import-linter — Story 1.6 Contract 5"
    )


def test_contract_is_forbidden_type() -> None:
    parser = ConfigParser()
    parser.read(_find_import_linter_config())
    assert parser.get(CONTRACT_SECTION, "type") == "forbidden"


def test_source_modules_cover_features_and_api() -> None:
    parser = ConfigParser()
    parser.read(_find_import_linter_config())
    raw = parser.get(CONTRACT_SECTION, "source_modules")
    sources = {line.strip() for line in raw.splitlines() if line.strip()}
    assert "agentive_backend.features" in sources
    assert "agentive_backend.api" in sources


def test_forbidden_modules_cover_langchain_and_native_sdks() -> None:
    parser = ConfigParser()
    parser.read(_find_import_linter_config())
    raw = parser.get(CONTRACT_SECTION, "forbidden_modules")
    forbidden = {line.strip() for line in raw.splitlines() if line.strip()}
    for mod in ("langchain_anthropic", "langchain_openai", "langchain_core", "anthropic", "openai"):
        assert mod in forbidden, f"Contract 5 must forbid {mod}"


def test_contract_block_is_documented_in_file_header_comments() -> None:
    """The block must reference llm-abstraction.md so future devs can find context."""
    raw = _find_import_linter_config().read_text()
    contract_block_idx = raw.index(f"[{CONTRACT_SECTION}]")
    # Walk backwards to the previous blank line — that is the comment block.
    preceding = raw[:contract_block_idx]
    last_blank = preceding.rfind("\n\n")
    comment_block = preceding[last_blank:contract_block_idx]
    assert "llm-abstraction.md" in comment_block
