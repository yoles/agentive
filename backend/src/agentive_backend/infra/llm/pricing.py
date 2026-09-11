"""LLM pricing tables — shared by the chat adapters AND
``features/workflow_engine/dry_run.py`` (Story 4.4 T3.5).

Split out of ``anthropic_adapter.py``/``openai_adapter.py`` on purpose:
those two modules import ``langchain_anthropic``/``langchain_openai``/
``langchain_core``, which ``.import-linter`` Contract 5 forbids
``features/*`` from reaching even TRANSITIVELY (unlike Contracts 3/4,
Contract 5 does not set ``allow_indirect_imports`` — every hop in the
import graph is checked). A feature importing ``MODEL_PRICING`` straight
from an adapter module therefore broke Contract 5 even though it never
touches a vendor SDK symbol itself. This module has ZERO langchain/vendor
SDK imports — plain ``Decimal`` tables.

**Scope of that guarantee, precisely** (review fix P10): it holds for the
import GRAPH, which is what Contract 5 checks and what the split was for.
It does NOT mean a feature importing this module loads no vendor SDK at
runtime — ``from agentive_backend.infra.llm.pricing import ...`` executes
the package's ``__init__.py``, which eagerly imports all four adapters and
therefore ``langchain_anthropic``, ``langchain_openai``, ``fastembed`` and
``voyageai``. ``grimp`` does not model parent-package execution, so
``lint-imports`` is right to pass and the contract is genuinely kept; the
runtime cost is simply a separate property this split never bought. Making
it true as well means giving ``infra/llm/__init__.py`` a lazy
``__getattr__`` — a change to Story 3.6's module surface, deliberately not
made here.

Both adapters re-export their table under the name ``MODEL_PRICING`` for
backward compatibility with existing call sites/tests; this module is the
single source of truth, not a second copy (the D84 lesson this repo has
already re-learned twice: two sources of truth for one setting diverge at
the first edit).

The tables are exposed as read-only ``MappingProxyType`` views (review fix
P21). Before the split each adapter owned its own ``dict``; re-exporting by
plain assignment made one mutable object shared by both adapters AND
``features/workflow_engine/dry_run.py``, so a single ``MODEL_PRICING[k] =
...`` anywhere would have silently rewritten pricing for every reader. No
call site mutates them today — the view is what keeps that true.

Snapshot 2026-05 — verify against
https://docs.anthropic.com/claude/docs/models-overview#model-pricing and
https://platform.openai.com/docs/pricing before relying on it for real
budgeting (same "Snapshot ... — verify against" posture as
``EMBEDDING_MODEL_PRICING``, ``shared/llm/metrics.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import Final

#: The pricing tables' shared shape: model id → (input, output) USD per 1M
#: tokens. ``Mapping``, not ``dict``: every export below is a read-only view.
ModelPricing = Mapping[str, tuple[Decimal, Decimal]]

# Tuple = (input USD per 1M tokens, output USD per 1M tokens).
#
# Long-form aliases — Anthropic returns dated identifiers in
# ``response_metadata.model_name`` (e.g. ``claude-sonnet-4-6-20250508``).
# Both forms must be priced or a cost lookup silently returns ``None`` for
# the long form, under-reporting ``LLM_COST_USD_TOTAL`` — exactly the case
# Story 9.4 budget caps need (review fix-batch P1).
_OPUS_PRICE = (Decimal("15.00"), Decimal("75.00"))
_SONNET_PRICE = (Decimal("3.00"), Decimal("15.00"))
_HAIKU_PRICE = (Decimal("0.80"), Decimal("4.00"))

ANTHROPIC_MODEL_PRICING: Final[ModelPricing] = MappingProxyType(
    {
        "claude-opus-4-7": _OPUS_PRICE,
        "claude-opus-4-7-20250508": _OPUS_PRICE,
        "claude-sonnet-4-6": _SONNET_PRICE,
        "claude-sonnet-4-6-20250508": _SONNET_PRICE,
        "claude-haiku-4-5": _HAIKU_PRICE,
        "claude-haiku-4-5-20251001": _HAIKU_PRICE,
    }
)

OPENAI_MODEL_PRICING: Final[ModelPricing] = MappingProxyType(
    {
        "gpt-5": (Decimal("10.00"), Decimal("30.00")),
        "gpt-5-mini": (Decimal("1.50"), Decimal("6.00")),
        "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
        "o4-mini": (Decimal("3.00"), Decimal("12.00")),
    }
)

__all__ = ["ANTHROPIC_MODEL_PRICING", "OPENAI_MODEL_PRICING", "ModelPricing"]
