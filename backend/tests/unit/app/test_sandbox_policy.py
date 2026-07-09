"""Unit tests for ``_enforce_mcp_sandbox_policy`` (audit A-03 / 5.3).

The setrlimit fallback cannot isolate network or filesystem. Enabling MCP
registration on that backend must refuse boot in production and warn
loudly in dev — never degrade silently.
"""

from __future__ import annotations

import pytest

from agentive_backend.app.lifespan import _enforce_mcp_sandbox_policy
from agentive_backend.shared.config import settings


def test_refuses_boot_in_production_with_flag_on_and_setrlimit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_allow_registration", True)
    monkeypatch.setattr(settings, "environment", "production")

    with pytest.raises(RuntimeError, match="setrlimit"):
        _enforce_mcp_sandbox_policy("setrlimit")


def test_warns_but_boots_in_dev_with_flag_on_and_setrlimit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_allow_registration", True)
    monkeypatch.setattr(settings, "environment", "development")

    # Must not raise — dev machines commonly run Docker profiles where
    # bwrap is inoperative; the guard logs a WARNING instead.
    _enforce_mcp_sandbox_policy("setrlimit")


@pytest.mark.parametrize("environment", ["production", "development"])
def test_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    monkeypatch.setattr(settings, "mcp_allow_registration", False)
    monkeypatch.setattr(settings, "environment", environment)

    _enforce_mcp_sandbox_policy("setrlimit")


@pytest.mark.parametrize("environment", ["production", "development"])
def test_noop_when_backend_is_bwrap(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    monkeypatch.setattr(settings, "mcp_allow_registration", True)
    monkeypatch.setattr(settings, "environment", environment)

    _enforce_mcp_sandbox_policy("bwrap")
