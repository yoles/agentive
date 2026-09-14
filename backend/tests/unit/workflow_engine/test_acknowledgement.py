"""Accusé de réception d'un run — Story 5.1 AC2 (T4).

Le contenu exigé par l'AC est littéral : « Compris. Je mobilise [agents].
ETA ~[X] min. ». Ces tests épinglent la phrase ET les propriétés qui la
rendent honnête — étiquetage de l'estimation, plancher, bornes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from agentive_backend.features.workflow_engine.acknowledgement import (
    MAX_AGENTS_IN_MESSAGE,
    build_acknowledgement,
    node_duration_samples,
)


def _run(status: str = "completed", per_node: Any = None) -> SimpleNamespace:
    return SimpleNamespace(status=status, metrics={"per_node": per_node or {}})


def _ack(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "agent_names": ["Dev Lead"],
        "node_ids": ["dev_lead"],
        "duration_samples": {},
        "default_node_duration_s": 60.0,
    }
    kwargs.update(overrides)
    return build_acknowledgement(**kwargs)


# ─── La phrase ───────────────────────────────────────────────────────


def test_the_message_is_the_sentence_the_ac_asks_for() -> None:
    assert _ack().message == "Compris. Je mobilise Dev Lead. ETA ~1 min."


def test_two_agents_are_joined_in_french() -> None:
    ack = _ack(agent_names=["Dev Lead", "Code Producer"], node_ids=["a", "b"])
    assert "Je mobilise Dev Lead et Code Producer." in ack.message


def test_three_agents_use_commas_then_et() -> None:
    ack = _ack(agent_names=["A", "B", "C"], node_ids=["a"])
    assert "Je mobilise A, B et C." in ack.message


def test_the_same_template_on_two_nodes_is_one_mobilised_agent() -> None:
    """Le répéter donnerait une phrase fausse."""
    ack = _ack(agent_names=["Dev Lead", "Dev Lead"], node_ids=["a", "b"])
    assert ack.agents == ("Dev Lead",)
    assert "Je mobilise Dev Lead." in ack.message


def test_a_long_agent_list_is_truncated_in_the_message_only() -> None:
    """Le champ `agents` reste complet et machine-lisible ; c'est la PHRASE
    qui est bornée, parce qu'un nom de template fait jusqu'à 255 caractères."""
    names = [f"Agent {i}" for i in range(MAX_AGENTS_IN_MESSAGE + 3)]
    ack = _ack(agent_names=names, node_ids=["a"])
    assert len(ack.agents) == len(names)
    assert "3 autres" in ack.message
    assert names[-1] not in ack.message


def test_a_single_hidden_agent_is_singular() -> None:
    names = [f"Agent {i}" for i in range(MAX_AGENTS_IN_MESSAGE + 1)]
    ack = _ack(agent_names=names, node_ids=["a"])
    assert "1 autre." in ack.message


def test_blank_agent_names_are_dropped() -> None:
    ack = _ack(agent_names=["  ", "Dev Lead", ""], node_ids=["a"])
    assert ack.agents == ("Dev Lead",)


# ─── L'ETA, et son honnêteté ─────────────────────────────────────────


def test_without_history_the_eta_is_labelled_heuristic() -> None:
    ack = _ack(node_ids=["a", "b"], duration_samples={})
    assert ack.eta_source == "heuristic"
    assert ack.eta_minutes == 2  # 2 nodes a 60 s


def test_with_full_history_the_eta_is_labelled_history() -> None:
    ack = _ack(node_ids=["a", "b"], duration_samples={"a": [90.0], "b": [30.0]})
    assert ack.eta_source == "history"
    assert ack.eta_minutes == 2


def test_a_single_node_without_history_degrades_the_whole_label() -> None:
    """Leçon de la revue du Dry Run (`node_estimate_from_fallback`) : une
    estimation partiellement mesurée n'est pas une estimation mesurée."""
    ack = _ack(node_ids=["a", "b"], duration_samples={"a": [90.0]})
    assert ack.eta_source == "heuristic"


def test_the_eta_uses_the_median_not_the_mean() -> None:
    """Un run aberrant (un timeout, un provider en carafe) ne doit pas
    déplacer l'estimation rendue à tous les suivants."""
    ack = _ack(node_ids=["a"], duration_samples={"a": [60.0, 60.0, 6000.0]})
    assert ack.eta_minutes == 1


