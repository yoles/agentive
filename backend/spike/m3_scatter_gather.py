"""Spike scatter-gather — Story 1.2 AC4.

Démontre l'orchestration **fan-out / fan-in** native de LangGraph 1.1.8 :

- Un nœud ``dispatcher`` émet plusieurs ``Send(...)`` qui activent en parallèle
  3 ``summarizer_*`` (chunks distincts) ;
- Chaque summarizer ajoute sa contribution à un champ d'état ``partial_summaries``
  typé ``Annotated[list[str], operator.add]`` ⇒ le reducer concatène
  automatiquement les contributions parallèles ;
- Un nœud ``aggregator`` reçoit l'état une fois les 3 partial_summaries collectés
  et calcule le résumé final.

Préparation Epic 5 (Dev Department — Dev Lead orchestre Architect/Producer/Reviewer
en parallèle via le même pattern). Le test :file:`tests/spike/test_m3_scatter_gather.py`
vérifie aussi que la durée totale est inférieure à 1.5x la durée d'un seul
summarizer (preuve de parallélisation effective).
"""

from __future__ import annotations

import asyncio
import logging
import operator
import time
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [scatter] %(message)s")

# Per-summarizer simulated work — large enough to make parallel speedup observable.
SUMMARIZER_WORK_SECONDS = 0.5


class ScatterState(TypedDict, total=False):
    task: str
    chunks: list[str]
    partial_summaries: Annotated[list[str], operator.add]
    final_summary: str | None
    durations: Annotated[list[float], operator.add]


class SummarizerInput(TypedDict):
    chunk_id: str
    chunk: str


def dispatcher(state: ScatterState) -> list[Send]:
    """Fan out one Send per chunk → 3 summarizers run in parallel."""
    chunks = state.get("chunks") or ["chunk-A", "chunk-B", "chunk-C"]
    logger.info("dispatcher fan-out=%d", len(chunks))
    return [
        Send("summarizer", {"chunk_id": f"S{i}", "chunk": chunk}) for i, chunk in enumerate(chunks)
    ]


async def summarizer(item: SummarizerInput) -> dict[str, list[object]]:
    """Single-chunk summarizer — runs concurrently with siblings via Send."""
    started = time.monotonic()
    logger.info("summarizer %s start", item["chunk_id"])
    await asyncio.sleep(SUMMARIZER_WORK_SECONDS)
    summary = f"summary({item['chunk_id']}:{item['chunk'][:20]})"
    duration = time.monotonic() - started
    logger.info("summarizer %s done duration=%.3fs", item["chunk_id"], duration)
    # Both fields use the operator.add reducer ⇒ each return appends to the list.
    return {"partial_summaries": [summary], "durations": [duration]}


def aggregator(state: ScatterState) -> dict[str, object]:
    """Collect parallel summaries and assemble the final output."""
    partials = state.get("partial_summaries") or []
    durations = state.get("durations") or []
    logger.info(
        "aggregator partials=%d durations=%s avg=%.3fs",
        len(partials),
        [round(d, 3) for d in durations],
        sum(durations) / max(len(durations), 1),
    )
    return {"final_summary": " | ".join(partials)}


def build_graph() -> object:
    builder = StateGraph(ScatterState)
    builder.add_node("summarizer", summarizer)
    builder.add_node("aggregator", aggregator)
    # The dispatcher is exposed as a conditional edge that returns Send(...) objects:
    # this is the canonical map-reduce / scatter-gather pattern in LangGraph.
    builder.add_conditional_edges(START, dispatcher, ["summarizer"])
    builder.add_edge("summarizer", "aggregator")
    builder.add_edge("aggregator", END)
    # MemorySaver is enough for this spike — no resume / no HITL needed here.
    return builder.compile(checkpointer=MemorySaver())


async def run_scatter_gather(
    chunks: list[str] | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    graph = build_graph()
    initial: ScatterState = {
        "task": "demo scatter-gather",
        "chunks": chunks or ["alpha-content", "beta-content", "gamma-content"],
        "partial_summaries": [],
        "durations": [],
        "final_summary": None,
    }
    result = await graph.ainvoke(initial, {"configurable": {"thread_id": "scatter-demo"}})
    total = time.monotonic() - started
    logger.info("scatter-gather complete total_duration=%.3fs", total)
    return dict(result) | {"_total_duration_s": total}


def main() -> None:
    asyncio.run(run_scatter_gather())


if __name__ == "__main__":
    main()
