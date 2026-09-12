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
from agentive_backend.features.workflow_engine.domain.provider_chain import resolve_provider_chain
from agentive_backend.infra.llm.pricing import KNOWN_PROVIDERS, provider_for_model
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

#: The model a template without an explicit `llm_model` actually runs on —
#: mirror `engine.agent_node.DEFAULT_LLM_MODEL` and `dry_run._DEFAULT_LLM_MODEL`
#: (both `config.get("llm_model") or DEFAULT`). Duplicated rather than
#: imported: `engine/__init__` pulls LangGraph in transitively, and this
#: module sits on the launch path of every run. Keep in sync with those two.
_DEFAULT_LLM_MODEL: Final = "claude-sonnet-4-6"

#: Every provider name this process can possibly register, i.e. the keys
#: `_provider_key_present` knows how to answer for. Needed by Story 4.6's
#: `provider_chain` resolution (T9.1), which must be given the set of
#: AVAILABLE providers — the same intersection `agent_node` performs at
#: runtime, so the check predicts what the run will actually do.
#:
#: Imported, not rebuilt locally: T9.1's whole point is that the pre-flight
#: and the runtime share ONE rule. A private copy here was identical to
#: `pricing`'s by luck, and `provider_for_model`'s own docstring already
#: claimed the two were shared "by construction" while they were not.
_KNOWN_PROVIDERS: Final[tuple[str, ...]] = KNOWN_PROVIDERS

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


#: Resolve a model to the provider that owns it. Aliased, not reimplemented:
#: `engine/agent_node` calls the same function, which is what makes "the
#: pre-flight predicts what the runtime does" true by construction (T9.1)
#: instead of true by coincidence.
_resolve_provider = provider_for_model


