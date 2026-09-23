"""Pure provider-chain resolution — Story 4.6 T7.1/T9.1, AC3.

This is the module that closes défer D12, and the intersection it performs
is not a defensive nicety: ``LLMRouter.complete()`` raises a bare
``ValueError`` for any provider name absent from its registry, and
``app.lifespan._build_llm_router`` only registers providers whose API key is
configured — so in dev, CI and testcontainers the ONLY registered provider
is ``"mock"``. Passing a template's declared ``["anthropic"]`` straight
through would kill every run of every integration test.

The same function answers the Mise en Place pre-flight check (T9.1), so the
check predicts exactly what the runtime DOES rather than what the config
LOOKS like.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentive_backend.features.workflow_engine.domain.provider_chain import (
    ChainResolution,
    resolve_provider_chain,
)


def test_resolve_provider_chain_when_fully_available_should_pass_it_through() -> None:
    resolution = resolve_provider_chain(
        {"provider_chain": ["anthropic", "openai"]}, available=("anthropic", "openai", "mock")
    )
    assert resolution == ChainResolution(
        chain=("anthropic", "openai"), configured=("anthropic", "openai"), dropped=()
    )


def test_resolve_provider_chain_when_partially_available_should_filter_and_report() -> None:
    resolution = resolve_provider_chain(
        {"provider_chain": ["anthropic", "openai"]}, available=("openai",)
    )
    assert resolution.chain == ("openai",)
    assert resolution.dropped == ("anthropic",)


def test_resolve_provider_chain_when_declared_order_differs_should_keep_declared_order() -> None:
    """Order is the whole point of a chain — never the registry's order."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["openai", "anthropic"]}, available=("anthropic", "openai")
    )
    assert resolution.chain == ("openai", "anthropic")


def test_resolve_provider_chain_when_nothing_available_should_yield_none() -> None:
    """THE trap of this story: the router would raise `ValueError` and the
    run would die. `None` means "use the process default chain"."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["anthropic", "openai"]}, available=("mock",)
    )
    assert resolution.chain is None
    assert resolution.dropped == ("anthropic", "openai")


@pytest.mark.parametrize(
    "config",
    [
        pytest.param({}, id="absent"),
        pytest.param({"provider_chain": None}, id="null"),
        pytest.param({"provider_chain": []}, id="empty"),
        pytest.param({"provider_chain": "anthropic"}, id="string-not-list"),
        pytest.param({"provider_chain": {"0": "anthropic"}}, id="dict-not-list"),
        pytest.param({"provider_chain": [123, "anthropic"]}, id="non-string-element"),
        pytest.param({"provider_chain": ["anthropic", None]}, id="null-element"),
        pytest.param({"provider_chain": [""]}, id="blank-element"),
    ],
)
def test_resolve_provider_chain_when_config_is_unusable_should_yield_none_without_raising(
    config: dict[str, Any],
) -> None:
    resolution = resolve_provider_chain(config, available=("anthropic", "openai"))
    assert resolution.chain is None
    assert resolution.dropped == ()
    assert resolution.configured == ()


def test_resolve_provider_chain_when_chain_has_duplicates_should_deduplicate() -> None:
    """Story 2.2 rejects duplicates at the HTTP boundary, but the column is
    free JSONB — a duplicate would retry the same failing provider twice."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["anthropic", "anthropic", "openai"]},
        available=("anthropic", "openai"),
    )
    assert resolution.chain == ("anthropic", "openai")


