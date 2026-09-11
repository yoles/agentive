"""Unit tests — Dry Run predictive estimation, pure domain core (Story 4.4 T2.7)."""

from __future__ import annotations

from uuid import uuid4

from agentive_backend.features.workflow_engine.domain.dry_run import (
    END_SENTINEL,
    average_node_tokens,
    classify_decision_points,
    compute_probable_path,
    find_root_nodes,
)
from agentive_backend.features.workflow_engine.domain.value_objects import (
    WorkflowDag,
    WorkflowEdge,
    WorkflowNode,
)


def _node(node_id: str) -> WorkflowNode:
    return WorkflowNode(node_id=node_id, agent_template_id=uuid4())


# ─── Parity of the hardcoded constants ─────────────────────────────────


def test_end_sentinel_matches_langgraphs_real_end_constant() -> None:
    """`domain/dry_run.py` hardcodes `END_SENTINEL = "__end__"` rather than
    importing LangGraph, so that `domain/` stays framework-free — and its
    docstring justifies the duplication by pointing at "a parity test [that]
    lives in the test suite". Review fix P11: that test did not exist, so a
    LangGraph rename would have silently broken END detection in the Dry
    Run while every test stayed green. The import belongs here, in a test
    file, exactly as that docstring says.
    """
    from langgraph.graph import END

    assert END_SENTINEL == END


def test_reserved_node_id_pattern_rejects_the_end_sentinel() -> None:
    """The other half of the hardcoding rationale: `END_SENTINEL` is safe to
    hardcode only because no real node id can ever collide with it."""
    from agentive_backend.features.workflow_engine.schemas import _RESERVED_NODE_ID_RE

    assert _RESERVED_NODE_ID_RE.match(END_SENTINEL) is not None


# ─── find_root_nodes ───────────────────────────────────────────────────


def test_find_root_nodes_returns_node_with_no_incoming_edge() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="b"),),
    )
    assert find_root_nodes(dag) == ("a",)


def test_find_root_nodes_returns_multiple_roots_in_declaration_order() -> None:
    dag = WorkflowDag(
        nodes=(_node("b"), _node("a"), _node("c")),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="c"),),
    )
    assert find_root_nodes(dag) == ("b", "a")


def test_find_root_nodes_returns_single_isolated_node() -> None:
    dag = WorkflowDag(nodes=(_node("a"),), edges=())
    assert find_root_nodes(dag) == ("a",)


# ─── classify_decision_points ──────────────────────────────────────────


def test_classify_decision_points_empty_when_all_edges_unconditional() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="b"),),
    )
    assert classify_decision_points(dag) == frozenset()


def test_classify_decision_points_flags_node_with_conditional_edge() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b", condition="output.status == 'ok'"),
            WorkflowEdge(from_node_id="a", to_node_id="c"),
        ),
    )
    assert classify_decision_points(dag) == frozenset({"a"})


# ─── compute_probable_path — structural (no decision points) ──────────


def test_compute_probable_path_follows_simple_linear_chain() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b"),
            WorkflowEdge(from_node_id="b", to_node_id="c"),
        ),
    )
    result = compute_probable_path(dag, history={})
    assert result.probable_path == ("a", "b", "c")
    assert result.agents_involved == ("a", "b", "c")
    assert result.uncertain_decision_points == ()


def test_compute_probable_path_unconditional_fan_out_follows_first_declared() -> None:
    """All-unconditional fan-out: not a decision, so no uncertainty — but
    EVERY branch still joins `agents_involved` (it will really execute)."""
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b"),
            WorkflowEdge(from_node_id="a", to_node_id="c"),
        ),
    )
    result = compute_probable_path(dag, history={})
    assert result.probable_path == ("a", "b")
    assert set(result.agents_involved) == {"a", "b", "c"}
    assert result.uncertain_decision_points == ()


def test_compute_probable_path_single_isolated_node_is_its_own_path() -> None:
    dag = WorkflowDag(nodes=(_node("a"),), edges=())
    result = compute_probable_path(dag, history={})
    assert result.probable_path == ("a",)
    assert result.agents_involved == ("a",)


