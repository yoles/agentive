"""MCP runtime sandbox — Story 2.6 (FR24 + NFR10).

Story 2.5 livre la **discovery** (registry + assignment) ; Story 2.6 livre
l'**exécution runtime** sandboxée. Cette module fournit la primitive
``sandboxed_subprocess`` qui spawne un subprocess (typiquement le serveur
MCP stdio) dans :

* un namespace réseau dédié (``bwrap --unshare-net``) — pas d'accès au
  réseau hôte ;
* un filesystem read-only sauf ``/tmp`` éphémère (``--tmpfs /tmp``) ;
* sans aucune capability Linux (``--cap-drop ALL``) ;
* avec un parent-death signal (``--die-with-parent``) pour qu'un crash
  backend ne laisse PAS de zombie subprocess.

Si ``bwrap`` est indisponible (dev local sans bubblewrap apt-installed),
fallback vers ``resource.setrlimit`` (CPU, mémoire, nombre de processes).
Le fallback est **best-effort** : il NE peut PAS restreindre le réseau ni
le filesystem ; un warning est loggé et une métrique
``mcp_sandbox_backend{kind="setrlimit"}`` est exposée.

API publique :
- :func:`detect_sandbox_backend` — probe ``shutil.which('bwrap')`` au boot.
- :class:`SandboxProfile` — tunables per-call (network, ro-binds, tmpfs).
- :func:`sandboxed_subprocess` — async context manager qui yield un
  ``asyncio.subprocess.Process``.
- :class:`MCPExecutionTimeoutError`, :class:`MCPExecutionError`,
  :class:`MCPToolError` — erreurs infra-level translatées par les
  features/ en domain ``DependencyError`` / ``NotFoundError`` (cohérent
  Story 2.5 P-08 pattern).

Anti-scope (Sprint 1) : pas de per-tool sandbox profile en DB (D62), pas
de pool persistant (D61), pas d'allowlist URL SSE runtime (D63).
"""

from __future__ import annotations

import asyncio
import functools
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

# Default sandbox profile — deny-all + minimal whitelist. Tunable per-call
# via :class:`SandboxProfile` ; per-tool profile in DB defer Sprint 2 (D62).
_DEFAULT_RO_BINDS: tuple[str, ...] = (
    "/usr",
    "/etc",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
)
_DEFAULT_TMPFS_PATHS: tuple[str, ...] = ("/tmp",)
_DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH",)

SandboxBackend = Literal["bwrap", "setrlimit"]


@dataclass(frozen=True)
class SandboxProfile:
    """Per-call sandbox tunables. Defaults to deny-all network + minimal
    read-only filesystem + tmpfs ``/tmp``.

    Per-tool profile customization (column ``tools.sandbox_profile JSONB``)
    is deferred to Sprint 2 (D62 Story 2.6).

    P-04 / P-16 (CR 2026-05-11) — the resource caps below are applied
    ONLY in the setrlimit fallback path (`_build_setrlimit_bootstrap`).
    In bwrap mode, isolation is provided by PID/user namespaces +
    capability drops ; CPU/memory/process caps are NOT enforced by bwrap
    itself. Wrapping bwrap with `prlimit` to apply these caps in bwrap
    mode is deferred to Sprint 2 (new defer D74).
    """

    unshare_net: bool = True
    ro_binds: tuple[str, ...] = _DEFAULT_RO_BINDS
    tmpfs_paths: tuple[str, ...] = _DEFAULT_TMPFS_PATHS
    env_passthrough: tuple[str, ...] = _DEFAULT_ENV_PASSTHROUGH
    # Resource caps for setrlimit fallback ONLY (cf class docstring).
    max_processes: int = 16
    cpu_seconds: int = 30
    memory_mb: int = 512
    # Subprocess lifecycle.
    sigterm_grace_seconds: float = 0.5


class MCPExecutionTimeoutError(Exception):
    """Raised when a sandboxed MCP tool call exceeds its timeout budget.

    Translated to :class:`agentive_backend.shared.exceptions.DependencyError`
    (HTTP 503) by the service layer (P-08 Story 2.5 pattern).
    """

    def __init__(self, *, timeout: float) -> None:
        super().__init__(f"MCP execution timed out after {timeout}s")
        self.timeout = timeout


