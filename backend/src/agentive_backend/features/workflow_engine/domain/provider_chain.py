"""Per-agent LLM provider chain resolution — pure, I/O-free (Story 4.6 T7.1).

Closes défer **D12**. Story 2.2 validated and persisted
``agent_templates.config["provider_chain"]``; nothing ever read it back, so
``execute_agent_node`` called ``LLMRouter.complete()`` without
``provider_chain=`` and every node silently ran on the process-wide default
chain. This module turns that stored JSONB into something the router will
accept.

**The intersection with the registered providers is mandatory, not
defensive.** ``LLMRouter.complete()`` raises a bare ``ValueError`` for any
name absent from its registry (``shared/llm/router.py``), and
``app.lifespan._build_llm_router`` only registers providers whose API key is
present — with no key at all, the sole registered provider is ``"mock"``. A
template carrying the archetypes' default ``provider_chain: ["anthropic"]``
would therefore raise inside the node, get caught by ``_execute``'s
``except Exception`` and end the run ``error``. In dev, in CI and under
testcontainers that is EVERY run of EVERY integration test.

Lives in ``domain/`` rather than next to its caller because two callers need
the exact same rule: the runtime (``engine/agent_node.py``, T7.3) and the
Mise en Place pre-flight check (``mise_en_place.py``, T9.1). A pre-flight
check must predict what the runtime DOES, never what the config looks like —
sharing one function is what makes that true by construction instead of by
vigilance.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ChainResolution:
    """Outcome of resolving one template's chain against what is registered.

    The three fields answer three different questions, which is why this is
    not simply ``list[str] | None``:

    * :attr:`chain` — what to hand ``LLMRouter.complete(provider_chain=...)``.
      ``None`` means "use the process default", the only safe answer when
      nothing the template asked for is registered.
    * :attr:`configured` — what the template DECLARED, usable or not. The
      Mise en Place check reports on this.
    * :attr:`dropped` — declared but not registered. Non-empty means the run
      will NOT use the chain its author wrote, which is worth a warning.
    * :attr:`malformed` — the column held something, and none of it was
      usable. Distinct from "declared nothing": both end on the process
      default, but only one of them means an author wrote a chain that is
      being ignored. Collapsing the two is what made a template carrying
      ``provider_chain: "anthropic"`` (a string, not a list) run on the
      wrong chain in total silence.
    """

    chain: tuple[str, ...] | None
    configured: tuple[str, ...]
    dropped: tuple[str, ...]
    malformed: bool = False
    #: The chain was rotated so the model's own provider leads. See
    #: :func:`resolve_provider_chain`'s note on the router's index-0 contract.
    reordered: bool = False
    #: The model's owning provider is not in the usable chain at all, so no
    #: ordering makes the chain coherent — :attr:`chain` is ``None``.
    incoherent: bool = False


def _declared(config: Mapping[str, Any]) -> tuple[tuple[str, ...], bool]:
    """Read ``config["provider_chain"]`` from free-form JSONB.

    Returns ``(chain, malformed)``. ``malformed`` is ``True`` when the key
    was PRESENT but yielded nothing usable — the caller needs to tell that
    apart from an absent key, because only the former deserves a warning.

    Mirrors ``agent_node._resolve_llm_params``'s posture on ``llm_params``:
    never presume the upstream Pydantic validation ran on THIS row. A row can
    predate the schema, come from a fixture, a data import or a migration —
    ``ProviderChain`` (the ``agent_registry`` VO) is strictly narrower than
    what this column can physically hold.

    Duplicates are dropped even though Story 2.2 rejects them at the HTTP
    boundary: a repeated provider would make the node re-try the same failing
    provider instead of moving on, defeating the chain.
    """
    raw = config.get("provider_chain")
    if raw is None:
        return (), False
    if not isinstance(raw, list) or not raw:
        # Present but unusable: a non-list (a bare string is the classic), or
        # an empty list. AC3 names both among the cases that must warn.
        return (), True
    seen: dict[str, None] = {}
    for item in raw:
        if not isinstance(item, str) or not item:
            # One bad element invalidates the whole chain rather than being
            # skipped: a partially-read chain is a chain nobody wrote, and
            # silently executing it is worse than falling back to the default.
            return (), True
        seen.setdefault(item, None)
    return tuple(seen), False


def resolve_provider_chain(
    config: Mapping[str, Any], *, available: Collection[str], model_owner: str | None = None
) -> ChainResolution:
    """Resolve a template's chain against the providers actually registered.

    ``available`` is ``LLMRouter.providers`` at the runtime call site and the
    set of key-configured providers at the Mise en Place call site — the same
    rule applied to the same question at two different moments.

    ``model_owner`` is the provider that owns ``config["llm_model"]`` (from
    ``infra.llm.pricing.provider_for_model``), passed in rather than looked up
    so this module keeps importing nothing but ``shared`` — the same reason
    ``available`` is a parameter.

    **The router treats index 0 as the model's owner.**
    ``LLMRouter._resolve_model`` returns the primary model VERBATIM for index
    0 and consults the fallback map only from index 1 on. So a chain whose
    head does not own the model sends, say, ``claude-sonnet-4-6`` to OpenAI:
    a 400, classified ``fatal``, raised on the spot with no fallback and no
    retry. Two ways to get there — a template declaring the order itself, and
    the intersection above PROMOTING a fallback provider to the head once the
    owner's key is missing.

    The answer is to rotate, not to refuse: the author chose a SET of
    providers, and the order that makes it work is derivable. When the owner
    is absent from the usable chain entirely, no order works and the chain is
    dropped (``incoherent``). An unknown ``model_owner`` (``None``) is never a
    reason to touch anything.
    """
    configured, malformed = _declared(config)
    if not configured:
        # No providers to drop either way — nobody asked for one that is
        # missing. `malformed` is what separates "the template declares no
        # chain" (nominal) from "the template declares a chain this code
        # cannot read" (a config bug the operator must hear about).
        return ChainResolution(chain=None, configured=(), dropped=(), malformed=malformed)

    usable = tuple(name for name in configured if name in available)
    dropped = tuple(name for name in configured if name not in available)
    if not usable:
        return ChainResolution(chain=None, configured=configured, dropped=dropped)

    reordered = False
    if model_owner is not None:
        if model_owner not in usable:
            # No rotation can fix this: whoever leads will be handed a model
            # it does not serve. Fall back to the process default, which
            # `_build_llm_router` keeps coherent with the keys it registered.
            return ChainResolution(
                chain=None, configured=configured, dropped=dropped, incoherent=True
            )
        if usable[0] != model_owner:
            usable = (model_owner, *(name for name in usable if name != model_owner))
            reordered = True

    return ChainResolution(
        chain=usable,
        configured=configured,
        dropped=dropped,
        reordered=reordered,
    )


__all__ = ["ChainResolution", "resolve_provider_chain"]
