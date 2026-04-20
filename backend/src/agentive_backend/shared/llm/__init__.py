"""LLM abstraction — multi-provider routing + budget + safety.

**Stub Sprint 0** — full implementation in Story 1.6.

Future public API :
    LLMProvider (Protocol) — `complete`, `raw_provider_call`
    LLMRouter — configures provider chain with fallback
    BudgetGuard — enforces budget caps + rate limits
"""

from __future__ import annotations
