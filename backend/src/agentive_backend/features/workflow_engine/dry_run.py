"""Dry Run predictive estimation — orchestration (Story 4.4 AC1, AC3, T3).

``DryRunService.dry_run()`` is the read-only counterpart to
``WorkflowService.create_workflow``/``WorkflowExecutionService.start_run``:
it loads a persisted workflow but NEVER starts a run (no ``workflow_runs``
row is created), NEVER touches an LLM (no ``LLMRouter`` reaches this class —
see :class:`DryRunService`'s docstring, AC1's structural "zero LLM call"
guarantee), and NEVER writes anything. No Alembic migration accompanies this
story.

The estimate itself combines :func:`~.domain.dry_run.compute_probable_path`
(pure, structural + historical) with history/cost aggregation done here (DB
access — this module is the only place that reads
``WorkflowRun.checkpoint``/``metrics`` for Dry Run purposes) and
provider/pricing resolution against ``infra.llm.*``. See Story 4.4 Dev Notes
§ Algorithme ``probable_path`` for why ``resolve_deterministic_targets``/
``decide_route`` (Story 4.3) cannot be reused: both require a node's real
``own_output``, which only exists after execution.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from agentive_backend.features.workflow_engine.domain.dry_run import (
    NodeMetricSample,
    RoutingHistory,
    average_node_tokens,
    compute_probable_path,
)
from agentive_backend.features.workflow_engine.schemas import (
    DryRunAgentInvolvement,
    DryRunResponse,
    DryRunRisk,
    ProviderTokenEstimate,
)
from agentive_backend.features.workflow_engine.service import _dag_from_stored, _load_templates
from agentive_backend.infra.llm.pricing import (
    ANTHROPIC_MODEL_PRICING,
    OPENAI_MODEL_PRICING,
    ModelPricing,
)
from agentive_backend.shared.exceptions import ValidationError
from agentive_backend.shared.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agentive_backend.infra.db.models import WorkflowRun
    from agentive_backend.shared.repositories import (
        AgentTemplateRepo,
        WorkflowRepo,
        WorkflowRunRepo,
    )

_log = get_logger(__name__)

# Mirror `engine/agent_node.py:38` — NOT imported (that module's `__all__`
# only exports `NODE_TIMEOUT_S`/`execute_agent_node`), same duplication
# precedent already established by `features/playground/service.py:87`.
# Low-stakes: a default never reached in practice, since `create_workflow`
# already requires a valid `llm_model` at template configuration time
# (Story 2.x).
_DEFAULT_LLM_MODEL: Final = "claude-sonnet-4-6"


# `(provider_name, pricing_table)`, checked in order. A model absent from
# BOTH resolves to `None` (T3.5) — logged, never fatal: one unrecognized
# model must not sink the whole estimate. Sourced from `infra.llm.pricing`
# (NOT the adapter modules directly): those import `langchain_*`, which
# `.import-linter` Contract 5 forbids `features/*` from reaching even
# transitively.
#
# Built per call rather than captured in a module-level constant (review
# fix P21): a constant froze the table objects at import time, so
# `monkeypatch.setattr(anthropic_adapter, "MODEL_PRICING", {...})` — the
# natural way to test an alternate price grid — rebound the adapter's name
# only. The adapter and this module then priced the same model differently
# within one process, with nothing to signal the divergence.
def _provider_pricing() -> tuple[tuple[str, ModelPricing], ...]:
    return (("anthropic", ANTHROPIC_MODEL_PRICING), ("openai", OPENAI_MODEL_PRICING))


#: Ceiling above which a historical token count is corruption, not data —
#: see :func:`_usable_token_count`. Two orders of magnitude above the
#: largest real context window, and far below the ~1e22 point where the
#: cost `Decimal` arithmetic overflows its 28-digit context. Mirrors the
#: `le=` bound `shared/config.py` applies to the fallback settings, which
#: reach the same arithmetic by the other path.
_MAX_PLAUSIBLE_TOKENS: Final = 100_000_000


@dataclass(frozen=True, slots=True)
class DryRunSettings:
    """Deployment-level knobs for Dry Run estimation, built ONCE from
    ``shared.config.settings`` by the assembly layer — mirror
    ``RoutingSettings`` (Story 4.3 T5.1). Never read from ``settings``
    inside this module (testability, and the golden rule that already
    forbids ``os.environ`` reads outside ``shared/config.py``)."""

    history_limit: int
    fallback_input_tokens: int
    fallback_output_tokens: int
    #: `None` disables the AC3 check entirely — a Sprint-1 safety net, not
    #: Story 9.4's real per-department/per-workflow budget caps.
    budget_cap_usd: Decimal | None


@dataclass(slots=True)
class _ProviderBucket:
    """Running totals for one resolved provider.

    A typed accumulator rather than a ``dict[str, Any]`` (review fix P20):
    this is the one place where ``int`` token counts and ``Decimal`` money
    share a container, which is exactly where an ``Any``-typed bag turns
    mypy off. ``priced_nodes`` is what makes
    :attr:`ProviderTokenEstimate.cost_usd` a per-GROUP answer (review fix
    P5) instead of the process-wide flag it used to be.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    priced_nodes: int = 0