def test_the_eta_never_rounds_down_to_zero() -> None:
    """`~0 min` ne veut rien dire, et ce dépôt refuse le zéro fabriqué."""
    ack = _ack(node_ids=["a"], duration_samples={"a": [0.4]})
    assert ack.eta_minutes == 1


def test_the_eta_rounds_up() -> None:
    ack = _ack(node_ids=["a"], duration_samples={"a": [61.0]})
    assert ack.eta_minutes == 2


def test_an_empty_dag_is_not_reported_as_measured() -> None:
    ack = _ack(agent_names=[], node_ids=[], duration_samples={})
    assert ack.eta_source == "heuristic"
    assert ack.eta_minutes == 1


def test_an_empty_dag_says_so_in_correct_french_without_inventing_work() -> None:
    """Le message rendu était « Compris. Je mobilise aucun agent. ETA ~1 min. »
    — la négation manque, et une minute est annoncée pour un run qui
    n'exécutera rien. Un test l'épinglait comme correct.

    Le module refuse le zéro FABRIQUÉ ; il ne doit pas non plus fabriquer du
    travail. `eta_minutes` reste à 1 parce que le contrat de sortie le borne à
    `>= 1` : ce qui porte l'information, c'est le message et `eta_source`.
    """
    ack = _ack(agent_names=[], node_ids=[], duration_samples={})
    assert "Je mobilise aucun agent" not in ack.message
    assert "aucun node exécutable" in ack.message
    assert ack.agents == ()


def test_a_dag_with_nodes_but_no_named_agent_still_mobilises_nothing_gracefully() -> None:
    """Contrepartie : des nodes existent, mais aucun nom de template
    exploitable. Le repli `_join_agents` reste en défense en profondeur."""
    ack = _ack(agent_names=["", "   "], node_ids=["a"], duration_samples={})
    assert "aucun agent" in ack.message
    assert ack.eta_source == "heuristic"


def test_the_payload_carries_the_four_fields() -> None:
    assert set(_ack().to_payload()) == {"message", "agents", "eta_minutes", "eta_source"}


# ─── Relevé de l'historique : défensif, jamais levé ──────────────────


def test_samples_are_read_from_metrics_per_node_duration_ms() -> None:
    runs = [_run(per_node={"a": {"duration_ms": 1500}})]
    assert node_duration_samples(runs) == {"a": [1.5]}


def test_only_completed_runs_are_sampled() -> None:
    """La durée d'un node d'un run annulé est une durée d'interruption."""
    runs = [
        _run(status="cancelled", per_node={"a": {"duration_ms": 1000}}),
        _run(status="error", per_node={"a": {"duration_ms": 1000}}),
        _run(status="running", per_node={"a": {"duration_ms": 1000}}),
    ]
    assert node_duration_samples(runs) == {}


def test_a_malformed_metrics_blob_is_skipped_not_raised() -> None:
    """`metrics` est du JSONB libre — même posture défensive que
    `dry_run._node_metric_samples_from_runs`. Un accusé de réception ne doit
    pas pouvoir faire échouer un lancement de run."""
    runs = [
        SimpleNamespace(status="completed", metrics=None),
        SimpleNamespace(status="completed", metrics={"per_node": "pas un dict"}),
        _run(per_node={"a": "pas un dict"}),
        _run(per_node={"a": {"duration_ms": "1500"}}),
        _run(per_node={"a": {"duration_ms": None}}),
        _run(per_node={"a": {}}),
        _run(per_node={"a": {"duration_ms": 900}}),
    ]
    assert node_duration_samples(runs) == {"a": [0.9]}


def test_a_boolean_duration_is_not_read_as_one_millisecond() -> None:
    """`bool` est une sous-classe de `int` — `True` passerait pour 1 ms."""
    assert node_duration_samples([_run(per_node={"a": {"duration_ms": True}})]) == {}


def test_non_positive_and_non_finite_durations_are_skipped() -> None:
    runs = [
        _run(per_node={"a": {"duration_ms": 0}}),
        _run(per_node={"a": {"duration_ms": -5}}),
        _run(per_node={"a": {"duration_ms": float("inf")}}),
        _run(per_node={"a": {"duration_ms": float("nan")}}),
    ]
    assert node_duration_samples(runs) == {}