def test_compute_probable_path_multi_root_dag_follows_first_declared_root() -> None:
    dag = WorkflowDag(
        nodes=(_node("root1"), _node("root2")),
        edges=(),
    )
    result = compute_probable_path(dag, history={})
    assert result.probable_path == ("root1",)
    assert set(result.agents_involved) == {"root1", "root2"}


# ─── compute_probable_path — decision points (history-driven) ─────────


def test_compute_probable_path_decision_point_follows_historical_majority() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b", condition="output.status == 'ok'"),
            WorkflowEdge(from_node_id="a", to_node_id="c", condition="output.status == 'ko'"),
        ),
    )
    history = {"a": {("b",): 5, ("c",): 1}}
    result = compute_probable_path(dag, history=history)
    assert result.probable_path == ("a", "b")
    assert result.uncertain_decision_points == ()


def test_compute_probable_path_decision_point_without_history_is_uncertain() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b", condition="output.status == 'ok'"),
            WorkflowEdge(from_node_id="a", to_node_id="c", condition="output.status == 'ko'"),
        ),
    )
    result = compute_probable_path(dag, history={})
    assert result.probable_path == ("a", "b")  # first declared, deterministic repli
    assert result.uncertain_decision_points == ("a",)


def test_compute_probable_path_decision_point_with_tied_history_is_uncertain() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b", condition="output.status == 'ok'"),
            WorkflowEdge(from_node_id="a", to_node_id="c", condition="output.status == 'ko'"),
        ),
    )
    history = {"a": {("b",): 3, ("c",): 3}}
    result = compute_probable_path(dag, history=history)
    assert result.probable_path == ("a", "b")  # first declared, deterministic repli
    assert result.uncertain_decision_points == ("a",)


def test_compute_probable_path_decision_point_recorded_end_terminates_path() -> None:
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="b", condition="output.status == 'ok'"),),
    )
    history = {"a": {(END_SENTINEL,): 4}}
    result = compute_probable_path(dag, history=history)
    assert result.probable_path == ("a",)
    assert result.uncertain_decision_points == ()


def test_compute_probable_path_decision_point_with_stale_history_is_uncertain() -> None:
    """History points at a target no longer declared on the DAG (it changed
    since those runs) — degrades to uncertain rather than routing outside
    the DAG."""
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(WorkflowEdge(from_node_id="a", to_node_id="b", condition="output.status == 'ok'"),),
    )
    history = {"a": {("ghost",): 10}}
    result = compute_probable_path(dag, history=history)
    assert result.probable_path == ("a", "b")
    assert result.uncertain_decision_points == ("a",)


def test_compute_probable_path_when_dag_has_a_cycle_should_stop_at_the_visited_node() -> None:
    """Anti-loop guard: even though the DAG is guaranteed acyclic by
    construction (Story 4.1 AC1), the traversal must terminate rather than
    trust that invariant blindly.

    Regression, review fix P9 — this test used to use `a→b, b→a`, where
    BOTH nodes have an incoming edge: `find_root_nodes` returned `()`, the
    walk never started, and the assertion compared two empty sequences.
    Neither `visited` nor the (now removed) length ceiling was exercised.
    Here `a` is a real root, so the walk runs `a → b → c` and must stop when
    `c` points back at the already-visited `b`.
    """
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b"),
            WorkflowEdge(from_node_id="b", to_node_id="c"),
            WorkflowEdge(from_node_id="c", to_node_id="b"),
        ),
    )

    result = compute_probable_path(dag, history={})

    assert result.probable_path == ("a", "b", "c")
    assert len(result.probable_path) == len(set(result.probable_path))