def test_resolve_provider_chain_when_unusable_should_report_no_drop() -> None:
    """An absent chain is not a DROPPED chain — it must not log a warning
    about providers the operator never asked for."""
    assert resolve_provider_chain({}, available=()).dropped == ()


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        pytest.param({}, False, id="absent-is-nominal"),
        pytest.param({"provider_chain": None}, False, id="null-is-nominal"),
        pytest.param({"provider_chain": []}, True, id="empty"),
        pytest.param({"provider_chain": "anthropic"}, True, id="string-not-list"),
        pytest.param({"provider_chain": {"0": "anthropic"}}, True, id="dict-not-list"),
        pytest.param({"provider_chain": [123, "anthropic"]}, True, id="non-string-element"),
        pytest.param({"provider_chain": [""]}, True, id="blank-element"),
        pytest.param({"provider_chain": ["anthropic"]}, False, id="usable"),
    ],
)
def test_resolve_provider_chain_should_separate_declaring_nothing_from_declaring_garbage(
    config: dict[str, Any], expected: bool
) -> None:
    """Both end on the process default chain, but only one of them means an
    author wrote a chain that is being ignored. Collapsing the two is what let
    a template carrying `provider_chain: "anthropic"` — a string where a list
    belongs — run on the wrong providers without a single log line, while AC3
    asks for a warning on "absent / mal typé / vide"."""
    assert resolve_provider_chain(config, available=("anthropic", "openai")).malformed is expected


# ─── Story 4.6 IG1 / review lot 12 — the `model_owner` half ────────────
#
# Every test above calls `resolve_provider_chain(config, available=...)` with
# no third argument, so the rotation and the `incoherent` drop — the two
# branches `IG1` added, and the ones deciding WHICH of three different
# operator messages the pre-flight emits — were never reached in the file
# that is supposed to be their exhaustive home. They were covered only
# through `agent_node`, which asserts the resulting `provider_chain=` kwarg
# and ignores both flags; `reordered` was asserted nowhere at all.


def test_resolve_provider_chain_rotates_the_models_owner_to_the_head() -> None:
    """`LLMRouter._resolve_model` hands index 0 the primary model VERBATIM
    and consults the fallback map only from index 1. A chain whose head does
    not own `llm_model` therefore sends `claude-sonnet-4-6` to OpenAI: a 400,
    classified `fatal`, raised with no fallback and no retry.

    The answer is to rotate, not to refuse — the author chose a SET of
    providers and the order that works is derivable."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["openai", "anthropic"]},
        available=("openai", "anthropic"),
        model_owner="anthropic",
    )

    assert resolution.chain == ("anthropic", "openai")
    assert resolution.reordered is True
    assert resolution.incoherent is False


def test_resolve_provider_chain_leaves_a_correctly_ordered_chain_alone() -> None:
    """`reordered` must mean something: a chain already led by the model's
    owner is not touched, and does not claim to have been."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["anthropic", "openai"]},
        available=("openai", "anthropic"),
        model_owner="anthropic",
    )

    assert resolution.chain == ("anthropic", "openai")
    assert resolution.reordered is False


def test_resolve_provider_chain_drops_a_chain_no_ordering_can_fix() -> None:
    """No rotation helps when the model's owner is not in the usable chain at
    all: whoever leads is handed a model it does not serve. The chain is
    abandoned for the process default, and `incoherent` is what lets the
    pre-flight say so instead of reporting a missing key."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["openai"]}, available=("openai",), model_owner="anthropic"
    )

    assert resolution.chain is None
    assert resolution.incoherent is True
    assert resolution.configured == ("openai",)


def test_resolve_provider_chain_never_rotates_on_an_unknown_model() -> None:
    """`model_owner=None` means the pricing tables do not know the model —
    never a reason to reorder what the author wrote."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["openai", "anthropic"]},
        available=("openai", "anthropic"),
        model_owner=None,
    )

    assert resolution.chain == ("openai", "anthropic")
    assert resolution.reordered is False
    assert resolution.incoherent is False


def test_resolve_provider_chain_rotation_survives_a_dropped_provider() -> None:
    """Rotation and intersection compose: the owner leads what REMAINS after
    the unavailable providers are dropped."""
    resolution = resolve_provider_chain(
        {"provider_chain": ["openai", "mistral", "anthropic"]},
        available=("openai", "anthropic"),
        model_owner="anthropic",
    )

    assert resolution.chain == ("anthropic", "openai")
    assert resolution.dropped == ("mistral",)
    assert resolution.reordered is True
