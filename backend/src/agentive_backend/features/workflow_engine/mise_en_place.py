"""Mise en Place automatique pré-workflow — orchestration (Story 4.5 AC1-AC3).

``MiseEnPlaceService.run_checks()`` runs the four pre-workflow checks — MCP
tool reachability, memory namespace existence, budget (via
:class:`~.dry_run.DryRunService`, reused as-is), LLM provider API-key
presence — IN PARALLEL (``asyncio.gather``) and returns a
:class:`~.domain.mise_en_place.MiseEnPlaceReport`. It never raises on a
failing check (a report with ``passed=False`` entries is a normal return
value) and never decides whether to block a launch — that decision belongs
to its caller, ``WorkflowExecutionService.start_run`` (T3.7), which alone
has access to the request's ``force``/``reason``.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Coroutine, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from agentive_backend.features.workflow_engine.domain.mise_en_place import (
    CHECK_CODES,
    CheckCode,
    CheckResult,
    MiseEnPlaceReport,
    build_report,
)
from agentive_backend.infra.llm.pricing import ANTHROPIC_MODEL_PRICING, OPENAI_MODEL_PRICING
from agentive_backend.infra.mcp.client import discover_tools
from agentive_backend.shared.exceptions import NotFoundError
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from agentive_backend.features.workflow_engine.dry_run import DryRunService
    from agentive_backend.infra.db.models import AgentTemplate, ToolServer
    from agentive_backend.shared.repositories import (
        AgentTemplateToolRepo,
        NamespaceRepo,
        ToolServerRepo,
    )

_log = get_logger(__name__)

#: `(provider_name, pricing_table)` — mirror `dry_run._provider_pricing`,
#: reduced to a membership test (this check needs a provider NAME, never a
#: cost — no token counts exist at this point in the flow).
_PROVIDER_PRICING: tuple[tuple[str, Mapping[str, Any]], ...] = (
    ("anthropic", ANTHROPIC_MODEL_PRICING),
    ("openai", OPENAI_MODEL_PRICING),
)

#: The model a template without an explicit `llm_model` actually runs on —
#: mirror `engine.agent_node.DEFAULT_LLM_MODEL` and `dry_run._DEFAULT_LLM_MODEL`
#: (both `config.get("llm_model") or DEFAULT`). Duplicated rather than
#: imported: `engine/__init__` pulls LangGraph in transitively, and this
#: module sits on the launch path of every run. Keep in sync with those two.
_DEFAULT_LLM_MODEL: Final = "claude-sonnet-4-6"

#: Ceiling on MCP probes running at once. `discover_tools` on a `stdio`
#: server SPAWNS A SUBPROCESS, so an unbounded `gather` turned a 20-server
#: workflow into 20 concurrent subprocesses per HTTP request (review P12).
_MAX_CONCURRENT_PROBES: Final = 8

#: The one archetype whose Push Memory is opt-IN (Story 3.5 AC2). For every
#: other archetype `push_memory.optin` is ignored and a configured namespace
#: is live — `{"namespace": "x", "optin": false}` means ENABLED there, and
#: only `namespace: null` means off.
_CONTROLEUR_ARCHETYPE: Final = "controleur"


def _config_of(template: AgentTemplate) -> Mapping[str, Any]:
    """``template.config`` when it really is a mapping, ``{}`` otherwise.

    The column is ``JSONB NOT NULL``, which guarantees valid JSON — not a
    JSON *object*. A row written outside the ORM holding ``'null'``, ``'[]'``
    or ``'"x"'` yields ``None``/``list``/``str`` here, and ``.get(...)`` on
    any of those is an ``AttributeError`` mid-check (review P17).
    """
    config = template.config
    return config if isinstance(config, Mapping) else {}


def _label_servers(servers: Sequence[ToolServer]) -> list[str]:
    """Sorted, human-readable labels for unreachable servers.

    ``tool_servers.name`` carries no uniqueness constraint, so two distinct
    servers can share one — and "unreachable: srv, srv" told an operator
    nothing about which to restart. Colliding names only are suffixed with
    an id prefix (review P20).
    """
    counts = Counter(server.name for server in servers)
    return sorted(
        server.name if counts[server.name] == 1 else f"{server.name} ({str(server.id)[:8]})"
        for server in servers
    )


def _resolve_provider(model: str) -> str | None:
    """Resolve ``model`` to its provider name, or ``None`` if it is absent
    from every known pricing table (T3.6) — the model has no key to check,
    same posture as Dry Run's ``model_price_unresolved`` (never a reason to
    fail this check on its own)."""
    for provider, pricing in _PROVIDER_PRICING:
        if model in pricing:
            return provider
    return None


@dataclass(frozen=True, slots=True)
class MiseEnPlaceSettings:
    """Deployment-level knobs for the Mise en Place hook, built ONCE from
    ``shared.config.settings`` by the assembly layer (``app.lifespan``) —
    mirror ``DryRunSettings``/``RoutingSettings`` (T1.3). Never read
    ``settings`` from inside this module or :class:`MiseEnPlaceService`
    beyond construction.
    """

    #: Per-MCP-server ping timeout for `mcp_tools_reachable` — deliberately
    #: shorter than `infra/mcp/client.py`'s registration-time discovery
    #: timeout (T1.1).
    tool_ping_timeout_s: float
    #: Wall-clock ceiling for ONE check, whichever it is (review P6). The
    #: ping timeout above bounds only `discover_tools`; the namespace lookups
    #: and the Dry Run behind `budget_available` had none, so a slow database
    #: could hang the launch path indefinitely. Per check, not per hook, so a
    #: single stuck check still yields the other three real outcomes.
    check_timeout_s: float
    #: `None` disables `budget_available`'s comparison entirely — the exact
    #: same Sprint-1 safety-net field as `DryRunSettings.budget_cap_usd`
    #: (Story 4.4), reused as-is, never a second threshold.
    budget_cap_usd: Decimal | None
    #: PRESENCE (not the secret itself) of each provider's API key, resolved
    #: ONCE at assembly from `Settings.anthropic_api_key`/`openai_api_key`.
    #: `llm_providers_configured` (T3.6) reads these booleans instead of the
    #: `settings` singleton, keeping the "never read settings beyond
    #: construction" rule intact for a check that genuinely needs to know
    #: whether a key is configured.
    anthropic_api_key_present: bool
    openai_api_key_present: bool
    #: `True` when the process runs on the `MockProvider` fallback (no
    #: provider key configured at all, which `app.lifespan` only permits
    #: outside production). Every run then completes against the mock, so a
    #: missing key predicts no failure and `llm_providers_configured` must
    #: not block — otherwise local/CI development becomes impossible without
    #: real secrets (review IG1).
    mock_llm_provider_active: bool = False


class MiseEnPlaceService:
    """Run the four Mise en Place checks for one workflow launch (AC1).

    Every dependency is a repo/service already resolved via
    ``shared.repositories``/the sibling ``DryRunService`` — no import of
    ``features.tool_hub``/``features.memory_manager`` (`.import-linter`
    Contract 1, préambule point 5 of the story).
    """

    def __init__(
        self,
        *,
        template_tool_repo: AgentTemplateToolRepo,
        tool_server_repo: ToolServerRepo,
        namespace_repo: NamespaceRepo,
        dry_run_service: DryRunService,
        settings: MiseEnPlaceSettings,
    ) -> None:
        self._template_tool_repo = template_tool_repo
        self._tool_server_repo = tool_server_repo
        self._namespace_repo = namespace_repo
        self._dry_run_service = dry_run_service
        self._settings = settings

    async def run_checks(
        self,
        *,
        workflow_id: UUID,
        templates: Mapping[str, AgentTemplate],
        task_input: dict[str, Any],
        tenant_id: UUID | None,
    ) -> MiseEnPlaceReport:
        """Run all four checks in parallel and assemble the report (AC1).

        NEVER raises — neither on a failing check (a ``MiseEnPlaceReport``
        with ``all_passed=False`` is a normal, valid return value) nor on a
        check that breaks outright. ``return_exceptions=True`` is what makes
        the second half of that promise true (T3.2): without it, a
        ``NotFoundError`` from an orphaned ``server_id``, a DB error in the
        namespace lookup, or anything ``DryRunService.dry_run()`` raises
        would escape as the endpoint's HTTP error — turning a guard-rail
        into a 404/500 with no report at all, and no ``force`` escape hatch
        for the caller. It also guarantees all four coroutines are awaited
        to completion, instead of leaving three of them running with open
        sessions and spawned subprocesses after the first one raised.

        A check that could not be *evaluated* is reported as
        ``passed=False``, not ``passed=True``: an unverifiable prerequisite
        must fail closed (``force=true`` remains the deliberate way through),
        the opposite of ``budget_available``'s "never block on an
        uncertainty" — which concerns a check that DID run and resolved no
        price.
        """
        # No `dag` parameter (review P14). T3.2 specified one, but every
        # check reads `templates` — which `_load_templates` already resolved
        # for EVERY node declared in the DAG (mirror `agents_involved`,
        # Story 4.4), so no check needs to re-walk edges. Keeping it meant
        # `start_run` paid a full `_dag_from_stored()` reconstruction per
        # launch, and gained a failure mode (a malformed stored DAG raising)
        # for a value that was immediately discarded.
        outcomes = await asyncio.gather(
            self._bounded(self._check_tools(templates, tenant_id=tenant_id)),
            self._bounded(self._check_namespaces(templates, tenant_id=tenant_id)),
            self._bounded(self._check_budget(workflow_id, task_input, tenant_id=tenant_id)),
            self._bounded(self._check_llm_providers(templates)),
            return_exceptions=True,
        )
        return build_report(
            tuple(
                self._coerce_outcome(code, outcome)
                for code, outcome in zip(CHECK_CODES, outcomes, strict=True)
            )
        )

    async def _bounded(self, check: Coroutine[Any, Any, CheckResult]) -> CheckResult:
        """Run one check under :attr:`MiseEnPlaceSettings.check_timeout_s`
        (review P6). A `TimeoutError` is an ordinary `Exception`, so
        :meth:`_coerce_outcome` turns it into that check's own
        ``passed=False`` entry — the hook stays bounded without any check
        being silently skipped."""
        async with asyncio.timeout(self._settings.check_timeout_s):
            return await check

    def _coerce_outcome(self, code: CheckCode, outcome: CheckResult | BaseException) -> CheckResult:
        """A check's return value, or a ``passed=False`` stand-in when it
        raised instead of returning one (T3.2)."""
        if isinstance(outcome, CheckResult):
            return outcome
        _log.warning(
            "workflow_engine.mise_en_place_check_errored",
            check_code=code,
            error_type=type(outcome).__name__,
        )
        return CheckResult(
            code=code,
            passed=False,
            detail=(
                f"Check could not be evaluated ({type(outcome).__name__}) — "
                "treated as a failure rather than silently passed."
            ),
            suggested_action=(
                f"Consulter les logs serveur (check {code}) puis relancer ; "
                "utiliser force=true pour passer outre en connaissance de cause"
            ),
            # A timeout or an infrastructure hiccup may well clear on its
            # own — this is the one failure shape worth retrying (BS5).
            retryable=True,
        )

    async def _check_tools(
        self, templates: Mapping[str, AgentTemplate], *, tenant_id: UUID | None
    ) -> CheckResult:
        """``mcp_tools_reachable`` (AC1) — ping every distinct MCP server
        backing a tool assigned to one of the referenced templates, and
        confirm each still exposes the tools actually assigned.

        The tool-level half EXTENDS AC1, which enumerated only connection
        failures (review BS3): a server that answers discovery but has
        dropped the tool a template depends on breaks the same promise as a
        server that is down, and the check's own name claims to cover it.
        Note that workflow runs do not invoke tools yet
        (``engine/agent_node`` anti-scope), so both halves of this check are
        anticipatory by AC1's deliberate design.

        Opens its OWN session (T3.3) rather than sharing one across the
        four checks — ``run_checks`` awaits all four concurrently via
        ``asyncio.gather``, and a single ``AsyncSession`` is not safe to
        drive from more than one coroutine at a time.
        """
        async with self._template_tool_repo.with_tenant(tenant_id) as session:
            servers_by_id: dict[UUID, ToolServer] = {}
            # Which tool names each server is expected to still expose
            # (review BS3) — `tools` is UNIQUE (server_id, name), so a name
            # identifies an assigned tool on its server unambiguously.
            expected_tools: dict[UUID, set[str]] = {}
            for template in templates.values():
                assigned = await self._template_tool_repo.list_by_template_in_session(
                    session, template.id
                )
                for tool, _assigned_at in assigned:
                    if tool.server_id not in servers_by_id:
                        servers_by_id[
                            tool.server_id
                        ] = await self._tool_server_repo.require_by_id_in_session(
                            session, tool.server_id
                        )
                    expected_tools.setdefault(tool.server_id, set()).add(tool.name)

        if not servers_by_id:
            return CheckResult(
                code="mcp_tools_reachable",
                passed=True,
                detail="No MCP tool assigned to any template referenced by this workflow.",
            )

        servers = list(servers_by_id.values())
        # Bounded fan-out (review P12): a `stdio` probe spawns a subprocess,
        # so an unbounded gather let one workflow launch fork as many
        # processes as it has distinct servers.
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)

        async def _probe(server: ToolServer) -> list[Any]:
            async with semaphore:
                return await discover_tools(
                    transport=server.transport,  # type: ignore[arg-type]  # Literal narrowed at registration
                    connection_config=server.connection_config,
                    timeout=self._settings.tool_ping_timeout_s,
                )

        raw_results = await asyncio.gather(
            *(_probe(server) for server in servers), return_exceptions=True
        )

        unreachable: list[ToolServer] = []
        missing_tools: list[str] = []
        for server, result in zip(servers, raw_results, strict=True):
            # `BaseException`, not `Exception`: `discover_tools` re-raises the
            # `BaseExceptionGroup` it caught when the deadline did not win the
            # race, and such a group carrying a `BaseException` member (a
            # `CancelledError` from anyio teardown, say) is NOT an `Exception`
            # — narrowing to `Exception` would silently file a failed probe
            # under "reachable", the one direction this check must never fail
            # in (review P4).
            if isinstance(result, BaseException):
                # Catch-all, mirror `ToolHubService.connect_server`'s
                # translation posture (`features/tool_hub/service.py`
                # ~184-260, MIRRORED not imported — Contract 1): timeout,
                # malformed connection_config, subprocess spawn failure, or
                # any other MCP/SDK error all count as "unreachable" here —
                # never an exception escaping this check.
                _log.warning(
                    "workflow_engine.mise_en_place_mcp_discovery_failed",
                    server_id=str(server.id),
                    server_name=server.name,
                    error_type=type(result).__name__,
                )
                unreachable.append(server)
                continue
            # Reachable — but does it still expose the tools assigned to
            # this workflow's templates? The probe's RESULT used to be
            # discarded entirely, so a server that had dropped the very tool
            # a template depends on (or exposed nothing at all) passed a
            # check named `mcp_tools_reachable` (review BS3).
            discovered = {getattr(info, "name", None) for info in result}
            for name in sorted(expected_tools.get(server.id, set()) - discovered):
                missing_tools.append(f"{server.name}/{name}")

        if unreachable:
            names = ", ".join(_label_servers(unreachable))
            detail = f"MCP server(s) unreachable: {names}"
            if missing_tools:
                # Both failures at once: report both, so one round-trip
                # tells the operator everything that is wrong.
                detail += f". Tool(s) missing from a reachable server: {', '.join(missing_tools)}"
            return CheckResult(
                code="mcp_tools_reachable",
                passed=False,
                detail=detail,
                # Singular keeps AC2's exact wording; plural exists because
                # interpolating a list into it produced "le serveur MCP a, b
                # est démarré" (review P15).
                suggested_action=(
                    f"Vérifier que les serveurs MCP {names} sont démarrés et joignables"
                    if len(unreachable) > 1
                    else f"Vérifier que le serveur MCP {names} est démarré et joignable"
                ),
                # A server being down is transient; a tool having vanished
                # is not. When both happened, the permanent half decides —
                # retrying cannot fix the missing tool (BS5).
                retryable=not missing_tools,
            )
        if missing_tools:
            joined = ", ".join(missing_tools)
            return CheckResult(
                code="mcp_tools_reachable",
                passed=False,
                detail=f"Tool(s) no longer exposed by their MCP server: {joined}",
                suggested_action=(
                    f"Re-synchroniser les outils du serveur MCP (outils absents : {joined}) "
                    "ou corriger l'assignation du template"
                ),
            )
        return CheckResult(
            code="mcp_tools_reachable",
            passed=True,
            detail="Every assigned MCP server responded, and still exposes its assigned tools.",
        )

    async def _check_namespaces(
        self, templates: Mapping[str, AgentTemplate], *, tenant_id: UUID | None
    ) -> CheckResult:
        """``memory_namespaces_accessible`` (AC1) — every non-null
        ``push_memory.namespace`` referenced by the templates must exist.

        MVP scope (préambule point 6, Anti-scope): existence only, not
        department access control (``MemoryManagerService._check_department_access``)
        — same class of debt as the `tenant_id=None` propagated by 4.1-4.4.
        """
        names: dict[str, None] = {}
        for template in templates.values():
            push_memory = _config_of(template).get("push_memory")
            if not isinstance(push_memory, Mapping):
                continue
            namespace = push_memory.get("namespace")
            if not (isinstance(namespace, str) and namespace):
                continue
            # Mirror the RUNTIME gate exactly (`PlaygroundService`'s Push
            # Memory injection): a Contrôleur that never opted in never opens
            # its namespace, so blocking a launch on that namespace's
            # existence is a pure false positive — the run would not have
            # touched it (review BS1).
            #
            # `is not True`, not `not optin`: a stored `optin: "false"` is
            # truthy under `bool(...)`, and Story 3.5's own review (P6) fixed
            # that exact inversion at the runtime site. The two predicates
            # must stay identical or this check drifts from what runs.
            if template.archetype == _CONTROLEUR_ARCHETYPE and push_memory.get("optin") is not True:
                continue
            names.setdefault(namespace, None)

        if not names:
            return CheckResult(
                code="memory_namespaces_accessible",
                passed=True,
                detail="No memory namespace referenced by this workflow.",
            )

        missing: list[str] = []
        for name in names:
            try:
                await self._namespace_repo.require_by_name(name, tenant_id=tenant_id)
            except NotFoundError:
                missing.append(name)

        if missing:
            joined = ", ".join(sorted(missing))
            return CheckResult(
                code="memory_namespaces_accessible",
                passed=False,
                detail=f"Namespace(s) not found: {joined}",
                # Singular is AC2's exact wording; plural avoids "Créer le
                # namespace a, b" (review P15).
                suggested_action=(
                    f"Créer les namespaces {joined} ou corriger la config push_memory des templates"
                    if len(missing) > 1
                    else f"Créer le namespace {joined} ou corriger la config "
                    "push_memory du template"
                ),
            )
        return CheckResult(
            code="memory_namespaces_accessible",
            passed=True,
            detail="Every referenced namespace exists.",
        )

    async def _check_budget(
        self, workflow_id: UUID, task_input: dict[str, Any], *, tenant_id: UUID | None
    ) -> CheckResult:
        """``budget_available`` (AC1) — reuse ``DryRunService.dry_run()``
        (Story 4.4, zero real LLM call) for ``cost_estimate_usd`` and compare
        against the SAME global cap Dry Run itself warns against (Dev Notes
        § Budget : avertissement vs blocage).

        Never blocks on an uncertainty: no cap configured, or no price
        resolved for the estimate, both pass — this check only blocks on a
        PROVEN overrun.
        """
        cap = self._settings.budget_cap_usd
        if cap is None:
            # Short-circuit BEFORE the Dry Run, not after (review P5). With
            # no cap there is nothing to compare against, so the estimate is
            # discarded — and `AGENTIVE_DRY_RUN_BUDGET_CAP_USD` ships
            # commented out, making "no cap" the DEFAULT deployment. Running
            # a full Dry Run (workflow + templates + run history) on every
            # single launch to throw the answer away was pure waste.
            return CheckResult(
                code="budget_available",
                passed=True,
                detail="No budget cap configured — nothing to compare against, check passes.",
            )

        response = await self._dry_run_service.dry_run(
            workflow_id=workflow_id, task_input=task_input, tenant_id=tenant_id
        )
        cost = (
            Decimal(response.cost_estimate_usd) if response.cost_estimate_usd is not None else None
        )
        if cost is None:
            return CheckResult(
                code="budget_available",
                passed=True,
                detail="No cost estimate resolved — never block on an uncertainty, check passes.",
            )
        if cost <= cap:
            return CheckResult(
                code="budget_available",
                passed=True,
                detail=f"Estimated cost {cost} USD is within the configured cap {cap} USD.",
            )
        return CheckResult(
            code="budget_available",
            passed=False,
            detail=f"Estimated cost {cost} USD exceeds the configured cap {cap} USD.",
            suggested_action=(
                "Ajuster AGENTIVE_DRY_RUN_BUDGET_CAP_USD ou réduire le scope du workflow"
            ),
        )

    async def _check_llm_providers(self, templates: Mapping[str, AgentTemplate]) -> CheckResult:
        """``llm_providers_configured`` (AC1) — every LLM provider resolved
        from the templates' ``llm_model`` must have an API key configured.

        MVP scope (Dev Notes § Divergences assumées): PRESENCE of a
        configured key, not a real network health-check — no such mechanism
        exists anywhere in this repo, and building one would either cost a
        real LLM call (contradicting Dry Run's zero-cost guarantee) or
        require health infrastructure absent elsewhere.
        """
        if self._settings.mock_llm_provider_active:
            # No key is configured anywhere, so `app.lifespan` wired the
            # `MockProvider` and every node of this run will complete
            # against it. There is no provider to have a key FOR, and the
            # run cannot fail for a missing one — the condition this check
            # exists to predict simply does not arise (review IG1).
            # Deliberately loud in `detail`: passing must never read as
            # "a real provider was verified".
            return CheckResult(
                code="llm_providers_configured",
                passed=True,
                detail=(
                    "No LLM provider key is configured — this process runs on the mock "
                    "provider (dev/test only; production refuses to boot this way), so "
                    "no run can fail for a missing key."
                ),
            )

        models: dict[str, None] = {}
        for template in templates.values():
            raw_model = _config_of(template).get("llm_model")
            # A template WITHOUT an explicit `llm_model` is not "nothing to
            # check": `agent_node.execute_agent_node` and `dry_run` both run
            # it on `_DEFAULT_LLM_MODEL` (`config.get("llm_model") or
            # DEFAULT`). Skipping it here cleared the gate for the single
            # most common template shape — a run on Anthropic reported as
            # "every provider configured" with no Anthropic key at all
            # (review P2).
            model = raw_model if isinstance(raw_model, str) and raw_model else _DEFAULT_LLM_MODEL
            models.setdefault(model, None)

        resolved_providers: dict[str, None] = {}
        missing_providers: dict[str, None] = {}
        for model in models:
            provider = _resolve_provider(model)
            if provider is None:
                # Unresolved model — no pricing-table entry, hence no known
                # provider to check a key for (mirror Dry Run's
                # `model_price_unresolved`, never a reason to fail here).
                continue
            resolved_providers.setdefault(provider, None)
            if not self._provider_key_present(provider):
                missing_providers.setdefault(provider, None)

        if missing_providers:
            joined = ", ".join(sorted(missing_providers))
            return CheckResult(
                code="llm_providers_configured",
                passed=False,
                detail=f"Missing API key for provider(s): {joined}",
                # Singular is AC2's exact wording (review P15).
                suggested_action=(
                    f"Configurer les clés API manquantes : {joined}"
                    if len(missing_providers) > 1
                    else f"Configurer la clé API {joined} manquante"
                ),
            )
        if not resolved_providers:
            # Passing, but say WHY: "every resolved provider has a key" read
            # as a positive verification when in fact nothing was verified —
            # every model was absent from both pricing tables (review P18).
            return CheckResult(
                code="llm_providers_configured",
                passed=True,
                detail=(
                    "No LLM provider could be resolved from the referenced templates "
                    "(no model matched a known pricing table) — nothing to verify."
                ),
            )
        return CheckResult(
            code="llm_providers_configured",
            passed=True,
            detail=(
                f"Every resolved LLM provider has a configured API key: "
                f"{', '.join(sorted(resolved_providers))}."
            ),
        )

    def _provider_key_present(self, provider: str) -> bool:
        if provider == "anthropic":
            return self._settings.anthropic_api_key_present
        if provider == "openai":
            return self._settings.openai_api_key_present
        return (
            False  # pragma: no cover — defensive, `_resolve_provider` only ever returns these two
        )


__all__ = ["MiseEnPlaceService", "MiseEnPlaceSettings"]