def test_compute_probable_path_when_chain_is_longer_than_100_should_not_truncate() -> None:
    """Regression, review fix P9 — a `len(probable_path) < 100` ceiling used
    to sit next to `visited`. It could never be the limiting factor for the
    corruption `visited` guards against, and on a >100-node DAG it would
    have truncated the path SILENTLY, returning a partial prediction
    indistinguishable from a complete one."""
    nodes = tuple(_node(f"n{i}") for i in range(150))
    edges = tuple(WorkflowEdge(from_node_id=f"n{i}", to_node_id=f"n{i + 1}") for i in range(149))

    result = compute_probable_path(WorkflowDag(nodes=nodes, edges=edges), history={})

    assert len(result.probable_path) == 150
    assert result.probable_path[-1] == "n149"


def test_compute_probable_path_when_edge_is_dangling_should_not_enter_probable_path() -> None:
    """Regression, review fix P6 — the reachability walk filtered targets
    absent from the DAG, the `probable_path` walk did not. A corrupted DAG
    produced a `probable_path` naming a node present neither in
    `agents_involved` nor in the DAG itself, which every consumer of the
    contract (`on_probable_path`, Epic 6's Dialog) would have keyed on."""
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="ghost"),
            WorkflowEdge(from_node_id="a", to_node_id="b"),
        ),
    )

    result = compute_probable_path(dag, history={})

    assert "ghost" not in result.probable_path
    assert result.probable_path == ("a", "b")
    assert set(result.probable_path) <= set(result.agents_involved)


def test_compute_probable_path_when_a_root_is_declared_twice_should_count_it_once() -> None:
    """Regression, review fix P7 — `seen_involved` deduplicated the root set
    but the BFS queue did not, so a duplicated root was appended to
    `agents_involved` twice and had its tokens AND cost counted twice."""
    dag = WorkflowDag(nodes=(_node("a"), _node("a")), edges=())

    result = compute_probable_path(dag, history={})

    assert result.agents_involved == ("a",)


def test_resolve_decision_when_top_target_is_stale_should_fall_to_valid_runner_up() -> None:
    """Regression, review fix P8 — with `{("ghost",): 10, ("c",): 9}` where
    `ghost` was removed from the DAG, the previous version discarded 9 real
    observations of `c` and guessed the first declared branch (`b`). The
    node stays flagged uncertain (the DAG changed under its own history),
    but the prediction now uses the best evidence that remains."""
    dag = WorkflowDag(
        nodes=(_node("a"), _node("b"), _node("c")),
        edges=(
            WorkflowEdge(from_node_id="a", to_node_id="b", condition="output.x == 1"),
            WorkflowEdge(from_node_id="a", to_node_id="c", condition="output.x == 2"),
        ),
    )
    history = {"a": {("ghost",): 10, ("c",): 9}}

    result = compute_probable_path(dag, history)

    assert result.probable_path == ("a", "c")
    assert result.uncertain_decision_points == ("a",)


# ─── average_node_tokens ────────────────────────────────────────────────


def test_average_node_tokens_when_no_samples_should_report_both_fields_unknown() -> None:
    assert average_node_tokens([]) == (None, None)


def test_average_node_tokens_averages_available_samples() -> None:
    samples = [
        {"input_tokens": 100, "output_tokens": 50},
        {"input_tokens": 200, "output_tokens": 150},
    ]
    assert average_node_tokens(samples) == (150, 100)


def test_average_node_tokens_ignores_none_fields_individually() -> None:
    samples = [
        {"input_tokens": 100, "output_tokens": None},
        {"input_tokens": None, "output_tokens": 50},
    ]
    assert average_node_tokens(samples) == (100, 50)


def test_average_node_tokens_when_one_field_never_usable_should_report_it_unknown() -> None:
    """Regression, review fix P3 — the field with no usable sample must come
    back ``None`` (so the caller applies ITS configured fallback), never
    ``0``. Returning ``0`` priced that half of the node at zero and made a
    fabricated number indistinguishable from a measured one."""
    samples = [
        {"input_tokens": None, "output_tokens": 50},
        {"input_tokens": None, "output_tokens": 70},
    ]
    assert average_node_tokens(samples) == (None, 60)
