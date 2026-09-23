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

import re
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

_PROVIDER_PRICING: Final[tuple[tuple[str, ModelPricing], ...]] = (
    ("anthropic", ANTHROPIC_MODEL_PRICING),
    ("openai", OPENAI_MODEL_PRICING),
)


#: Every provider name these tables describe. The Mise en Place pre-flight
#: uses it as the universe of providers it can answer "is a key configured?"
#: for; exposed here so that set and :func:`provider_for_model` can never
#: disagree about which providers exist.
KNOWN_PROVIDERS: Final[tuple[str, ...]] = tuple(name for name, _pricing in _PROVIDER_PRICING)


#: Story 4.12 AC6 / D98 (Story 4.9) — OWNERSHIP, independent of pricing-table
#: completeness. A model released after this file's last pricing snapshot
#: (or a dated variant nobody has added yet) used to make `provider_for_model`
#: return `None`, which `domain.provider_chain.resolve_provider_chain` reads
#: as "unknown, never rotate, never flag `incoherent`" — so a `provider_chain`
#: whose head does not serve the template's `llm_model` sailed through
#: unrotated, the router sent the model to the wrong provider verbatim at
#: index 0, and the node died on a `fatal` 400 the retry dispatcher correctly
#: refuses to retry. Prefix-matched rather than another exact table: a
#: provider's model-naming families are stable across releases even when
#: individual model ids are not, and this only needs to answer "whose API
#: would accept this name", never "how much does it cost" — pricing
#: completeness stays `dry_run.py`'s separate, already-handled concern
#: (`model_price_unresolved`).
#
# `claude-` stays a bare stem: a trademarked product name, so a third-party
# family colliding with it is not a realistic concern.
_MODEL_OWNER_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("claude-", "anthropic"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
)

#: "GPT" is a GENERIC term, unlike `claude`, so a bare `gpt-` stem also
#: claims third-party families like `gpt-oss-*`, `gpt-neox` and `gpt-j` —
#: which would rotate OpenAI to the head of the chain and post a model
#: OpenAI has never heard of to its API, a `fatal` 400 the dispatcher
#: refuses to retry. Requiring a DIGIT separates them without re-listing a
#: generation on every release: `gpt-4`, `gpt-3.5-turbo` and an unreleased
#: `gpt-6` match, the families above do not.
_OPENAI_GPT_FAMILY_RE: Final = re.compile(r"^gpt-\d")


def provider_for_model(model: str) -> str | None:
    """The provider that OWNS ``model``, or ``None`` if nothing recognises it.

    Lives here because this is the layer that holds the tables, and because
    two features need the exact same answer: the Mise en Place pre-flight
    check and ``engine/agent_node``'s chain resolution. A pre-flight check
    must predict what the runtime DOES, and that is only true by construction
    when both read the same function.

    Checks the exact pricing-table keys FIRST (the common case, and the only
    source that can also answer "how much"), then falls back to
    :data:`_MODEL_OWNER_PREFIXES` for a model this file has not priced yet.
    ``None`` is still possible — a name matching no known provider's family —
    and is not a failure: an unknown model simply cannot be reasoned about,
    and no caller may fail a run on that alone.
    """
    for provider, pricing in _PROVIDER_PRICING:
        if model in pricing:
            return provider
    if _OPENAI_GPT_FAMILY_RE.match(model):
        return "openai"
    for prefix, provider in _MODEL_OWNER_PREFIXES:
        if model.startswith(prefix):
            return provider
    return None


__all__ = [
    "ANTHROPIC_MODEL_PRICING",
    "KNOWN_PROVIDERS",
    "OPENAI_MODEL_PRICING",
    "ModelPricing",
    "provider_for_model",
]
