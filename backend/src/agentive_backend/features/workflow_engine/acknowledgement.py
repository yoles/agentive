"""Accusé de réception d'un run — Story 5.1 AC2.

« Compris. Je mobilise [agents]. ETA ~[X] min. »

Calculé **sans aucun appel LLM**, sur le chemin synchrone de
``POST /workflows/{workflow_id}/runs``, et persisté dans l'INSERT du run
(``workflow_runs.acknowledgement``). Les deux chemins SSE le rendent donc à
l'identique : la frame ``state`` de rattrapage (client attaché tard) et
l'event ``workflow_run.started`` (client déjà attaché).

**Pourquoi pas un appel LLM.** L'AC promet une première frame en moins de
deux secondes. Un appel de modèle sur ce chemin y mettrait la latence d'un
provider, ses retries et sa chaîne de repli — le pire cas légal se compte en
dizaines de secondes (``provider_chain`` fois ``NODE_TIMEOUT_S``). Tout ce que
l'accusé doit dire est déjà connu à ce moment-là : les templates du DAG sont
chargés, et l'historique des runs donne la durée.

**Pourquoi l'ETA est étiquetée.** ``eta_source`` vaut ``"history"`` seulement
si CHAQUE node du DAG a au moins un échantillon mesuré ; dès qu'un seul
retombe sur le défaut, c'est ``"heuristic"``. C'est la leçon que le Dry Run a
dû apprendre en revue (``no_execution_history``, ``node_estimate_from_fallback``,
``model_price_unresolved``) : un chiffre estimé qui se présente comme mesuré
est pire que pas de chiffre.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any, Final, Literal

from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)

EtaSource = Literal["history", "heuristic"]

#: Au-delà, la liste d'agents est tronquée dans le MESSAGE (jamais dans le
#: champ `agents`, qui reste complet et machine-lisible). Un nom de template
#: fait jusqu'à 255 caractères et un DAG jusqu'à plusieurs dizaines de nodes :
#: sans cette borne, une phrase censée rassurer devient un mur de texte sur la
#: première frame. Même raisonnement que `MAX_TOOL_RESULT_CHARS` (Story 5.0).
MAX_AGENTS_IN_MESSAGE: Final = 6

#: Plancher de l'ETA. `~0 min` ne veut rien dire pour un lecteur humain, et
#: ce dépôt refuse le zéro fabriqué (cf `Completion.cost_estimate_usd`, qui
#: impose `None` pour « inconnu » précisément pour ne pas le confondre).
MIN_ETA_MINUTES: Final = 1


@dataclass(frozen=True)
class Acknowledgement:
    """Ce que la première frame SSE d'un run doit dire."""

    message: str
    agents: tuple[str, ...]
    eta_minutes: int
    eta_source: EtaSource

    def to_payload(self) -> dict[str, Any]:
        """La forme JSONB persistée, et telle quelle la forme rendue en SSE."""
        return {
            "message": self.message,
            "agents": list(self.agents),
            "eta_minutes": self.eta_minutes,
            "eta_source": self.eta_source,
        }


def node_duration_samples(runs: Sequence[Any]) -> dict[str, list[float]]:
    """Durées par node, relevées sur ``metrics.per_node[*].duration_ms``.

    Même posture défensive que ``dry_run._node_metric_samples_from_runs``,
    dont c'est le jumeau sur un autre champ : ``metrics`` est du JSONB libre,
    une forme inattendue est ignorée et jamais levée. Un accusé de réception
    ne doit pas pouvoir faire échouer un lancement de run.

    Ne lit que les runs ``completed`` : la durée d'un node d'un run planté ou
    annulé n'est pas une durée d'exécution, c'est une durée d'interruption.
    """
    samples: dict[str, list[float]] = {}
    for run in runs:
        if getattr(run, "status", None) != "completed":
            continue
        metrics = getattr(run, "metrics", None)
        if not isinstance(metrics, dict):
            continue
        per_node = metrics.get("per_node")
        if not isinstance(per_node, dict):
            continue
        for node_id, metric in per_node.items():
            if not isinstance(node_id, str) or not isinstance(metric, dict):
                continue
            duration_ms = metric.get("duration_ms")
            # `bool` is an `int` subclass — `True` would be read as 1 ms.
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, int | float):
                continue
            if duration_ms <= 0 or not math.isfinite(duration_ms):
                continue
            samples.setdefault(node_id, []).append(float(duration_ms) / 1000.0)
    return samples


