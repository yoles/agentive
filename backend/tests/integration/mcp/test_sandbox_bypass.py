"""Sandbox bypass tests — Story 2.6 T7 / AC4 (CI bloquants).

Validates that the sandbox actually prevents :
- Fork-bomb (process count caps respected)
- Network outbound (bwrap --unshare-net blocks ; setrlimit fallback cannot)
- Filesystem write to read-only mounts (bwrap --ro-bind blocks ; setrlimit
  fallback cannot)

These tests run with the REAL ``sandboxed_subprocess`` (no mocks) so the
sandbox boundary is exercised end-to-end. They skip cleanly if ``bwrap``
is absent — in that case the setrlimit fallback is active but does not
restrict network/filesystem, so only the fork-bomb test stays runnable.

CI gating : these are marked ``@pytest.mark.security`` AND
``@pytest.mark.integration``. They are part of ``make test`` ; if a future
change weakens the sandbox profile (e.g. accidentally drops
``--unshare-net``), CI fails and the build is blocked.
"""

from __future__ import annotations

import asyncio

import pytest

from agentive_backend.infra.mcp.sandbox import (
    SandboxProfile,
    detect_sandbox_backend,
    sandboxed_subprocess,
)

# Real probe : not only "is bwrap binary present" but also "does it actually
# spawn" (kernel allows unprivileged user namespaces). Docker default seccomp
# blocks this — tests must skip cleanly rather than fail with cryptic errors.
bwrap_available = detect_sandbox_backend() == "bwrap"


@pytest.mark.security
@pytest.mark.integration
async def test_sandbox_blocks_network_outbound() -> None:
    """AC4 — Inside the sandbox, attempting ``socket.connect`` to an
    external IP MUST fail. We try to reach 1.1.1.1:443 — without
    ``--unshare-net`` this would succeed.
    """
    if not bwrap_available:
        pytest.skip("bwrap not installed; setrlimit fallback cannot restrict network")

    payload = (
        "import socket, sys; "
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
        "s.settimeout(2); "
        "exit_code = 0; "
        "try:\n"
        "    s.connect(('1.1.1.1', 443))\n"
        "    print('CONNECT_OK', flush=True)\n"
        "    exit_code = 0\n"
        "except OSError as e:\n"
        "    print(f'CONNECT_BLOCKED:{e.errno}', flush=True)\n"
        "    exit_code = 42\n"
        "sys.exit(exit_code)"
    )
    async with sandboxed_subprocess(
        "python",
        ["-c", payload],
        env={"PATH": "/usr/bin:/bin"},
        profile=SandboxProfile(),
        backend="bwrap",
    ) as proc:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    assert "CONNECT_BLOCKED" in output, (
        f"Network NOT blocked by sandbox — bypass possible.\nstdout={output}"
    )
    assert "CONNECT_OK" not in output


@pytest.mark.security
@pytest.mark.integration
async def test_sandbox_blocks_filesystem_write_to_ro_paths() -> None:
    """AC4 — Inside the sandbox, attempting to ``open("/usr/PWNED", "w")``
    on a read-only bind mount MUST raise ``OSError``. /tmp remains
    writable as designed.
    """
    if not bwrap_available:
        pytest.skip("bwrap not installed; setrlimit fallback cannot restrict fs")

    payload = (
        "import sys; "
        "blocked = False\n"
        "try:\n"
        "    open('/usr/PWNED-by-bypass-test', 'w').close()\n"
        "    print('WRITE_OK', flush=True)\n"
        "except (OSError, PermissionError) as e:\n"
        "    print(f'WRITE_BLOCKED:{type(e).__name__}', flush=True)\n"
        "    blocked = True\n"
        "# /tmp must remain writable.\n"
        "try:\n"
        "    open('/tmp/sandbox-test-marker', 'w').close()\n"
        "    print('TMP_WRITE_OK', flush=True)\n"
        "except OSError:\n"
        "    print('TMP_WRITE_BLOCKED', flush=True)\n"
        "sys.exit(0 if blocked else 1)"
    )
    async with sandboxed_subprocess(
        "python",
        ["-c", payload],
        env={"PATH": "/usr/bin:/bin"},
        profile=SandboxProfile(),
        backend="bwrap",
    ) as proc:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    assert "WRITE_BLOCKED" in output, f"Read-only filesystem NOT enforced.\nstdout={output}"
    assert "WRITE_OK" not in output
    # /tmp tmpfs is writable.
    assert "TMP_WRITE_OK" in output