@dataclass(frozen=True, slots=True)
class _ProviderScan:
    """What one pass over the templates found, before it is phrased.

    Six buckets rather than a verdict, because they are not
    interchangeable: a missing key is fixed in the environment, an
    incoherent, unreadable or misspelt chain is fixed in the template, and a
    degraded chain is fixed by nobody — it runs, just without its fallback
    leg. Collapsing any two of them is how the check came to tell an
    operator to edit a template whose only problem was an absent API key,
    and — the other direction — to go configure a key for a provider that
    does not exist.
    """

    resolved: dict[str, None]
    missing: dict[str, None]
    incoherent: dict[str, None]
    malformed: dict[str, None]
    #: Declared provider names outside :data:`_KNOWN_PROVIDERS`. Kept apart
    #: from :attr:`missing` because no environment can ever satisfy them.
    unknown: dict[str, None]
    degraded: set[str]


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

    def _scan_templates(
        self, templates: Mapping[str, AgentTemplate], *, available: tuple[str, ...]
    ) -> _ProviderScan:
        """Classify every template into the buckets the verdict is built from.

        Split out of :meth:`_check_llm_providers` to keep both under the
        repo's cyclomatic gate (``pyproject.toml`` is explicit that the
        ceiling is a ratchet: split the function, never raise the number).
        The separation is also the honest one — this decides WHAT is wrong,
        the caller decides how to SAY it.
        """
        resolved_providers: dict[str, None] = {}
        missing_providers: dict[str, None] = {}
        incoherent_models: dict[str, None] = {}
        malformed_templates: dict[str, None] = {}
        unknown_providers: dict[str, None] = {}
        degraded_providers: set[str] = set()
        for node_id, template in templates.items():
            config = _config_of(template)
            # Resolved BEFORE the chain branch because the chain resolution
            # now depends on it: the router hands index 0 the model verbatim,
            # so which provider owns the model decides whether a declared
            # chain is usable at all. Hoisting it is what keeps this check
            # calling `resolve_provider_chain` with the SAME arguments the
            # node passes.
            raw_model = config.get("llm_model")
            # A template WITHOUT an explicit `llm_model` is not "nothing to
            # check": `agent_node.execute_agent_node` and `dry_run` both run
            # it on `_DEFAULT_LLM_MODEL` (`config.get("llm_model") or
            # DEFAULT`). Skipping it here cleared the gate for the single
            # most common template shape — a run on Anthropic reported as
            # "every provider configured" with no Anthropic key at all
            # (review P2).
            model = raw_model if isinstance(raw_model, str) and raw_model else _DEFAULT_LLM_MODEL
            model_provider = _resolve_provider(model)
            # Story 4.6 T9.1 — a template declaring a `provider_chain` will
            # run on THAT chain, not on the provider its `llm_model` implies.
            # Before this story the runtime ignored the chain entirely, so
            # checking `llm_model` alone was accurate; now it is not. A
            # template with `provider_chain: ["openai"]` and
            # `llm_model: "claude-sonnet-4-6"` used to be reported as fine
            # with only an Anthropic key configured — and then failed at the
            # first node. A pre-flight check must predict what the runtime
            # DOES, never what the config LOOKS like (fil rouge of the 4.5
            # review), which is why this calls the very function the node
            # calls (`resolve_provider_chain`) rather than re-deriving it.
            resolution = resolve_provider_chain(
                config, available=available, model_owner=model_provider
            )
            if resolution.malformed:
                # The column held something this code cannot read (a bare
                # string, an empty list, a non-string element), so the whole
                # chain is ignored and the node runs on the process default —
                # against providers the author did not choose. Same class of
                # problem as `incoherent`, and reported separately from a
                # missing key because the remediation is different: fix the
                # template, not the environment. `resolution.malformed` was
                # computed and then dropped on the floor here, so a template
                # carrying `provider_chain: "anthropic"` passed this gate
                # without a word.
                malformed_templates.setdefault(node_id, None)
                continue

            if resolution.configured:
                # Only the providers the run will ACTUALLY use. Feeding this
                # from `configured` listed the dropped ones too, so the
                # success message ("Every resolved LLM provider has a
                # configured API key: anthropic, openai") named a provider
                # with no key at all — the check stated the opposite of what
                # it had just measured.
                for provider in resolution.chain or ():
                    resolved_providers.setdefault(provider, None)
                # `dropped` means "declared but not usable", which lumps two
                # faults with OPPOSITE remediations into one tuple:
                # `resolve_provider_chain` cannot tell them apart because it
                # is given only the AVAILABLE set, and a provider absent from
                # it is either real-but-keyless or not a provider at all.
                # Here we do know: everything outside `_KNOWN_PROVIDERS` is a
                # name this process can never register, whatever the
                # environment. Reported as a missing key, it produced the one
                # verdict an operator cannot act on — "Configurer la clé API
                # mistral manquante", for a setting that does not exist.
                keyless = tuple(name for name in resolution.dropped if name in _KNOWN_PROVIDERS)
                for name in resolution.dropped:
                    if name not in _KNOWN_PROVIDERS:
                        unknown_providers.setdefault(f"{node_id} ({name})", None)
                if keyless:
                    degraded_providers.update(keyless)
                # FAIL only when NOTHING in the declared chain is usable.
                #
                # A partially-available chain is a degradation, not a
                # failure: the runtime intersects and runs on the providers
                # that ARE configured, logging the ones it dropped. Failing
                # there would block a launch that works perfectly well
                # without its fallback leg — the opposite of NFR12's intent.
                #
                # An EMPTY intersection is different in kind: the node falls
                # back to the process default chain, so the run executes
                # against a provider the template's author did not choose.
                # That is the case T9.3 names, and the one this check
                # previously waved through by only ever looking at
                # `llm_model`.
                # `keyless`, not `dropped`: `_resolve_provider` only ever
                # returns a `_KNOWN_PROVIDERS` name, so the two are the same
                # test here — the narrower one just says so out loud.
                if resolution.incoherent and model_provider in keyless:
                    # The model's own provider IS in the chain — it was just
                    # dropped for want of a key. Reporting this as "your
                    # template is incoherent" sent the operator to edit a
                    # template that is perfectly correct, while the real fix
                    # (set the API key) went unmentioned. `dropped` held the
                    # answer and was discarded on this branch.
                    missing_providers.setdefault(model_provider, None)
                elif resolution.incoherent:
                    # Every declared provider is registered, yet NONE of them
                    # serves `llm_model`. No ordering saves this chain, so the
                    # node drops it and runs on the process default — against
                    # a provider the author did not choose. Distinct from a
                    # missing key, and reported as such.
                    incoherent_models.setdefault(f"{node_id} ({model})", None)
                elif resolution.chain is None:
                    # Only the real ones. A chain emptied purely by misspelt
                    # names leaves this empty and is carried by
                    # `unknown_providers` instead.
                    for provider in keyless:
                        missing_providers.setdefault(provider, None)
                continue

            if model_provider is None:
                # Unresolved model — no pricing-table entry, hence no known
                # provider to check a key for (mirror Dry Run's
                # `model_price_unresolved`, never a reason to fail here).
                continue
            resolved_providers.setdefault(model_provider, None)
            if not self._provider_key_present(model_provider):
                missing_providers.setdefault(model_provider, None)

        return _ProviderScan(
            resolved=resolved_providers,
            missing=missing_providers,
            incoherent=incoherent_models,
            malformed=malformed_templates,
            unknown=unknown_providers,
            degraded=degraded_providers,
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

        # Which providers are configured AT ALL in this process — the same
        # set `app.lifespan._build_llm_router` registers, and therefore the
        # same set `agent_node._resolve_chain` will intersect a template's
        # declared chain against at runtime (Story 4.6 T9.1). Computed here
        # so the check and the runtime answer "which providers can this run
        # actually use?" with ONE rule instead of two that drift.
        available = tuple(
            provider for provider in _KNOWN_PROVIDERS if self._provider_key_present(provider)
        )

        scan = self._scan_templates(templates, available=available)
        resolved_providers = scan.resolved
        missing_providers = scan.missing
        incoherent_models = scan.incoherent
        malformed_templates = scan.malformed
        unknown_providers = scan.unknown
        degraded_providers = scan.degraded

        # One verdict carrying EVERY problem found, not the first one. Three
        # separate `return`s meant an instance with both a missing key and a
        # broken template showed only one: the operator fixed it, relaunched,
        # and discovered the next — two round trips for information the check
        # held all along.
        problems: list[str] = []
        actions: list[str] = []
        if missing_providers:
            joined = ", ".join(sorted(missing_providers))
            problems.append(f"Missing API key for provider(s): {joined}")
            # Singular is AC2's exact wording (review P15).
            actions.append(
                f"Configurer les clés API manquantes : {joined}"
                if len(missing_providers) > 1
                else f"Configurer la clé API {joined} manquante"
            )
        if incoherent_models:
            joined = ", ".join(sorted(incoherent_models))
            # Named by NODE, with the model in parentheses. Listing models
            # alone was unactionable the moment two templates shared one:
            # the sentence said "template(s)" and the list held model names.
            problems.append(
                f"Node(s) whose provider_chain serves none of their llm_model: {joined}"
            )
            actions.append(
                "Ajouter le provider du modèle à provider_chain, ou choisir un "
                "llm_model servi par la chaîne déclarée"
            )
        if malformed_templates:
            joined = ", ".join(sorted(malformed_templates))
            problems.append(f"Node(s) with an unreadable provider_chain: {joined}")
            actions.append("Corriger provider_chain (liste de chaînes non vide) ou la retirer")
        if unknown_providers:
            # FAILS even when the rest of the chain is usable, unlike a
            # missing key — and the asymmetry is deliberate. An absent key is
            # a legitimate per-environment state (staging runs without an
            # OpenAI key on purpose); blocking on it would make staging
            # unlaunchable, which is why a partially-available chain passes
            # as `degraded`. A name outside `_KNOWN_PROVIDERS` is a typo in
            # no environment's interest: the template claims a fallback leg
            # that has never existed and never will, and the operator
            # believes they are covered. `force` remains the escape hatch.
            joined = ", ".join(sorted(unknown_providers))
            problems.append(f"Node(s) declaring an unknown provider name: {joined}")
            actions.append(
                "Corriger provider_chain — providers supportés : "
                f"{', '.join(sorted(_KNOWN_PROVIDERS))}"
            )
        if problems:
            return CheckResult(
                code="llm_providers_configured",
                passed=False,
                detail="; ".join(problems),
                suggested_action=" / ".join(actions),
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
        detail = (
            f"Every resolved LLM provider has a configured API key: "
            f"{', '.join(sorted(resolved_providers))}."
        )
        if degraded_providers:
            # Passing, but not silently: a partially-available chain runs
            # WITHOUT its fallback leg. That is a degradation the launch
            # survives (hence `passed=True`, decision T9) — the operator
            # still deserves to know the run has no second provider to fall
            # back on, which the previous message actively concealed by
            # listing the dropped providers as if they were configured.
            detail += (
                " Degraded: no API key for "
                f"{', '.join(sorted(degraded_providers))}, dropped from the declared chain(s)."
            )
        return CheckResult(code="llm_providers_configured", passed=True, detail=detail)

    def _provider_key_present(self, provider: str) -> bool:
        if provider == "anthropic":
            return self._settings.anthropic_api_key_present
        if provider == "openai":
            return self._settings.openai_api_key_present
        return (
            False  # pragma: no cover — defensive, `_resolve_provider` only ever returns these two
        )


__all__ = ["MiseEnPlaceService", "MiseEnPlaceSettings"]