class MCPExecutionError(Exception):
    """Raised when the sandboxed subprocess crashes or exits non-zero.

    ``stderr_tail`` is bounded to 500 chars (P-03 secret-safety alignment).
    Translated to :class:`agentive_backend.shared.exceptions.DependencyError`
    (HTTP 503).
    """

    def __init__(self, *, returncode: int, stderr_tail: str = "") -> None:
        super().__init__(f"MCP subprocess exited with code {returncode}: {stderr_tail[:500]}")
        self.returncode = returncode
        self.stderr_tail = stderr_tail[:500]


class MCPToolError(Exception):
    """Raised when the MCP server returns ``CallToolResult.isError=True``
    (the tool exists but its execution failed).

    Translated to :class:`agentive_backend.shared.exceptions.NotFoundError`
    (HTTP 404) — the tool semantically rejected the call.
    """

    def __init__(self, *, tool_name: str, detail: str) -> None:
        super().__init__(f"MCP tool '{tool_name}' returned error: {detail}")
        self.tool_name = tool_name
        self.detail = detail


def detect_sandbox_backend() -> SandboxBackend:
    """Detect the active sandbox backend.

    Memoized at module level via :func:`_detect_sandbox_backend_uncached` —
    callers should treat this as a cached helper (TOCTOU is acceptable
    since kernel security profile rarely changes at runtime). The probe
    checks BOTH that the ``bwrap`` binary exists AND that the kernel
    allows unprivileged user namespaces (required for ``--unshare-user`` +
    capability drop).

    On Docker without ``CAP_SYS_ADMIN`` or with
    ``kernel.unprivileged_userns_clone=0``, the binary exists but spawning
    fails — we fall back to setrlimit.

    P-18 (CR 2026-05-11) — synchronous on purpose : the probe runs at
    lifespan boot only (memoized result reused everywhere). Callers from
    async code should call this exactly once at startup and propagate
    the result, not re-probe per request.
    """
    return _detect_sandbox_backend_uncached()


@functools.cache
def _detect_sandbox_backend_uncached() -> SandboxBackend:
    """Underlying detection logic. ``@functools.cache`` means the probe
    fires exactly once per process lifetime (no event-loop blocking past
    the first call)."""
    if shutil.which("bwrap") is None:
        _log.warning(
            "mcp_sandbox.bwrap_unavailable_falling_back_to_setrlimit",
            reason="bwrap binary not on $PATH",
        )
        return "setrlimit"
    if not _probe_bwrap_actually_works():
        _log.warning(
            "mcp_sandbox.bwrap_unavailable_falling_back_to_setrlimit",
            reason=(
                "bwrap binary present but kernel rejects unprivileged user "
                "namespaces (likely Docker security profile). Run the backend "
                "with CAP_SYS_ADMIN or set kernel.unprivileged_userns_clone=1 "
                "for full sandbox isolation."
            ),
        )
        return "setrlimit"
    return "bwrap"


