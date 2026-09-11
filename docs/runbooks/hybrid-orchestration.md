# Hybrid Orchestration Runbook (Story 4.3)

Routing for a workflow node with at least one conditional outgoing edge
goes through three layers, in strict order: the DSL (Story 4.1/4.2), then
declarative rules, then a lightweight LLM escalation. This runbook covers
where each layer lives, how to tune it, and how to read the resulting
metrics.

## Where the rules live

- **Catalog**: `backend/src/agentive_backend/features/workflow_engine/templates/routing-rules.yaml`,
  loaded once at boot by `app.lifespan` into `app.state.routing_rules`
  (`features/workflow_engine/routing_catalog.py::load_routing_rules`). A
  malformed catalog fails BOOT, not a request.
- **Threshold and escalation knobs** live in `Settings`
  (`shared/config.py`), never in the YAML — see `.env.example` for the four
  `AGENTIVE_ROUTING_*` variables and their defaults.
- Adding a rule is a YAML edit only — no code change, no redeploy of
  anything but the catalog file. Declaration order matters: on an equal
  score, the FIRST declared rule wins (`domain/routing_rules.py::evaluate_rules`).

## Adjusting the confidence threshold

```bash
# .env or environment
AGENTIVE_ROUTING_CONFIDENCE_THRESHOLD=0.8   # default; comparison is >=
```

Lowering it makes more decisions resolve deterministically via rules
(cheaper, less accurate); raising it escalates more often to the LLM
(costlier, more adaptive). `1.0` means only a rule with `base_confidence:
1.0` and no active penalty ever decides without the LLM.

## Reading `GET /api/v1/workflows/{workflow_id}/routing-stats`

```json
{
  "workflow_id": "...",
  "runs_counted": 12,
  "deterministic": 40,
  "llm_escalated": 6,
  "deterministic_pct": 87.0
}
```

`deterministic_pct` is `null` when the workflow has never reached a
routing decision POINT (a node with at least one conditional outgoing
edge) — a purely sequential workflow, or one with zero runs. That is a
legitimate state, not a bug.

`runs_counted` counts **every** run of the workflow whatever its status —
pre-4.3 runs, runs still `running`, failed runs. It is NOT the denominator
of `deterministic_pct`, which is `deterministic + llm_escalated` and counts
DECISIONS. A workflow can legitimately report many runs and zero decisions.

## What an escalation costs

Each run's `workflow_runs.metrics` carries a `routing` block:

```json
"routing": {
  "deterministic": 4,
  "llm_escalated": 1,
  "tokens": {"input": 210, "output": 38},
  "cost_usd": "0.00042"
}
```

These tokens and this cost are **included** in the run's `total_tokens` /
`total_cost_usd`, and kept separately here so routing spend stays
comparable against node spend — the claim "routing deterministically is
cheaper" is only falsifiable if the two can be told apart. A run with no
escalation reports `cost_usd: null` and zero tokens.

## Metrics

| Metric | Meaning |
|---|---|
| `agentive_workflow_engine_routing_decisions_total{mode,source}` | Decisions taken. Incremented **once per run, at run end**, from the run's final state — so it agrees with `metrics["routing"]` and `/routing-stats` even for a resumed run. It does NOT move live during a run. |
| `agentive_workflow_engine_routing_escalation_seconds` | Latency of an escalation **attempt**, successful or not — a timeout is exactly the one worth seeing. |
| `agentive_workflow_engine_routing_escalation_failures_total{reason}` | Escalations that produced no decision. `reason` ∈ `llm_error` (provider chain exhausted or timed out), `unparsable` (response was not usable JSON), `target_rejected` (the model named a node outside the declared candidates). A non-zero rate here means runs are dying — every one of these fails its run. |

## When an escalation fails

The run goes to `status="error"`; there is no silent degradation to `END`
(a truncated run reporting `completed` is worse than one that fails
loudly). The node whose decision failed had already executed and paid for
its own LLM call: its output and its token/cost metrics **are** preserved
in the run's checkpoint and totals, and its `node_statuses` entry reads
`error` because its step as a whole did not complete. Check
`checkpoint.last_error` for the real cause — it carries the underlying
failure, not a wrapper message.

## What counts as a "routing decision"

Only a node with **at least one conditional outgoing edge** — the same set
LangGraph installs `add_conditional_edges` for. A node whose successors are
all unconditional is structurally determined by the DAG's author and is
never counted, never evaluated against a rule, and never calls an LLM
(Story 4.2 behavior, unchanged).

## Interpreting a rising escalation rate

A climbing `llm_escalated` share, or a sudden burst of
`workflow_engine.workflow_run.routing_escalated` events, usually means
one of:

1. **A node's output shape drifted** — check `checkpoint.routing_decisions[node_id].reason`
   on affected runs (or the SSE `routing_escalated` frame) for what the
   LLM actually decided and why no rule matched.
2. **The catalog is missing a rule** for a case that has become common —
   candidate for a new `routing-rules.yaml` entry.
3. **The threshold was raised** recently — check `AGENTIVE_ROUTING_CONFIDENCE_THRESHOLD`
   deploy history first, before assuming a data-shape regression.

Escalations are NOT a failure signal by themselves — they are the
designed fallback (FR3/FR4). An escalation that raises
`RoutingEscalationError` (LLM unreachable, or its response names a target
outside the DAG's declared successors) DOES fail the run explicitly
(`status="error"`, `checkpoint.last_error`) — by design, never a silent
`END` (see Dev Notes § Dégradation silencieuse vs échec explicite in
Story 4.3).

## Tracing one decision

- `checkpoint.routing_decisions[node_id]` on the `workflow_runs` row
  (`{mode, source, targets, confidence, rule_id, reason, llm_model, llm_latency_ms}`).
- The `workflow_engine.workflow_run.routing_escalated` event (outbox +
  SSE), for escalations only — carries `candidates`/`context` for
  debugging, deliberately WITHOUT the node's own output (payload-size
  discipline for the bounded SSE queue).
- Prometheus: `agentive_workflow_engine_routing_decisions_total{mode,source}`
  and `agentive_workflow_engine_routing_escalation_seconds` (labels are
  bounded by design — never `workflow_id`/`run_id`/`node_id`).