@pytest.mark.security
@pytest.mark.integration
async def test_sandbox_caps_process_count() -> None:
    """AC4 — A fork-bomb-like loop must be capped. Either bwrap's PID
    namespace + RLIMIT_NPROC (set by ``_setrlimit_preexec`` in fallback,
    or by the bwrap setrlimit defaults Sprint 2) or the user-namespace
    isolation prevents unbounded fork().

    Sprint 1 — we verify the subprocess exits non-zero OR is killed
    quickly (under 5s). A full fork-bomb test that crashes the host
    would defeat the purpose ; we trigger a bounded fork() and verify
    the BehavioralOutcome stays bounded.
    """
    if not bwrap_available:
        pytest.skip("bwrap not functional in this environment (kernel/docker constraint)")

    # P-04 (CR 2026-05-11) — STRICT assertion : the bypass test must FAIL
    # if the sandbox does NOT block the fork-bomb. The previous accept-
    # both-outcomes assertion (FORK_BLOCKED_AT OR FORK_OK_SPAWNED) was a
    # no-op that passed even when the cap was bypassed. AC4 requires
    # failure on bypass.
    #
    # Sandbox cap = 8 max processes. We attempt 64 forks. The script
    # prints either ``FORK_BLOCKED_AT:N:<errno>`` (cap hit at N < 64) or
    # ``FORK_OK_SPAWNED:64`` (no cap, security regression).
    payload = (
        "import os, sys\n"
        "spawned = 0\n"
        "for i in range(64):\n"
        "    try:\n"
        "        pid = os.fork()\n"
        "        if pid == 0:\n"
        "            import time; time.sleep(0.1)\n"
        "            sys.exit(0)\n"
        "        spawned += 1\n"
        "    except OSError as e:\n"
        "        print(f'FORK_BLOCKED_AT:{spawned}:{e.errno}', flush=True)\n"
        "        sys.exit(0)\n"
        "print(f'FORK_OK_SPAWNED:{spawned}', flush=True)\n"
        "sys.exit(0)\n"
    )
    async with sandboxed_subprocess(
        "python",
        ["-c", payload],
        env={"PATH": "/usr/bin:/bin"},
        profile=SandboxProfile(max_processes=8),
        backend="bwrap",
    ) as proc:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    output = stdout.decode("utf-8", errors="replace") if stdout else ""
    assert "FORK_BLOCKED_AT" in output, (
        f"Fork-bomb cap NOT enforced — security regression in sandbox.\nstdout={output}"
    )
    assert "FORK_OK_SPAWNED" not in output, (
        f"Fork-bomb fully ran past the cap — security regression.\nstdout={output}"
    )


@pytest.mark.security
@pytest.mark.integration
async def test_sandbox_happy_path_python_inside_bwrap() -> None:
    """Smoke — verify that a plain ``python -c "print('hi')"`` runs
    successfully inside the sandbox. Catches regressions where bwrap
    flags accidentally break the basic execution path."""
    if not bwrap_available:
        pytest.skip("bwrap not installed")

    async with sandboxed_subprocess(
        "python",
        ["-c", "print('hi from sandbox')"],
        env={"PATH": "/usr/bin:/bin"},
        profile=SandboxProfile(),
        backend="bwrap",
    ) as proc:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    assert b"hi from sandbox" in stdout
    assert proc.returncode == 0
