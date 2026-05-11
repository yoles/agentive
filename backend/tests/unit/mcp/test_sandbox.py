"""Unit tests for ``infra/mcp/sandbox.py`` — Story 2.6 T8.

Scope :
- ``SandboxProfile`` defaults are deny-by-default + minimal whitelist.
- ``_build_bwrap_argv`` produces the expected argv shape.
- ``detect_sandbox_backend`` returns "bwrap" when binary present, else
  "setrlimit" with a warning logged.
- Exception classes carry the expected attributes for diagnostics.
"""

from __future__ import annotations

import pytest

from agentive_backend.infra.mcp.sandbox import (
    MCPExecutionError,
    MCPExecutionTimeoutError,
    MCPToolError,
    SandboxProfile,
    _build_bwrap_argv,
    _build_setrlimit_bootstrap,
    detect_sandbox_backend,
)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SandboxProfile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_sandbox_profile_default_is_deny_by_default() -> None:
    """Defaults must isolate network + drop caps + use ephemeral /tmp."""
    profile = SandboxProfile()
    assert profile.unshare_net is True
    assert "/tmp" in profile.tmpfs_paths
    assert "/usr" in profile.ro_binds
    # PATH is the ONLY env var passed through by default.
    assert profile.env_passthrough == ("PATH",)
    # Defense-in-depth rlimits are also set.
    assert profile.max_processes == 16
    assert profile.cpu_seconds == 30
    assert profile.memory_mb == 512


def test_sandbox_profile_is_frozen_dataclass() -> None:
    """Cannot mutate a profile after construction — prevents accidental
    runtime escalation."""
    profile = SandboxProfile()
    with pytest.raises(Exception):
        profile.unshare_net = False  # type: ignore[misc]  # FrozenInstanceError


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _build_bwrap_argv
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_build_bwrap_argv_includes_critical_flags() -> None:
    """The bwrap argv MUST include --unshare-net, --cap-drop ALL,
    --die-with-parent — these are the safety-critical flags."""
    argv = _build_bwrap_argv("python", ["-m", "mock"], profile=SandboxProfile())
    assert argv[0] == "bwrap"
    assert "--unshare-net" in argv
    # --cap-drop is followed by ALL.
    cap_idx = argv.index("--cap-drop")
    assert argv[cap_idx + 1] == "ALL"
    assert "--die-with-parent" in argv
    # The command and args land at the tail.
    assert argv[-3:] == ["python", "-m", "mock"]


def test_build_bwrap_argv_respects_unshare_net_false() -> None:
    """A custom profile with ``unshare_net=False`` does NOT add the flag
    — relevant for hypothetical "trusted local tool" profiles Sprint 2+."""
    profile = SandboxProfile(unshare_net=False)
    argv = _build_bwrap_argv("python", [], profile=profile)
    assert "--unshare-net" not in argv


def test_build_bwrap_argv_binds_tmpfs_paths() -> None:
    """Each path in ``tmpfs_paths`` produces a ``--tmpfs <path>`` pair."""
    profile = SandboxProfile(tmpfs_paths=("/tmp", "/var/tmp"))
    argv = _build_bwrap_argv("python", [], profile=profile)
    pairs = [(argv[i], argv[i + 1]) for i, tok in enumerate(argv[:-1]) if tok == "--tmpfs"]
    paths = {p for _, p in pairs}
    assert "/tmp" in paths
    assert "/var/tmp" in paths


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# _build_setrlimit_bootstrap (fallback)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_setrlimit_bootstrap_runs_python_with_resource_module() -> None:
    """The fallback bootstrap is ``python -c "import resource; ...;
    os.execvp(...)"`` — verifies the shape, not the runtime semantics."""
    bootstrap = _build_setrlimit_bootstrap("python", ["-m", "mock"], profile=SandboxProfile())
    assert bootstrap[0].endswith("python") or bootstrap[0].endswith("python3")
    assert bootstrap[1] == "-c"
    assert "resource.setrlimit" in bootstrap[2]
    assert "os.execvp" in bootstrap[2]
    # Real command + args trail after the -c payload.
    assert bootstrap[-3:] == ["python", "-m", "mock"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# detect_sandbox_backend
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_detect_sandbox_backend_returns_bwrap_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When bwrap binary exists AND the spawn probe succeeds, return ``bwrap``."""
    monkeypatch.setattr(
        "agentive_backend.infra.mcp.sandbox.shutil.which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr(
        "agentive_backend.infra.mcp.sandbox._probe_bwrap_actually_works",
        lambda: True,
    )
    assert detect_sandbox_backend() == "bwrap"


def test_detect_sandbox_backend_falls_back_to_setrlimit_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``shutil.which('bwrap')`` returns None, fall back to setrlimit."""
    monkeypatch.setattr(
        "agentive_backend.infra.mcp.sandbox.shutil.which",
        lambda name: None,
    )
    assert detect_sandbox_backend() == "setrlimit"


def test_detect_sandbox_backend_falls_back_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When bwrap binary exists but kernel rejects unprivileged user namespaces
    (Docker default seccomp), the spawn probe returns False → setrlimit."""
    monkeypatch.setattr(
        "agentive_backend.infra.mcp.sandbox.shutil.which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    monkeypatch.setattr(
        "agentive_backend.infra.mcp.sandbox._probe_bwrap_actually_works",
        lambda: False,
    )
    assert detect_sandbox_backend() == "setrlimit"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Exception classes
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def test_mcp_execution_timeout_error_carries_timeout() -> None:
    err = MCPExecutionTimeoutError(timeout=2.5)
    assert err.timeout == 2.5
    assert "2.5" in str(err)


def test_mcp_execution_error_truncates_stderr_tail() -> None:
    """``stderr_tail`` is bounded to 500 chars (P-03 secret-safety)."""
    long_stderr = "x" * 1200
    err = MCPExecutionError(returncode=139, stderr_tail=long_stderr)
    assert err.returncode == 139
    assert len(err.stderr_tail) == 500


def test_mcp_tool_error_carries_tool_name_and_detail() -> None:
    err = MCPToolError(tool_name="echo", detail="invalid argument 'text'")
    assert err.tool_name == "echo"
    assert err.detail == "invalid argument 'text'"
    assert "echo" in str(err)