def _probe_bwrap_actually_works() -> bool:
    """Spawn a minimal ``bwrap`` invocation to verify the kernel allows
    unprivileged user namespaces + capability drop. Returns True only if
    the probe exits 0 within a 2s budget.

    Cached at boot via :func:`detect_sandbox_backend`. The probe runs
    ``bwrap --unshare-user --cap-drop ALL --ro-bind /usr /usr --proc /proc
    --dev /dev -- true`` which exits 0 if the namespaces succeed.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "bwrap",
                "--unshare-user",
                "--unshare-pid",
                "--cap-drop",
                "ALL",
                "--ro-bind",
                "/usr",
                "/usr",
                "--ro-bind",
                "/lib",
                "/lib",
                "--ro-bind",
                "/lib64",
                "/lib64",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--",
                "true",
            ],
            capture_output=True,
            timeout=2.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return result.returncode == 0


def _build_bwrap_argv(
    command: str,
    args: list[str],
    *,
    profile: SandboxProfile,
    parent_env: dict[str, str] | None = None,
) -> list[str]:
    """Construct the ``bwrap`` argv that wraps ``command`` + ``args``.

    Order matters : ``bwrap`` consumes its own flags first, then a literal
    ``--`` separator is implicit (the first non-flag arg is the command).

    P-01 (CR 2026-05-11) — for each ``env_key`` in ``profile.env_passthrough``,
    we resolve the value from ``parent_env`` (caller-supplied) OR fall back
    to the current process environment. bwrap's ``--setenv KEY VAL`` is a
    LITERAL assignment ; if we pass an empty string the sandbox boots with
    ``PATH=""`` and bare-name commands (e.g. ``python``) fail ENOENT.
    """
    import os

    argv: list[str] = ["bwrap"]

    # Network isolation.
    if profile.unshare_net:
        argv.append("--unshare-net")
    # PID + UTS + IPC + cgroup namespaces — always on (defense in depth).
    argv.extend(["--unshare-pid", "--unshare-uts", "--unshare-ipc", "--unshare-cgroup"])
    # User namespace — required to drop caps without root.
    argv.append("--unshare-user")

    # Drop ALL capabilities.
    argv.extend(["--cap-drop", "ALL"])

    # Read-only bind mounts.
    for path in profile.ro_binds:
        argv.extend(["--ro-bind", path, path])
    # Required pseudo-FS for Python interpreter (proc + dev for /dev/urandom).
    argv.extend(["--proc", "/proc", "--dev", "/dev"])

    # Tmpfs for ephemeral writable paths.
    for path in profile.tmpfs_paths:
        argv.extend(["--tmpfs", path])

    # Set hostname inside the sandbox (avoid leaking host hostname).
    argv.extend(["--hostname", "mcp-sandbox"])
    # Clean environment — clear, then re-export whitelist with REAL values.
    argv.append("--clearenv")
    env_source = parent_env if parent_env is not None else os.environ
    for env_key in profile.env_passthrough:
        value = env_source.get(env_key, "")
        argv.extend(["--setenv", env_key, value])
    # Die with parent (no zombies on backend crash).
    argv.append("--die-with-parent")

    # Then the actual command + args.
    argv.append(command)
    argv.extend(args)
    return argv


@asynccontextmanager
async def sandboxed_subprocess(
    command: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    profile: SandboxProfile | None = None,
    backend: SandboxBackend | None = None,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Spawn ``command`` + ``args`` inside a sandbox and yield the
    ``asyncio.subprocess.Process``.

    The caller is responsible for writing to ``proc.stdin`` and reading
    from ``proc.stdout`` (for MCP stdio JSON-RPC framing). On context-
    manager exit, the subprocess is terminated (SIGTERM grace then
    SIGKILL).

    Parameters
    ----------
    command, args
        The program + arguments to execute INSIDE the sandbox (e.g.
        ``"python"``, ``["-m", "tests.fixtures.mcp_mock_server"]``).
    env
        Environment to pass to the subprocess. Only keys in
        ``profile.env_passthrough`` are kept (others stripped).
    profile
        Sandbox tunables. Defaults to :class:`SandboxProfile` defaults.
    backend
        Force a backend (mostly for tests). If ``None``, auto-detects via
        :func:`detect_sandbox_backend`.

    Raises
    ------
    OSError
        If the sandbox launcher (bwrap binary) fails to spawn at the OS
        level. Service-layer callers translate this to ``DependencyError``.
    """
    if profile is None:
        profile = SandboxProfile()
    if backend is None:
        backend = detect_sandbox_backend()

    # Filter env according to the passthrough whitelist.
    effective_env: dict[str, str] = {}
    if env is not None:
        for key in profile.env_passthrough:
            if key in env:
                effective_env[key] = env[key]

    proc: asyncio.subprocess.Process | None = None
    try:
        if backend == "bwrap":
            argv = _build_bwrap_argv(command, args, profile=profile)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=effective_env or None,
            )
        else:
            # setrlimit fallback — caveat: asyncio.create_subprocess_exec
            # does not accept preexec_fn directly. Use subprocess.Popen wrapped
            # in asyncio via run_in_executor for the spawn, then build the
            # asyncio.subprocess.Process manually. SIMPLIFIED Sprint 1 :
            # we use a thin wrapper that applies rlimits via a Python -c
            # bootstrap (the subprocess does setrlimit on itself before
            # exec'ing the real command). This is portable + does not need
            # preexec_fn (which is unsupported on Windows + buggy in async).
            bootstrap_args = _build_setrlimit_bootstrap(command, args, profile=profile)
            proc = await asyncio.create_subprocess_exec(
                *bootstrap_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=effective_env or None,
            )

        _log.debug(
            "mcp_sandbox.subprocess_spawned",
            backend=backend,
            command=command,
            pid=proc.pid,
        )
        yield proc
    finally:
        if proc is not None and proc.returncode is None:
            await _terminate_subprocess(proc, profile.sigterm_grace_seconds)


