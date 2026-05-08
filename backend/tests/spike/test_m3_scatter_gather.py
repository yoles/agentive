"""Test AC4 — scatter-gather avec Send + reducer operator.add.

Vérifie :

- les 3 summarizers contribuent chacun à ``partial_summaries`` (concaténation
  via le reducer ``operator.add`` sur le champ annoté) ;
- l'aggregator reçoit exactement 3 entrées et produit un ``final_summary`` ;
- la durée totale est < 1.5x la durée d'un seul summarizer (preuve de
  parallélisation effective — séquentiel donnerait ~3x la durée).
"""

from __future__ import annotations

import pytest
from spike.m3_scatter_gather import SUMMARIZER_WORK_SECONDS, run_scatter_gather

pytestmark = pytest.mark.asyncio


async def test_scatter_gather_runs_in_parallel() -> None:
    chunks = ["alpha-content", "beta-content", "gamma-content"]
    result = await run_scatter_gather(chunks=chunks)

    partials = result.get("partial_summaries") or []
    assert len(partials) == 3, f"expected 3 partial_summaries, got {len(partials)}"
    assert result.get("final_summary"), "final_summary must be assembled by aggregator"

    durations = result.get("durations") or []
    assert len(durations) == 3, "each summarizer should report its duration"

    total = float(result["_total_duration_s"])  # type: ignore[index]
    parallel_ceiling = SUMMARIZER_WORK_SECONDS * 1.5  # generous overhead allowance
    assert total < parallel_ceiling, (
        f"total duration {total:.3f}s exceeds parallel ceiling {parallel_ceiling:.3f}s — "
        f"summarizers may be running sequentially"
    )
