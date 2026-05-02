"""AC5 — verify ``import-linter`` Contract 3 still forbids direct DB access from features.

Two complementary checks:

* :func:`test_contract3_forbids_sqlalchemy_psycopg_asyncpg_from_features`
  — fast static check (Approach A in the story Dev Notes): parses
  ``.import-linter`` and asserts the Contract 3 stanza still names the
  expected source/forbidden modules. Catches accidental section renames
  or removed entries.
* :func:`test_contract3_actually_blocks_a_violation_via_lint_imports` —
  Approach B: writes a fixture file under ``features/`` that imports
  ``sqlalchemy.ext.asyncio.AsyncSession``, runs ``lint-imports`` in a
  subprocess, and asserts the contract reports BROKEN. Catches a typo
  in the contract syntax that the static check would silently miss.
  Skipped if the test environment can't write under ``features/`` or
  if ``lint-imports``/the config file aren't reachable.
"""

from __future__ import annotations

import shutil
import subprocess
from configparser import ConfigParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


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


def _features_package_dir() -> Path | None:
    """Return the on-disk path of ``agentive_backend.features`` if writable."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "src" / "agentive_backend" / "features"
        if candidate.is_dir():
            return candidate
    return None


def test_contract3_forbids_sqlalchemy_psycopg_asyncpg_from_features() -> None:
    config_path = _find_import_linter_config()
    parser = ConfigParser()
    parser.read(config_path)

    section = "importlinter:contract:no-direct-db-access-from-features"
    assert section in parser.sections(), f"Contract 3 section '{section}' missing — was it renamed?"

    contract = parser[section]
    assert contract["type"] == "forbidden"

    source_modules = {
        line.strip() for line in contract["source_modules"].splitlines() if line.strip()
    }
    assert "agentive_backend.features" in source_modules

    forbidden = {
        line.strip() for line in contract["forbidden_modules"].splitlines() if line.strip()
    }
    expected = {"sqlalchemy", "asyncpg", "psycopg", "psycopg2"}
    missing = expected - forbidden
    assert not missing, f"Contract 3 must forbid {expected}, missing: {missing}"


def test_contract3_actually_blocks_a_violation_via_lint_imports(tmp_path: Path) -> None:
    """Plant a violation under ``features/``, run ``lint-imports``, expect BROKEN."""
    config_path = _find_import_linter_config()
    features_dir = _features_package_dir()
    if features_dir is None:
        pytest.skip("features/ package not writable in this environment")
    if shutil.which("lint-imports") is None:
        pytest.skip("lint-imports binary not on PATH")

    violation_pkg = features_dir / "m_smoke_violation"
    violation_pkg.mkdir(parents=True, exist_ok=False)
    try:
        (violation_pkg / "__init__.py").write_text("")
        (violation_pkg / "violation.py").write_text(
            "# Test fixture: this file deliberately violates Contract 3.\n"
            "from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401\n"
        )

        proc = subprocess.run(
            ["lint-imports", "--config", str(config_path)],
            capture_output=True,
            text=True,
            check=False,
        )

        assert proc.returncode != 0, (
            "lint-imports should fail when a feature module imports sqlalchemy. "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        # Confirm Contract 3 specifically is what broke (not an unrelated layer).
        assert "no-direct-db-access-from-features" in proc.stdout.lower() or (
            "Feature modules must not import sqlalchemy" in proc.stdout
        ), f"Expected Contract 3 to be broken; got: {proc.stdout}"
    finally:
        shutil.rmtree(violation_pkg, ignore_errors=True)