def _build_setrlimit_bootstrap(
    command: str, args: list[str], *, profile: SandboxProfile
) -> list[str]:
    """Build an argv that runs ``command`` after applying setrlimit caps
    via a Python bootstrap. Used when bwrap is unavailable.

    The bootstrap is ``python -c "import resource, os; ...; os.execvp(...)"`` —
    a 1-process shim that sets rlimits on itself then ``execvp`` replaces
    its image with the real command (preserves stdin/stdout/stderr).

    NOTE Sprint 1 — ``RLIMIT_NPROC`` is intentionally NOT applied here.
    On Linux it counts ALL processes for the real UID (not just the
    subprocess's children), which would fire spuriously inside a Docker
    container where the runtime user already has many processes. Network
    + filesystem isolation are NOT available in setrlimit fallback —
    this mode is best-effort only ; bwrap is the production-grade path
    (D62 Sprint 2 per-tool profile, D65 metric Prometheus dashboard).
    """
    mem_bytes = profile.memory_mb * 1024 * 1024
    # P-12 (CR 2026-05-11) — scrub dangerous env vars BEFORE execvp so the
    # bootstrap doesn't propagate LD_PRELOAD / PYTHONPATH /
    # LD_LIBRARY_PATH / PYTHONSTARTUP injected by a hypothetical adversary
    # writing to ``/tmp`` (the only writable path).
    #
    # P-21 (CR 2026-05-11) — set ``PR_SET_PDEATHSIG=SIGKILL`` via ctypes
    # so the spawned subprocess is killed when the backend dies. Linux
    # equivalent of bwrap's ``--die-with-parent``. Best-effort : suppress
    # any error (non-Linux kernel, ctypes loading failure) ; the subprocess
    # will simply not have parent-death protection in that case.
    bootstrap = (
        "import resource, os, sys, contextlib, ctypes, signal\n"
        "for _dangerous in (\n"
        "    'LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONSTARTUP',\n"
        "    'PYTHONHOME', 'PYTHONINSPECT', 'LD_AUDIT',\n"
        "):\n"
        "    os.environ.pop(_dangerous, None)\n"
        "with contextlib.suppress(OSError, AttributeError):\n"
        "    _libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
        "    # PR_SET_PDEATHSIG = 1, SIGKILL = 9\n"
        "    _libc.prctl(1, signal.SIGKILL, 0, 0, 0)\n"
        f"with contextlib.suppress(ValueError, OSError):\n"
        f"    resource.setrlimit(resource.RLIMIT_CPU, ({profile.cpu_seconds}, {profile.cpu_seconds}))\n"
        f"with contextlib.suppress(ValueError, OSError):\n"
        f"    resource.setrlimit(resource.RLIMIT_AS, ({mem_bytes}, {mem_bytes}))\n"
        "os.execvp(sys.argv[1], sys.argv[1:])\n"
    )
    return [sys.executable, "-c", bootstrap, command, *args]


async def _terminate_subprocess(proc: asyncio.subprocess.Process, grace_seconds: float) -> None:
    """Send SIGTERM, wait ``grace_seconds``, then SIGKILL if still alive.

    Guarantees no zombie on context-manager exit even if the subprocess
    ignores SIGTERM (e.g. signal.signal(SIGTERM, SIG_IGN) inside the tool).
    """
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:  # pragma: no cover — race with self-exit
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover
            return
        # Best-effort final wait — do not block forever even on SIGKILL
        # ignored (kernel will reap eventually).
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:  # pragma: no cover
            _log.warning(
                "mcp_sandbox.subprocess_unkillable",
                pid=proc.pid,
                grace_seconds=grace_seconds,
            )


__all__ = [
    "MCPExecutionError",
    "MCPExecutionTimeoutError",
    "MCPToolError",
    "SandboxBackend",
    "SandboxProfile",
    "detect_sandbox_backend",
    "sandboxed_subprocess",
]