def _resolve_provider_and_cost(
    model: str, input_tokens: int, output_tokens: int
) -> tuple[str, Decimal] | None:
    """Resolve ``model`` to its provider name and estimated cost, or
    ``None`` if it is absent from every known pricing table (T3.5).

    The return type states the real invariant (review fix P5). It was
    ``tuple[str | None, Decimal | None]``, which advertised a
    ``(provider, None)`` case this function cannot produce — provider
    resolution here IS pricing-table membership. The caller carried a
    matching ``if node_cost is not None`` guard that could never be false,
    and that phantom case was what made the global ``any_cost`` flag look
    like a real computation.

    Mirrors ``_compute_cost`` (``infra/llm/anthropic_adapter.py:128-130``,
    ``infra/llm/openai_adapter.py:121-130``) — duplicated rather than
    imported: both are private (``_``-prefixed) to their own adapter
    module.
    """
    for provider, pricing in _provider_pricing():
        prices = pricing.get(model)
        if prices is None:
            continue
        in_price, out_price = prices
        cost = (
            Decimal(input_tokens) / Decimal(1_000_000) * in_price
            + Decimal(output_tokens) / Decimal(1_000_000) * out_price
        )
        return provider, cost.quantize(Decimal("0.000001"))
    return None


def _iter_recorded_decisions(
    runs: Sequence[WorkflowRun],
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(node_id, decision)`` for every well-formed routing decision
    recorded across ``runs`` (Story 4.3 T9.5's ``checkpoint`` shape).

    Tolerant of legacy/absent/corrupted shapes (pre-4.3 runs never wrote
    this key at all) — mirror ``aggregate_routing_modes``'s defensive
    posture (Story 4.3 T10.1): an unexpected shape counts for nothing,
    never raises.
    """
    for run in runs:
        checkpoint = run.checkpoint if isinstance(run.checkpoint, dict) else {}
        decisions = checkpoint.get("routing_decisions")
        if not isinstance(decisions, dict):
            continue
        for node_id, decision in decisions.items():
            if isinstance(node_id, str) and isinstance(decision, dict):
                yield node_id, decision


def _routing_history_from_runs(runs: Sequence[WorkflowRun]) -> RoutingHistory:
    """Tally historical routing decisions per node.

    ``runs`` must already be filtered to COMPLETED runs (Story 4.4 review,
    IG5). The story justified reading every status for per-node token
    metrics — a run that failed after a node still paid for it, valid data —
    and that reasoning was then silently reused for routing decisions, where
    it does not hold: a branch taken by fifteen runs that crashed is not the
    branch a healthy run is likely to take. A run still `running` could even
    flip the predicted majority between two consecutive Dry Runs of an
    unchanged workflow.
    """
    tally: dict[str, dict[tuple[str, ...], int]] = {}
    for node_id, decision in _iter_recorded_decisions(runs):
        raw_targets = decision.get("targets")
        if isinstance(raw_targets, str):
            raw_targets = [raw_targets]
        if not isinstance(raw_targets, list) or not all(isinstance(t, str) for t in raw_targets):
            continue
        targets = tuple(raw_targets)
        per_node = tally.setdefault(node_id, {})
        per_node[targets] = per_node.get(targets, 0) + 1
    return tally


@dataclass(slots=True)
class _EscalationAverage:
    """Expected LLM-escalation cost of ONE decision point, per run."""

    input_tokens: int
    output_tokens: int
    model: str


def _escalation_averages_from_runs(
    runs: Sequence[WorkflowRun],
) -> dict[str, _EscalationAverage]:
    """Average LLM-escalation usage per decision point (Story 4.4 review,
    IG6).

    Story 4.3's hybrid router escalates to an LLM when no declarative rule
    resolves a decision, and records what that cost on the decision itself
    (``llm_model``/``llm_input_tokens``/``llm_output_tokens``,
    ``domain/routing_rules.py:239-244``). ``_aggregate_metrics`` adds those
    to the run's real totals — that was IG1 of the Story 4.3 review, fixed
    there precisely so routing spend stops being invisible. The Dry Run
    loaded the same rows and threw the fields away, so it compared a
    "nodes only" estimate against a "nodes + escalations" reality and
    understated every workflow that escalates.

    Averaged over EVERY recorded decision at that node, not only the
    escalated ones: a node that escalates once in ten runs costs a tenth of
    an escalation per run, and that expectation is the useful number. Only
    nodes with at least one priced escalation appear in the result.
    """
    totals: dict[str, dict[str, Any]] = {}
    for node_id, decision in _iter_recorded_decisions(runs):
        entry = totals.setdefault(
            node_id, {"decisions": 0, "input": 0, "output": 0, "models": Counter[str]()}
        )
        entry["decisions"] += 1
        input_tokens = _usable_token_count(decision.get("llm_input_tokens"))
        output_tokens = _usable_token_count(decision.get("llm_output_tokens"))
        if input_tokens is None and output_tokens is None:
            # A deterministic decision — it really did cost nothing, and it
            # still counts in the denominator.
            continue
        entry["input"] += input_tokens or 0
        entry["output"] += output_tokens or 0
        model = decision.get("llm_model")
        if isinstance(model, str) and model:
            entry["models"][model] += 1

    averages: dict[str, _EscalationAverage] = {}
    for node_id, entry in totals.items():
        models: Counter[str] = entry["models"]
        if not models or not entry["decisions"]:
            continue
        averages[node_id] = _EscalationAverage(
            input_tokens=round(entry["input"] / entry["decisions"]),
            output_tokens=round(entry["output"] / entry["decisions"]),
            # The model this node actually escalated with most often. Not
            # `settings.routing_escalation_model`: that is today's config,
            # while these tokens were spent under whatever was configured
            # then, and pricing must follow the recorded model.
            model=models.most_common(1)[0][0],
        )
    return averages


def _usable_token_count(value: object) -> int | None:
    """``value`` as a token count, or ``None`` if it is not plausibly one.

    ``metrics.per_node`` is JSONB: nothing at the SQL level constrains what
    a row holds. Three shapes reach here that ``isinstance(value, int)``
    alone waves through, each with a different failure downstream (review
    fix P2):

    * ``True``/``False`` — booleans ARE ``int`` in Python, so a corrupted
      row counted as **1 token**. Worse than the wrong number: a node with
      one such sample looks "historised", which suppresses the configured
      heuristic fallback entirely.
    * a negative count — averaged straight into ``ProviderTokenEstimate``,
      whose ``Field(ge=0)`` then raises a ``pydantic.ValidationError``.
      That is neither an ``AgentiveError`` nor a ``RequestValidationError``,
      so it escapes both handlers as a bare 500 with no RFC 7807 body.
    * an absurd magnitude (``10**30``) — ``Decimal(n) / 1_000_000 * price``
      then overflows the decimal context's 28-digit precision and
      ``.quantize()`` raises ``InvalidOperation``: another bare 500.

    All three are corruption, not data. Treating them as absent is exactly
    this module's stated posture ("an unexpected shape counts for nothing,
    never raises") and lets the per-field fallback take over.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value < 0 or value > _MAX_PLAUSIBLE_TOKENS:
        return None
    return value


def _node_metric_samples_from_runs(
    runs: Sequence[WorkflowRun],
) -> dict[str, list[NodeMetricSample]]:
    """Collect per-node metric samples from past runs' ``metrics.per_node``
    (``_aggregate_metrics``, ``service.py:531-585``). Same defensive
    posture as :func:`_routing_history_from_runs`: an unexpected shape is
    skipped, never raised — see :func:`_usable_token_count` for what
    "unexpected" covers here and why each case matters.
    """
    samples: dict[str, list[NodeMetricSample]] = {}
    for run in runs:
        metrics = run.metrics if isinstance(run.metrics, dict) else {}
        per_node = metrics.get("per_node")
        if not isinstance(per_node, dict):
            continue
        for node_id, metric in per_node.items():
            if not isinstance(node_id, str) or not isinstance(metric, dict):
                continue
            samples.setdefault(node_id, []).append(
                {
                    "input_tokens": _usable_token_count(metric.get("input_tokens")),
                    "output_tokens": _usable_token_count(metric.get("output_tokens")),
                }
            )
    return samples


class DryRunService:
    """Predict a workflow run's probable path, agents mobilized, token/cost
    estimate and identified risks — WITHOUT executing anything (Story 4.4
    AC1).

    Deliberately has NO ``llm_router`` parameter: unlike
    :class:`~.service.WorkflowExecutionService`, this class cannot call an
    LLM even by implementation error — the "zero real LLM call" guarantee
    is structural, not merely tested (contrast Story 4.3 AC1, which had to
    PROVE it with a mock that raises if called).
    """

    def __init__(
        self,
        *,
        workflow_repo: WorkflowRepo,
        workflow_run_repo: WorkflowRunRepo,
        template_repo: AgentTemplateRepo,
        settings: DryRunSettings,
    ) -> None:
        self._workflow_repo = workflow_repo
        self._workflow_run_repo = workflow_run_repo
        self._template_repo = template_repo
        self._settings = settings

    async def dry_run(
        self, *, workflow_id: UUID, task_input: dict[str, Any], tenant_id: UUID | None = None
    ) -> DryRunResponse:
        """Compute the Dry Run estimate for ``workflow_id`` (AC1).

        ``task_input`` is accepted for parity with ``StartRunRequest``/AC1's
        request shape but not otherwise consulted: the estimation is
        structural + historical (Story 4.4 Dev Notes § Algorithme
        ``probable_path``), never a function of this specific input — Dry
        Run makes zero real LLM call, so it can never learn what a
        particular input would have produced.

        Raises:
            NotFoundError: ``workflow_id`` does not reference an existing
                workflow (404 — the URL's primary resource).
            ValidationError: the workflow exists but ``status != "active"``
                (422 — mirror ``WorkflowExecutionService.start_run``,
                ``service.py:655-659``).
        """
        del task_input  # see docstring — accepted for request-shape parity only
        workflow = await self._workflow_repo.require_by_id(workflow_id, tenant_id=tenant_id)
        if workflow.status != "active":
            raise ValidationError(
                detail=f"Workflow '{workflow_id}' is not active (status={workflow.status!r})",
                context={"workflow_id": str(workflow_id), "status": workflow.status},
            )

        dag = _dag_from_stored(workflow.dag)
        templates = await _load_templates(self._template_repo, workflow.dag, tenant_id=tenant_id)
        history_runs = await self._workflow_run_repo.list_by_workflow(
            workflow_id, tenant_id=tenant_id, limit=self._settings.history_limit
        )
        # Two different windows over the same rows, on purpose (IG5):
        # * token/cost metrics read EVERY status — a run that failed after
        #   a node still paid for that node, which is valid measurement
        #   (the story's original justification, unchanged).
        # * routing decisions read COMPLETED runs only — the branch fifteen
        #   crashed runs took is not the branch a healthy run will take,
        #   and a still-`running` row could flip the predicted majority
        #   between two Dry Runs of an unchanged workflow.
        completed_runs = [run for run in history_runs if run.status == "completed"]
        routing_history = _routing_history_from_runs(completed_runs)
        escalation_averages = _escalation_averages_from_runs(completed_runs)
        node_cost_samples = _node_metric_samples_from_runs(history_runs)

        result = compute_probable_path(dag, routing_history)
        probable_path_set = set(result.probable_path)

        agents_involved: list[DryRunAgentInvolvement] = []
        provider_totals: dict[str, _ProviderBucket] = {}
        total_cost: Decimal | None = None
        probable_path_cost: Decimal | None = None
        unpriced: list[tuple[str, str]] = []
        from_fallback: list[str] = []

        def _account(node_id: str, model: str, input_tokens: int, output_tokens: int) -> None:
            """Fold one priced unit (a node, or a node's escalation) into the
            provider totals, the grand total and the probable-path total."""
            nonlocal total_cost, probable_path_cost
            resolved = _resolve_provider_and_cost(model, input_tokens, output_tokens)
            if resolved is None:
                _log.warning(
                    "workflow_engine.dry_run_model_price_unresolved",
                    workflow_id=str(workflow_id),
                    node_id=node_id,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                unpriced.append((node_id, model))
                return
            provider, cost = resolved
            bucket = provider_totals.setdefault(provider, _ProviderBucket())
            bucket.input_tokens += input_tokens
            bucket.output_tokens += output_tokens
            bucket.cost_usd += cost
            bucket.priced_nodes += 1
            total_cost = (total_cost or Decimal("0")) + cost
            if node_id in probable_path_set:
                probable_path_cost = (probable_path_cost or Decimal("0")) + cost

        for node_id in result.agents_involved:
            template = templates[node_id]
            model = str(template.config.get("llm_model") or _DEFAULT_LLM_MODEL)
            # Per-FIELD fallback (review fix P3): a node can have measured
            # output tokens and no usable input tokens. Falling back only
            # when BOTH are missing priced the other field at zero.
            avg_input, avg_output = average_node_tokens(node_cost_samples.get(node_id, []))
            input_tokens = (
                avg_input if avg_input is not None else self._settings.fallback_input_tokens
            )
            output_tokens = (
                avg_output if avg_output is not None else self._settings.fallback_output_tokens
            )
            if history_runs and (avg_input is None or avg_output is None):
                # The workflow HAS run, but not in a way that measured this
                # node (IG2). `no_execution_history` cannot say that — it
                # only ever fires when there are no runs at all.
                from_fallback.append(node_id)

            agents_involved.append(
                DryRunAgentInvolvement(
                    node_id=node_id,
                    agent_template_id=template.id,
                    on_probable_path=node_id in probable_path_set,
                )
            )

            _account(node_id, model, input_tokens, output_tokens)

            # Routing escalations this node paid for historically (IG6).
            escalation = escalation_averages.get(node_id)
            if escalation is not None and (escalation.input_tokens or escalation.output_tokens):
                _account(
                    node_id,
                    escalation.model,
                    escalation.input_tokens,
                    escalation.output_tokens,
                )

        token_estimate_per_provider = {
            provider: ProviderTokenEstimate(
                input_tokens=bucket.input_tokens,
                output_tokens=bucket.output_tokens,
                # Per-GROUP, not a process-wide flag (review fix P5): this
                # is what `ProviderTokenEstimate.cost_usd`'s docstring has
                # always promised. Unreachable today — every node in a
                # bucket is priced by construction — but the moment
                # provider resolution stops implying pricing, this reads
                # the right variable instead of one other providers set.
                cost_usd=str(bucket.cost_usd) if bucket.priced_nodes else None,
            )
            for provider, bucket in provider_totals.items()
        }

        identified_risks: list[DryRunRisk] = [
            DryRunRisk(
                code="routing_decision_uncertain",
                node_id=node_id,
                detail=(
                    f"No historical majority target for decision point {node_id!r} — "
                    "the probable path guesses the first declared branch."
                ),
            )
            for node_id in result.uncertain_decision_points
        ]
        identified_risks.extend(
            DryRunRisk(
                code="routing_decision_multi_target",
                node_id=node_id,
                detail=(
                    f"Decision point {node_id!r} historically routed to several targets at "
                    "once — the engine fans out here, and `probable_path` can only show one "
                    "of those branches. All of them are priced in `cost_estimate_usd`."
                ),
            )
            for node_id in result.multi_target_decision_points
        )
        if not history_runs:
            identified_risks.append(
                DryRunRisk(
                    code="no_execution_history",
                    detail=(
                        "This workflow has never run — every estimate below uses the "
                        "configured fallback heuristic, not measured history."
                    ),
                )
            )
        identified_risks.extend(
            DryRunRisk(
                code="node_estimate_from_fallback",
                node_id=node_id,
                detail=(
                    f"No usable token history for node {node_id!r} even though this workflow "
                    f"has run — its estimate uses the configured heuristic "
                    f"({self._settings.fallback_input_tokens} in / "
                    f"{self._settings.fallback_output_tokens} out), not measurement."
                ),
            )
            for node_id in from_fallback
        )
        identified_risks.extend(
            DryRunRisk(
                code="model_price_unresolved",
                node_id=node_id,
                detail=(
                    f"Model {model!r} (node {node_id!r}) is absent from every known pricing "
                    "table — this node's tokens AND cost are missing from the totals below, "
                    "which are therefore a LOWER bound."
                ),
            )
            for node_id, model in unpriced
        )

        # `None`, never a fabricated `"0"` (Dev Notes § Pièges connus #3):
        # both totals stay `None` until a first node prices, so "costs
        # nothing" and "cannot be priced" stay distinguishable.
        cost_estimate_usd = str(total_cost) if total_cost is not None else None
        cap = self._settings.budget_cap_usd
        if cap is not None and total_cost is not None and total_cost > cap:
            identified_risks.append(
                DryRunRisk(
                    code="budget_cap_exceeded",
                    # Both figures as fields, not only inside the prose
                    # (BS1): AC3 asks the entry to carry them, and Epic 6's
                    # Dialog should not have to parse an English sentence
                    # to show a number it is about to gate a button on.
                    estimated_usd=str(total_cost),
                    threshold_usd=str(cap),
                    detail=(
                        f"Estimated cost {total_cost} USD exceeds the configured cap {cap} USD."
                        + (
                            " That estimate is incomplete — see `model_price_unresolved`."
                            if unpriced
                            else ""
                        )
                    ),
                )
            )

        return DryRunResponse(
            workflow_id=workflow_id,
            probable_path=list(result.probable_path),
            agents_involved=agents_involved,
            token_estimate_per_provider=token_estimate_per_provider,
            cost_estimate_usd=cost_estimate_usd,
            probable_path_cost_usd=(
                str(probable_path_cost) if probable_path_cost is not None else None
            ),
            identified_risks=identified_risks,
        )


__all__ = ["DryRunService", "DryRunSettings"]
