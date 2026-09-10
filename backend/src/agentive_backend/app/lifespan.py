"""Startup / shutdown hooks for the FastAPI app."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import bcrypt
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agentive_backend.features.agent_registry import load_registry
from agentive_backend.features.memory_manager.push_memory import (
    MemoryManagerPushMemoryProvider,
)
from agentive_backend.features.memory_manager.service import MemoryManagerService
from agentive_backend.features.memory_manager.ttl import MemoryArchivalWorker
from agentive_backend.features.workflow_engine.recovery import WorkflowRecoveryWorker
from agentive_backend.features.workflow_engine.service import (
    WorkflowExecutionService,
    cancel_inflight_runs,
)
from agentive_backend.infra.db.session import get_session_factory
from agentive_backend.infra.llm import (
    AnthropicProvider,
    FastEmbedProvider,
    OpenAIProvider,
    VoyageProvider,
)
from agentive_backend.infra.llm.fastembed_adapter import (
    EMBEDDING_DIMENSIONS as FASTEMBED_EMBEDDING_DIMENSIONS,
)
from agentive_backend.infra.llm.fastembed_adapter import (
    EMBEDDING_MODEL_NAME as FASTEMBED_EMBEDDING_MODEL_NAME,
)
from agentive_backend.infra.llm.openai_adapter import (
    EMBEDDING_DIMENSIONS as OPENAI_EMBEDDING_DIMENSIONS,
)
from agentive_backend.infra.llm.openai_adapter import (
    EMBEDDING_MODEL_NAME as OPENAI_EMBEDDING_MODEL_NAME,
)
from agentive_backend.infra.llm.voyage_adapter import (
    EMBEDDING_DIMENSIONS as VOYAGE_EMBEDDING_DIMENSIONS,
)
from agentive_backend.infra.llm.voyage_adapter import (
    EMBEDDING_MODEL_NAME as VOYAGE_EMBEDDING_MODEL_NAME,
)
from agentive_backend.shared.config import settings
from agentive_backend.shared.contracts.events import (
    SystemShutdownEvent,
    SystemStartedEvent,
)
from agentive_backend.shared.correlation import _correlation_id_var, new_correlation_id
from agentive_backend.shared.event_bus import OutboxWorker, publish_and_commit
from agentive_backend.shared.llm import (
    Completion,
    Embedder,
    EmbeddingRouter,
    FallbackCallback,
    FallbackContext,
    LLMRouter,
)
from agentive_backend.shared.llm.testing import MockEmbedder, MockProvider
from agentive_backend.shared.logging import configure_logging, get_logger
from agentive_backend.shared.repositories import (
    AgentTemplateRepo,
    ChunkEmbeddingRepo,
    MemoryChunkRepo,
    NamespaceRepo,
    WorkflowRepo,
    WorkflowRunRepo,
)

log = get_logger(__name__)

# LangGraph checkpointer pool (Story 4.2 T9.1, review finding #8). Small on
# purpose: checkpoint writes are short and serialized per run, so this sizes
# for concurrent RUNS, not for request throughput. `max_lifetime` recycles
# connections so a long-lived process never accumulates stale sockets behind
# a load balancer or a Postgres `idle_session_timeout`.
_CHECKPOINT_POOL_MIN_SIZE = 1
_CHECKPOINT_POOL_MAX_SIZE = 10
_CHECKPOINT_POOL_MAX_LIFETIME_S = 30 * 60.0
_CHECKPOINT_POOL_OPEN_TIMEOUT_S = 10.0


def _workflow_checkpoint_dsn() -> str:
    """Canonical ``postgresql://`` DSN for the runtime :class:`AsyncPostgresSaver`
    (Story 4.2 T9.1) — mirror ``spike/m3_langgraph.py::_checkpoint_dsn``, but
    on ``settings.database_url`` (``agentive_app`` role, DML-only grants)
    rather than the owner DSN the T1.2 migration used for ``CREATE TABLE``.
    """
    return settings.psycopg_dsn


# bcrypt hash version prefixes — 2a/2b/2y are all valid bcrypt outputs from
# different implementations (PHP commonly emits $2y$, modern Python emits $2b$,
# legacy ones $2a$). Any of them must be treated as "already hashed" — falling
# through to plaintext compare would either crash production or silently store
# the hash STRING as the comparison plaintext (auth bypass risk).
_BCRYPT_PREFIX_RE = re.compile(r"^\$2[aby]\$\d{2}\$")


def _init_auth_token(app: FastAPI) -> None:
    """Initialise ``app.state.auth_token_hash`` from ``AGENTIVE_API_TOKEN``.

    Mode detection:
    * Matches ``$2[aby]$NN$`` (any standard bcrypt prefix) → already hashed.
    * Anything else                                       → plaintext.

    Hard guards (raise :class:`RuntimeError` to abort boot):
    * Empty ``AGENTIVE_API_TOKEN``        → always rejected.
    * Plaintext in production             → rejected with operator hint.
    * Hash-looking value but malformed    → rejected via smoke ``bcrypt.checkpw``.
    """
    raw = settings.agentive_api_token.get_secret_value()

    # P4 — empty token is never acceptable. Without this guard, an empty env
    # var falls through to plaintext mode and ``verify_token("", "")`` returns
    # True via ``hmac.compare_digest`` → unauthenticated requests would be
    # accepted with a literal ``Authorization: Bearer `` header.
    if not raw:
        log.critical("auth.token_init_failed_empty", env=settings.environment)
        raise RuntimeError(
            "AGENTIVE_API_TOKEN is empty. Set it to a bcrypt hash (production) "
            "or any non-empty plaintext value (dev/test)."
        )

    # P3 — accept all standard bcrypt prefixes ($2a$, $2b$, $2y$), not only $2b$.
    is_bcrypt_shaped = bool(_BCRYPT_PREFIX_RE.match(raw))
    mode = "bcrypt" if is_bcrypt_shaped else "plaintext"

    # P12 — emit CRITICAL telemetry before fail-fast so operators see the
    # cause even if the RuntimeError traceback is suppressed by a process
    # supervisor.
    if settings.is_production and mode == "plaintext":
        log.critical(
            "auth.token_init_failed_production_plaintext",
            env=settings.environment,
        )
        raise RuntimeError(
            "AGENTIVE_API_TOKEN must be a bcrypt hash in production. "
            'Generate one with: python -c "'
            "import bcrypt, secrets; "
            "print(bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt(12)).decode())"
            '"'
        )

    # P3 — smoke-check the hash structure so a truncated/corrupt $2b$ value
    # does not boot silently and then 401 every request without explanation.
    # ``bcrypt.checkpw`` raises ``ValueError`` on a malformed hash; we don't
    # care about the boolean result, only that parsing succeeds.
    if mode == "bcrypt":
        try:
            bcrypt.checkpw(b"smoke-test", raw.encode())
        except (ValueError, TypeError) as exc:
            log.critical("auth.token_init_failed_malformed_bcrypt", error=str(exc))
            raise RuntimeError(
                "AGENTIVE_API_TOKEN looks like a bcrypt hash (matched $2[aby]$ "
                "prefix) but is malformed — bcrypt.checkpw could not parse it. "
                "Re-generate the hash and verify there is no truncation or "
                "stray whitespace."
            ) from exc

    app.state.auth_token_hash = raw
    log.info("auth.token_initialized", mode=mode, env=settings.environment)


def _enforce_mcp_sandbox_policy(backend: str) -> None:
    """Guard the flag/sandbox-backend combination at boot (audit A-03 / 5.3).

    The ``setrlimit`` fallback caps CPU/memory/nproc but CANNOT isolate
    network or filesystem (documented in ``infra/mcp/sandbox.py``). With
    ``AGENTIVE_ALLOW_MCP_REGISTRATION=true`` on such a backend, executing
    an untrusted MCP tool is an uncontained RCE/SSRF surface. Before this
    guard the degradation was silent (a lone WARNING at detection time,
    not correlated with the flag).

    Policy — same fail-fast philosophy as :func:`_init_auth_token`:
    * production → refuse to boot (``RuntimeError`` with operator hint) ;
    * dev/test   → loud WARNING (bwrap is commonly inoperative in local
      Docker profiles ; blocking dev would hurt more than it protects).
    """
    if not settings.mcp_allow_registration or backend != "setrlimit":
        return
    if settings.is_production:
        log.critical(
            "mcp_sandbox.refusing_boot_degraded_backend",
            backend=backend,
            env=settings.environment,
        )
        raise RuntimeError(
            "AGENTIVE_ALLOW_MCP_REGISTRATION=true but the effective sandbox "
            "backend is 'setrlimit' — no network/filesystem isolation for "
            "MCP tool execution. Refusing to boot in production. Fix: run "
            "the backend with a bwrap-capable kernel (CAP_SYS_ADMIN or "
            "kernel.unprivileged_userns_clone=1), or set "
            "AGENTIVE_ALLOW_MCP_REGISTRATION=false."
        )
    log.warning(
        "mcp_sandbox.degraded_backend_with_registration_enabled",
        backend=backend,
        risk="MCP tools run WITHOUT network/filesystem isolation (RCE/SSRF uncontained)",
        remediation=(
            "enable bwrap (CAP_SYS_ADMIN / kernel.unprivileged_userns_clone=1) "
            "or set AGENTIVE_ALLOW_MCP_REGISTRATION=false"
        ),
    )


def _build_llm_router(*, on_fallback: FallbackCallback | None = None) -> LLMRouter:
    """Build the singleton :class:`LLMRouter` for the process.

    Decision matrix
    ---------------
    * Both keys absent + ``environment == "production"`` → :class:`RuntimeError`.
      A prod deployment without LLM access is almost certainly a misconfig.
    * Both keys absent + dev/test → wire a :class:`MockProvider` so the
      app boots locally without secrets.
    * Anthropic only / OpenAI only → wire the configured provider as the
      sole entry of the chain. Fallback gracefully degrades to "no
      fallback" rather than failing the boot.
    * Both keys present → canonical Sprint 0 chain ``["anthropic", "openai"]``.

    The ``on_fallback`` kwarg accepts the bus-publishing callback wired
    by :func:`lifespan` once the session factory is available — passing
    it here avoids the previous private-attribute mutation
    (``llm_router._on_fallback = ...``) and the race window between
    ``app.state.llm_router = ...`` and that mutation (review fix-batch P3).
    """
    anthropic_key = settings.anthropic_api_key
    openai_key = settings.openai_api_key

    has_anthropic = anthropic_key is not None and anthropic_key.get_secret_value()
    has_openai = openai_key is not None and openai_key.get_secret_value()

    if not has_anthropic and not has_openai:
        if settings.is_production:
            raise RuntimeError(
                "Refusing to boot in production without any LLM provider "
                "configured. Set ANTHROPIC_API_KEY and/or OPENAI_API_KEY."
            )
        log.warning(
            "llm_router.built_with_mock",
            reason="no_api_keys_configured",
            environment=settings.environment,
        )
        # P20 — synthesize an UNBOUNDED mock that always returns the same
        # placeholder response. The previous list of 1024 pre-built
        # Completions caused the dev provider to silently start raising
        # ``ValueError`` (= fatal) after ~1024 dev calls.
        placeholder = Completion(
            text="[mock] no provider configured",
            model="mock-model",
            provider="mock",
            input_tokens=0,
            output_tokens=10,
            finish_reason="stop",
            latency_ms=1.0,
            cost_estimate_usd=Decimal("0"),
        )
        mock = MockProvider("mock", [], infinite_default=placeholder)
        return LLMRouter(
            providers={"mock": mock},
            default_chain=["mock"],
            on_fallback=on_fallback,
        )

    providers: dict[str, Any] = {}
    chain: list[str] = []
    if has_anthropic:
        assert anthropic_key is not None
        providers["anthropic"] = AnthropicProvider(api_key=anthropic_key)
        chain.append("anthropic")
    if has_openai:
        assert openai_key is not None
        providers["openai"] = OpenAIProvider(api_key=openai_key)
        chain.append("openai")

    log.info(
        "llm_router.built",
        providers=list(providers),
        default_chain=chain,
        environment=settings.environment,
    )
    return LLMRouter(
        providers=providers,
        default_chain=chain,
        on_fallback=on_fallback,
    )


def _build_embedding_router() -> EmbeddingRouter:
    """Build the singleton :class:`EmbeddingRouter` for the process
    (Story 3.6 T6.1) — replaces the single-provider :func:`_build_embedder`
    it superseded (Story 3.1 T1.5).

    Decision matrix
    ---------------
    * ``cloud`` (OpenAI) — mandatory, unchanged decision from the old
      ``_build_embedder``: no key + production → :class:`RuntimeError`; no
      key + dev/test → :class:`MockEmbedder`.
    * ``local`` (FastEmbed) — always ATTEMPTED, no API key needed, but the
      load can fail (network unreachable to the HuggingFace Hub, or the
      first-run download not yet cached — T2.1 loads eagerly at
      construction). Decision: catch that failure, log it, and simply leave
      ``"local"`` out of ``providers`` in EVERY environment (dev AND
      production) — the same "optional backend that may not be wired"
      posture as ``voyage`` below, not a boot-blocking failure. Décision
      John 2026-09-09 (Story 3.6 code review) : namespaces configured for
      ``local`` get an explicit 503 from :meth:`EmbeddingRouter.resolve`
      until the model is reachable — NOT a silent fallback to ``cloud``,
      which would break write/read backend symmetry. Crashing the WHOLE
      process — search and chunk writes on every other namespace included —
      over one optional backend would still be a strictly worse failure
      mode than serving every other namespace while this one 503s.
    * ``voyage`` — built only when ``VOYAGE_API_KEY`` is configured;
      absent, the backend simply does not exist in ``providers`` (same
      "optional, refuses explicitly rather than silently" contract as
      ``local``'s failure path above).
    """
    providers: dict[str, Embedder] = {}
    model_by_backend: dict[str, str] = {}
    dimensions_by_backend: dict[str, int] = {}

    # `.strip()`: a whitespace-only secret is truthy, and used to let the
    # production boot succeed with a key that fails 401 on every request
    # instead of refusing to start. The `is None` / falsy test also narrows
    # for mypy, replacing an `assert` that `python -O` would have erased
    # (code review Story 3.1, P12).
    openai_key = settings.openai_api_key
    if openai_key is None or not openai_key.get_secret_value().strip():
        if settings.is_production:
            raise RuntimeError(
                "Refusing to boot in production without OPENAI_API_KEY — "
                "the memory manager embedding backend requires it."
            )
        log.warning(
            "embedding_router.cloud_built_with_mock",
            reason="no_openai_api_key_configured",
            environment=settings.environment,
        )
        providers["cloud"] = MockEmbedder()
    else:
        providers["cloud"] = OpenAIProvider(api_key=openai_key)
    model_by_backend["cloud"] = OPENAI_EMBEDDING_MODEL_NAME
    dimensions_by_backend["cloud"] = OPENAI_EMBEDDING_DIMENSIONS

    try:
        providers["local"] = FastEmbedProvider()
    except Exception:
        log.exception(
            "embedding_router.local_backend_unavailable",
            reason="fastembed_model_load_failed",
        )
    else:
        model_by_backend["local"] = FASTEMBED_EMBEDDING_MODEL_NAME
        dimensions_by_backend["local"] = FASTEMBED_EMBEDDING_DIMENSIONS

    voyage_key = settings.voyage_api_key
    normalized_voyage_key = voyage_key.get_secret_value().strip() if voyage_key is not None else ""
    if normalized_voyage_key:
        providers["voyage"] = VoyageProvider(api_key=normalized_voyage_key)
        model_by_backend["voyage"] = VOYAGE_EMBEDDING_MODEL_NAME
        dimensions_by_backend["voyage"] = VOYAGE_EMBEDDING_DIMENSIONS

    log.info(
        "embedding_router.built",
        backends=sorted(providers),
        environment=settings.environment,
    )
    return EmbeddingRouter(
        providers=providers,
        model_by_backend=model_by_backend,
        dimensions_by_backend=dimensions_by_backend,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan : logging config, event-bus worker, lifecycle events."""
    configure_logging()
    log.info("agentive_startup", version=app.version)
    started_monotonic = time.monotonic()

    # Initialise auth token hash from env (fail-fast in production if plaintext).
    _init_auth_token(app)

    # Story 2.1 — load the 8 universal archetypes registry. Fail-fast if the
    # YAML is missing/malformed (RuntimeError surfaces the cause to the
    # operator instead of the app booting with an empty registry).
    app.state.archetype_registry = load_registry()
    log.info("archetype_registry_loaded", count=len(app.state.archetype_registry))

    # Story 2.6 — detect sandbox backend once at boot. Stored on app.state
    # so the m5 service reads it without re-running ``shutil.which`` per call
    # (avoid TOCTOU + cheap to memoize). Warning logged inside the helper
    # when bwrap is unavailable.
    from agentive_backend.infra.mcp.sandbox import detect_sandbox_backend

    app.state.mcp_sandbox_backend = detect_sandbox_backend()
    log.info(
        "mcp_sandbox.backend_selected",
        backend=app.state.mcp_sandbox_backend,
    )
    # Audit A-03 (5.3) — refuse (prod) or warn (dev) when MCP registration
    # is enabled while the sandbox has no network/FS isolation.
    _enforce_mcp_sandbox_policy(app.state.mcp_sandbox_backend)
    # P-14 (CR 2026-05-11) — AC3 demands a ``mcp_sandbox_backend{kind=...}``
    # counter (per-backend invocation count). Stored on app.state as a
    # plain dict ; Prometheus integration formalized Story 7.x (D65).
    app.state.mcp_sandbox_invocations = {"bwrap": 0, "setrlimit": 0}

    # Build the session factory FIRST so the LLM fallback callback can
    # close over it. The callback is wired into the LLMRouter at
    # construction time (no post-construction private-attribute mutation).
    session_factory = get_session_factory()
    # P1 — middleware + admin endpoints (auth audit, rotate-token) read
    # ``request.app.state.session_factory`` to schedule background outbox
    # writes. Previously the factory was only a local in this scope, so
    # every authenticated /api/v1/* request crashed with AttributeError.
    app.state.session_factory = session_factory

    async def _publish_fallback(ctx: FallbackContext) -> None:
        """Publish ``workflow_engine.llm.fallback_triggered`` on the event bus.

        Captures the per-call ``correlation_id`` from the FallbackContext
        (router populates it from the structlog ContextVar at fallback
        time) and binds it on this publish — without binding,
        ``publish_and_commit`` would raise ``MissingCorrelationIdError``
        in non-HTTP code paths (review fix-batch P8).
        """
        # Bind a correlation_id on this background task. If the router
        # captured an unbound ContextVar earlier the value is the literal
        # ``"unbound"`` — use a fresh UUID instead so the audit chain is
        # still meaningful.
        try:
            from uuid import UUID

            cid: str | UUID = ctx.correlation_id
            if cid == "unbound":
                cid = new_correlation_id()
            else:
                # Best-effort UUID parse — fall back to the raw string if
                # somebody pushes a non-UUID id (e.g. external trace id).
                with contextlib.suppress(ValueError):
                    cid = UUID(str(cid))

            token = _correlation_id_var.set(str(cid))
            try:
                async with session_factory() as session:
                    await publish_and_commit(
                        session,
                        "workflow_engine.llm.fallback_triggered",
                        {
                            "failed_provider": ctx.failed_provider,
                            "next_provider": ctx.next_provider,
                            "error_class": ctx.error_class,
                            "error_type": ctx.error_type,
                            "model_attempted": ctx.model_attempted,
                            "model_fallback": ctx.model_fallback,
                            "correlation_id": str(cid),
                        },
                    )
            finally:
                _correlation_id_var.reset(token)
        except Exception:
            # P39 — keep the warning informative: include the
            # failed_provider so operators can correlate with router
            # metrics, and use exception-info logging.
            log.exception(
                "llm_fallback_event_publish_failed",
                failed_provider=ctx.failed_provider,
                next_provider=ctx.next_provider,
            )

    # Build the LLM router with the callback wired at construction —
    # eliminates the race window where requests landing between
    # ``app.state.llm_router = ...`` and ``llm_router._on_fallback = ...``
    # would silently no-op the fallback event.
    llm_router = _build_llm_router(on_fallback=_publish_fallback)
    app.state.llm_router = llm_router

    # Story 3.1 T1.5 / Story 3.6 T6.2 — memory manager embedding backend,
    # wired once at boot (same lifetime as llm_router). `embedding_router`
    # replaces the single-provider `embedder` attribute.
    app.state.embedding_router = _build_embedding_router()

    # Story 3.5 T11.1 — Push Memory : a full MemoryManagerService, built
    # here (not via `memory_manager/router.py._build_service`, request-
    # scoped) because `app` may import several features (Contract 2),
    # unlike `features.playground` which may not import
    # `features.memory_manager` directly (Contract 1). Mirrors the import
    # already at the top of this module for `MemoryArchivalWorker`.
    app.state.push_memory_provider = MemoryManagerPushMemoryProvider(
        memory_manager_service=MemoryManagerService(
            memory_chunk_repo=MemoryChunkRepo(session_factory=session_factory),
            chunk_embedding_repo=ChunkEmbeddingRepo(session_factory=session_factory),
            namespace_repo=NamespaceRepo(session_factory=session_factory),
            embedding_router=app.state.embedding_router,
        )
    )

    # Background tasks need an explicit correlation_id — there is no HTTP
    # request to inherit from, so the middleware never runs at startup.
    # We bind via a token that is reset before yielding so the value never
    # leaks into the first HTTP request handler (P2).
    startup_token = _correlation_id_var.set(new_correlation_id())

    worker = OutboxWorker(session_factory=session_factory)
    try:
        await worker.start()
    except Exception:
        log.exception("agentive_outbox_worker_start_failed")
        _correlation_id_var.reset(startup_token)
        raise
    app.state.outbox_worker = worker

    # Story 3.3 T9.1 — same start-up posture as OutboxWorker just above.
    memory_archival_worker = MemoryArchivalWorker(session_factory=session_factory)
    try:
        await memory_archival_worker.start()
    except Exception:
        log.exception("agentive_memory_archival_worker_start_failed")
        # Unlike the OutboxWorker block above, this one is NOT first in the
        # sequence: the outbox worker is already running with a live LISTEN
        # connection, and raising here abandons the lifespan before its
        # `finally` ever runs. Copying that block verbatim therefore leaked
        # the `_listen_loop` task and its connection (code review Story 3.3, P6).
        with contextlib.suppress(Exception):
            await worker.stop()
        _correlation_id_var.reset(startup_token)
        raise
    app.state.memory_archival_worker = memory_archival_worker

    # Story 4.2 T9.1 — LangGraph checkpointer, opened ONCE for the app's
    # lifetime (mirror `session_factory`/`llm_router`, not per-request).
    # `agentive_app` role (DML-only grants, T1.2 migration) — `CREATE TABLE`
    # already ran there under `agentive_owner`; T9.2 deliberately does NOT
    # call `checkpointer.setup()` again here (least-privilege posture).
    # `AsyncExitStack` closes it in the shutdown sequence below.
    #
    # A POOL, not `AsyncPostgresSaver.from_conn_string`. T9.1's single
    # long-lived connection had no recovery path whatsoever: one network
    # blip, one Postgres restart, one `idle_in_transaction_session_timeout`,
    # and EVERY subsequent run in the process failed to checkpoint until an
    # operator restarted it — the checkpointer being the one resource the
    # whole crash-recovery story rests on. `AsyncConnectionPool` reconnects
    # on its own, and `AsyncPostgresSaver` accepts one directly (its `conn`
    # parameter is typed `AsyncConnection | AsyncConnectionPool`).
    # `kwargs` mirrors `from_conn_string`'s own connection settings exactly —
    # `autocommit`/`prepare_threshold`/`row_factory` are load-bearing for the
    # saver, not stylistic.
    workflow_exit_stack = contextlib.AsyncExitStack()
    try:
        workflow_pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            conninfo=_workflow_checkpoint_dsn(),
            min_size=_CHECKPOINT_POOL_MIN_SIZE,
            max_size=_CHECKPOINT_POOL_MAX_SIZE,
            max_lifetime=_CHECKPOINT_POOL_MAX_LIFETIME_S,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
        )
        await workflow_exit_stack.enter_async_context(workflow_pool)
        # Fail fast at boot rather than on the first run: `open()` alone is
        # lazy, `wait()` proves the credentials and the network actually work.
        await workflow_pool.wait(timeout=_CHECKPOINT_POOL_OPEN_TIMEOUT_S)
        workflow_checkpointer = AsyncPostgresSaver(workflow_pool)
    except Exception:
        log.exception("agentive_workflow_checkpointer_start_failed")
        with contextlib.suppress(Exception):
            await memory_archival_worker.stop()
        with contextlib.suppress(Exception):
            await worker.stop()
        _correlation_id_var.reset(startup_token)
        raise
    app.state.workflow_checkpointer = workflow_checkpointer

    # Story 4.2 T9.3 — one shared `WorkflowExecutionService` for BOTH the
    # recovery worker and the router (`router.py` reads it straight from
    # `app.state` instead of reconstructing its repos per request).
    workflow_execution_service = WorkflowExecutionService(
        workflow_repo=WorkflowRepo(session_factory=session_factory),
        workflow_run_repo=WorkflowRunRepo(session_factory=session_factory),
        template_repo=AgentTemplateRepo(session_factory=session_factory),
        llm_router=llm_router,
        checkpointer=workflow_checkpointer,
    )
    app.state.workflow_execution_service = workflow_execution_service

    workflow_recovery_worker = WorkflowRecoveryWorker(
        workflow_execution_service=workflow_execution_service,
        session_factory=session_factory,
    )
    try:
        await workflow_recovery_worker.start()
    except Exception:
        log.exception("agentive_workflow_recovery_worker_start_failed")
        with contextlib.suppress(Exception):
            await memory_archival_worker.stop()
        with contextlib.suppress(Exception):
            await worker.stop()
        with contextlib.suppress(Exception):
            await workflow_exit_stack.aclose()
        _correlation_id_var.reset(startup_token)
        raise
    app.state.workflow_recovery_worker = workflow_recovery_worker

    # Best-effort startup event — a transient DB hiccup must not prevent the
    # app from serving traffic (the worker will replay any orphaned writes
    # next time the publish path succeeds).
    try:
        async with session_factory() as session:
            await publish_and_commit(
                session,
                SystemStartedEvent.event_type,
                SystemStartedEvent(version=app.version),
            )
    except Exception:
        log.exception("agentive_startup_event_publish_failed")

    # Reset the ContextVar BEFORE yielding so the first HTTP request handler
    # gets a fresh state and the middleware's own set_correlation_id wins.
    _correlation_id_var.reset(startup_token)

    try:
        yield
    finally:
        # New correlation_id scoped to the shutdown sequence so its logs
        # cluster — reset on exit so we don't leak into post-yield Python
        # cleanup (uvicorn worker, etc.).
        shutdown_token = _correlation_id_var.set(new_correlation_id())
        try:
            uptime_seconds = time.monotonic() - started_monotonic
            try:
                async with session_factory() as session:
                    await publish_and_commit(
                        session,
                        SystemShutdownEvent.event_type,
                        SystemShutdownEvent(uptime_seconds=uptime_seconds),
                    )
            except Exception:
                log.exception("agentive_shutdown_event_publish_failed")

            # P8 — drain pending audit fire-and-forget tasks BEFORE stopping
            # the outbox worker. Without this, in-flight `system.token.used`
            # writes get cancelled mid-transaction during a rolling deploy
            # under load → the outbox row never commits → audit gap. We cap
            # the wait at 5s so a hung task can't delay shutdown indefinitely.
            from agentive_backend.app.middleware import _background_tasks

            if _background_tasks:
                pending = list(_background_tasks)
                log.info("agentive_drain_background_tasks", count=len(pending))
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=5.0,
                    )

            # Story 4.2 T5.4 — still NO drain of workflow_engine's own
            # `_background_tasks` (fire-and-forget `start_run`/resume tasks):
            # an in-flight run interrupted by shutdown IS the AC3 scenario,
            # and waiting for every run to finish would violate NFR3 (a
            # restart must not be blocked by a long-running run).
            #
            # But "no drain" is not "no handling". These three steps are
            # ORDER-CRITICAL:
            #   1. stop the recovery worker first, so it cannot claim a new
            #      orphan while we are tearing the checkpointer down;
            #   2. CANCEL the in-flight run tasks — `CancelledError` derives
            #      from `BaseException`, so `_execute`'s `except Exception`
            #      lets it through and the run stays `running`, claimable by
            #      the next process's sweep;
            #   3. only then close the checkpointer.
            # Skipping step 2 (the previous behaviour) meant `aclose()` shut
            # the connection under those tasks, each raised a plain
            # `Exception`, and `_mark_failed` buried the run in the terminal
            # `error` status that no recovery sweep ever revisits — the exact
            # opposite of what this comment used to promise.
            with contextlib.suppress(Exception):
                await workflow_recovery_worker.stop()
            with contextlib.suppress(Exception):
                cancelled = await cancel_inflight_runs()
                if cancelled:
                    log.info("agentive_cancelled_inflight_runs", count=cancelled)
            with contextlib.suppress(Exception):
                await workflow_exit_stack.aclose()
            with contextlib.suppress(Exception):
                await memory_archival_worker.stop()
            with contextlib.suppress(Exception):
                await worker.stop()
            log.info("agentive_shutdown", uptime_seconds=uptime_seconds)
        finally:
            _correlation_id_var.reset(shutdown_token)


__all__ = ["_build_llm_router", "_enforce_mcp_sandbox_policy", "_init_auth_token", "lifespan"]
