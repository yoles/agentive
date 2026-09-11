# Dry Run Predictif Runbook (Story 4.4)

`POST /api/v1/workflows/{workflow_id}/dry-run` predicts a workflow run's
probable path, the agents it would mobilize, a token/cost estimate, and
identified risks — WITHOUT executing anything and WITHOUT any real LLM
call. This runbook covers where the estimation logic lives, how to tune
it, and how to read the response.

## Where the logic lives

- **Pure algorithm** (structural + historical path prediction):
  `backend/src/agentive_backend/features/workflow_engine/domain/dry_run.py`
  — `compute_probable_path`. No I/O, no DB, no LLM.
- **Orchestration** (DB access, provider/pricing resolution):
  `backend/src/agentive_backend/features/workflow_engine/dry_run.py` —
  `DryRunService`. Deliberately constructed WITHOUT an `LLMRouter` — it
  cannot call an LLM even by implementation error.
- **Pricing tables**: `infra/llm/pricing.py` (`ANTHROPIC_MODEL_PRICING`/
  `OPENAI_MODEL_PRICING`) — single source of truth, also re-exported as
  `MODEL_PRICING` by `infra/llm/anthropic_adapter.py`/`openai_adapter.py`
  for their own `_compute_cost`.
- **Settings** live in `Settings` (`shared/config.py`), never hardcoded —
  see `.env.example` for the four `AGENTIVE_DRY_RUN_*` variables.

## Tuning the estimation

```bash
# .env or environment
AGENTIVE_DRY_RUN_HISTORY_LIMIT=20            # past runs read per workflow
AGENTIVE_DRY_RUN_FALLBACK_INPUT_TOKENS=500   # used when a node has NO history
AGENTIVE_DRY_RUN_FALLBACK_OUTPUT_TOKENS=500
AGENTIVE_DRY_RUN_BUDGET_CAP_USD=50.0         # optional — unset disables the check
```

`AGENTIVE_DRY_RUN_BUDGET_CAP_USD` is a **single global safety net**, not
Story 9.4's real per-department/per-workflow budget caps (which do not
exist yet in this codebase). When 9.4 ships, it is designed to consume
this same `identified_risks[].code == "budget_cap_exceeded"` contract with
a real per-department/per-workflow threshold instead of this one setting.

## Why `probable_path` can differ from what actually runs