def _join_agents(agents: Sequence[str]) -> str:
    """« A », « A et B », « A, B et C », puis « A, B, …, F et 3 autres »."""
    if not agents:
        # Ce cas ne devrait pas se produire — un DAG sans node est refusé en
        # amont — mais `_load_templates` peut légitimement recevoir un DAG
        # STOCKÉ vide. La phrase rendue était « Je mobilise aucun agent. » :
        # français cassé, et une ETA d'une minute annoncée pour zéro travail.
        # `_zero_node_acknowledgement` traite le cas à la source ; ce repli
        # reste par défense en profondeur.
        return "aucun agent"
    shown = list(agents[:MAX_AGENTS_IN_MESSAGE])
    hidden = len(agents) - len(shown)
    if hidden > 0:
        shown.append(f"{hidden} autre{'s' if hidden > 1 else ''}")
    if len(shown) == 1:
        return shown[0]
    return f"{', '.join(shown[:-1])} et {shown[-1]}"


def _zero_node_acknowledgement(agents: tuple[str, ...]) -> Acknowledgement:
    """L'accusé d'un DAG sans node exécutable — dit ce qui est, sans inventer.

    ``eta_minutes`` reste à :data:`MIN_ETA_MINUTES` parce que le contrat de
    sortie le borne à ``>= 1`` ; ce qui porte l'information, c'est
    ``eta_source="heuristic"`` et le message, qui ne prétend mobiliser
    personne.
    """
    return Acknowledgement(
        message="Compris. Aucun agent à mobiliser : ce workflow n'a aucun node exécutable.",
        agents=agents,
        eta_minutes=MIN_ETA_MINUTES,
        eta_source="heuristic",
    )


def build_acknowledgement(
    *,
    agent_names: Sequence[str],
    node_ids: Sequence[str],
    duration_samples: Mapping[str, Sequence[float]],
    default_node_duration_s: float,
) -> Acknowledgement:
    """Compose l'accusé de réception d'un run qui démarre.

    Args:
        agent_names: noms des templates mobilisés, dans l'ordre du DAG.
            Dédupliqués ici — un même template monté sur deux nodes est un
            seul agent mobilisé, et le répéter donnerait une phrase fausse.
        node_ids: les nodes du DAG. L'ETA les somme TOUS, sans tenter de
            deviner un chemin probable : c'est le rôle du Dry Run, qui a son
            propre endpoint et ne tient pas dans le budget de deux secondes.
            L'accusé sur-estime donc un DAG très branché, et c'est le bon
            côté sur lequel se tromper.
        duration_samples: sortie de :func:`node_duration_samples`.
        default_node_duration_s: repli quand un node n'a aucun échantillon.
    """
    unique_agents: dict[str, None] = {}
    for name in agent_names:
        if isinstance(name, str) and name.strip():
            unique_agents.setdefault(name.strip(), None)
    agents = tuple(unique_agents)

    if not node_ids:
        # Zéro node : ni « aucun agent » (agrammatical), ni une ETA d'une
        # minute pour un run qui n'exécutera rien. Le module refuse le zéro
        # FABRIQUÉ ; il ne doit pas non plus fabriquer du travail.
        return _zero_node_acknowledgement(agents)

    total_s = 0.0
    from_history = bool(node_ids)
    for node_id in node_ids:
        samples = duration_samples.get(node_id)
        if samples:
            total_s += float(median(samples))
        else:
            total_s += default_node_duration_s
            from_history = False

    eta_minutes = max(MIN_ETA_MINUTES, math.ceil(total_s / 60.0))
    eta_source: EtaSource = "history" if from_history else "heuristic"

    acknowledgement = Acknowledgement(
        message=f"Compris. Je mobilise {_join_agents(agents)}. ETA ~{eta_minutes} min.",
        agents=agents,
        eta_minutes=eta_minutes,
        eta_source=eta_source,
    )
    _log.debug(
        "workflow_engine.acknowledgement_built",
        agent_count=len(agents),
        node_count=len(node_ids),
        eta_minutes=eta_minutes,
        eta_source=eta_source,
    )
    return acknowledgement


__all__ = [
    "MAX_AGENTS_IN_MESSAGE",
    "MIN_ETA_MINUTES",
    "Acknowledgement",
    "EtaSource",
    "build_acknowledgement",
    "node_duration_samples",
]