A node whose every outgoing edge is unconditional is structurally
determined (Story 4.3's own definition of "not a decision point") — the
Dry Run is certain about it. A node with at least one conditional
outgoing edge is a real decision point, and Dry Run **cannot** evaluate
its condition (that needs the node's real output, which does not exist
pre-execution). Instead it looks at **history**: the most frequent target
this workflow's past runs actually took at that node
(`WorkflowRun.checkpoint["routing_decisions"]`, Story 4.3). No history, or
a tie, degrades to the first declared branch and flags the node with
`identified_risks[].code == "routing_decision_uncertain"`. **Every**
reachable decision point is evaluated this way, not only those the
representative path crosses: a node off the path is still billed, so its
uncertainty still matters.

Only **completed** runs vote in that majority. Token/cost metrics read runs
of every status (a run that failed after a node still paid for that node —
valid measurement), but a branch taken by fifteen crashed runs is not the
branch a healthy run will take, and a still-`running` row could otherwise
flip the prediction between two Dry Runs of an unchanged workflow.

When the winning historical decision named **several** targets, the engine
really would fan out there and a single sequence cannot show it — the node
is reported with `routing_decision_multi_target`. That is not uncertainty:
the history is unambiguous, it just does not fit in a list.

`agents_involved` is intentionally a SUPERSET of `probable_path`: every
node structurally reachable from any root joins it (including every
branch of an unconditional fan-out, and every decision point's declared
targets), because a node that might run must be priced — excluding it
would understate cost, the opposite of what a Dry Run is for.

## Reading the response

```json
{
  "workflow_id": "...",
  "probable_path": ["a", "b"],
  "agents_involved": [
    {"node_id": "a", "agent_template_id": "...", "on_probable_path": true},
    {"node_id": "b", "agent_template_id": "...", "on_probable_path": true},
    {"node_id": "c", "agent_template_id": "...", "on_probable_path": false}
  ],
  "token_estimate_per_provider": {
    "anthropic": {"input_tokens": 620, "output_tokens": 210, "cost_usd": "0.005160"}
  },
  "cost_estimate_usd": "0.005160",
  "probable_path_cost_usd": "0.003440",
  "identified_risks": [
    {"code": "routing_decision_uncertain", "node_id": "a", "detail": "..."},
    {"code": "budget_cap_exceeded", "node_id": null, "detail": "...",
     "estimated_usd": "0.005160", "threshold_usd": "0.001000"}
  ]
}
```

- **Two cost figures, on purpose.** `cost_estimate_usd` is an UPPER BOUND:
  it bills every node in `agents_involved`, including branches that are
  mutually exclusive at runtime — a 5-way exclusive decision bills all five
  while a real run pays one. `probable_path_cost_usd` is the EXPECTED cost:
  only the nodes on `probable_path`. Show the expected figure to a human
  deciding `[Lancer]`/`[Annuler]`; use the bound when you need a worst case.
- Either is `null` when NO node resolved a price (model absent from every
  pricing table) — never a fabricated `"0"`. A `null` total means "cannot be
  priced with current data", not "this workflow is free."
- `identified_risks[].code` is a CLOSED set of six:

  | code | meaning |
  |---|---|
  | `routing_decision_uncertain` | A decision point has no usable historical majority — the path guesses there. |
  | `routing_decision_multi_target` | History says this decision fans out to several targets; `probable_path` shows only one of them. |
  | `no_execution_history` | The workflow has never run: **every** figure is heuristic. |
  | `node_estimate_from_fallback` | THIS node has no usable history even though the workflow has run (added since, or only reached on a rare branch). |
  | `model_price_unresolved` | This node's model is in no pricing table, so its tokens AND cost are **missing** from the totals — they are a lower bound. |
  | `budget_cap_exceeded` | `cost_estimate_usd` is over `AGENTIVE_DRY_RUN_BUDGET_CAP_USD` (AC3). Carries `estimated_usd` and `threshold_usd` as fields — read those, not the prose. |

  There is no "dynamic recruitment" risk code — no such mechanism exists in
  this codebase yet (Growth phase).
- **A response with risks is not a failed estimate**, it is an honest one.
  The three partial-estimate codes exist so a caller can tell a complete
  total from an incomplete one; `budget_cap_exceeded` says in its `detail`
  when the total it compared was itself incomplete.

## Token/cost estimation source

For every node in `agents_involved`, in order:

1. Average of `workflow_runs.metrics.per_node[node_id]` across the last
   `AGENTIVE_DRY_RUN_HISTORY_LIMIT` runs of THIS workflow (any status —
   `completed` and `error` runs both carry valid per-node data for the
   nodes that did execute, Story 4.2 AC4).
2. If no sample exists for that field: the matching configured fallback
   constant — a rough, documented approximation, not a measurement. The
   fallback applies **per field**: a node with measured output tokens and
   no usable input tokens gets the heuristic for the input half, never a
   zero. Whenever any field falls back on a workflow that HAS run, the node
   is reported with `node_estimate_from_fallback`.
3. The resolved token counts are priced against the model actually
   configured on the node's `AgentTemplate` (`config.llm_model`), looked
   up in `infra/llm/pricing.py` — never a historical dollar amount
   averaged directly, since a model's price can change between the
   sampled runs and now.
4. **Routing escalations count too.** When a decision point escalated to an
   LLM in past runs (Story 4.3), those tokens are averaged over every
   recorded decision at that node — a node escalating once in ten runs
   costs a tenth of an escalation per run — and priced against the model
   the escalation actually used. `_aggregate_metrics` already folds this
   into a real run's totals, so leaving it out made the Dry Run understate
   every workflow that escalates.

## No frontend surface yet

`POST /workflows/{id}/dry-run` is backend-only. The Chat "Dry Run preview"
Dialog (`[Lancer]`/`[Annuler]`/`[Ajuster]`, epic AC2/AC3) is Epic 6 (Chat
Interface, backlog Sprint 3) — `features/chat/` does not exist in the
frontend yet. This endpoint's response already carries everything that
Dialog will need.

## Known gap vs Story 9.4

`budget_cap_exceeded` compares the estimate against ONE global,
unscoped setting. It does not know about departments, per-workflow caps,
or actual consumption over a time window — that is Story 9.4's job
(`epics.md` Story 9.4, backlog). Do not interpret the absence of this
risk as "under budget organization-wide" — only as "under the configured
global safety net, if one is configured at all."
