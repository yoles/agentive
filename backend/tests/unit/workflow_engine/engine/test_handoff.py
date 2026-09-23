"""Unit tests — :func:`summarize_handoff` (Story 4.7 T2, AC1).

`summarize_handoff` never raises — every failure mode (LLM error, unparsable
response, contract violation) degrades to `None`, asserted as an executable
proof for each branch rather than a structural claim.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agentive_backend.features.workflow_engine.engine.handoff import (
    MAX_SUMMARY_INPUT_CHARS,
    HandoffSettings,
    summarize_handoff,
)
from agentive_backend.shared.contracts.handoff import HandoffSummary
from agentive_backend.shared.llm.exceptions import LLMProviderUnavailableError
from agentive_backend.shared.llm.types import Completion


def _settings() -> HandoffSettings:
    return HandoffSettings(model="claude-haiku-4-5", max_tokens=512, timeout_s=20.0)


def _completion(text: str, *, input_tokens: int = 40, output_tokens: int = 12) -> Completion:
    return Completion(
        text=text,
        model="claude-haiku-4-5",
        provider="anthropic",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason="stop",
        latency_ms=5.0,
    )


def _router(
    completion: Completion | None = None, *, side_effect: Exception | None = None
) -> AsyncMock:
    router = AsyncMock()
    if side_effect is not None:
        router.complete = AsyncMock(side_effect=side_effect)
    else:
        router.complete = AsyncMock(return_value=completion)
    return router


_VALID_JSON = json.dumps(
    {
        "decisions": ["chose plan A"],
        "artifacts_refs": ["report.md"],
        "blockers": [],
        "next_questions": ["confirm budget?"],
    }
)


@pytest.mark.asyncio
async def test_summarize_handoff_when_response_is_clean_json_should_return_summary_and_tokens() -> (
    None
):
    router = _router(_completion(_VALID_JSON, input_tokens=50, output_tokens=20))

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is not None
    assert result.summary == HandoffSummary(
        decisions=["chose plan A"],
        artifacts_refs=["report.md"],
        blockers=[],
        next_questions=["confirm budget?"],
    )
    assert (result.input_tokens, result.output_tokens) == (50, 20)


@pytest.mark.asyncio
async def test_summarize_handoff_when_response_wrapped_in_markdown_fence_should_still_parse() -> (
    None
):
    fenced = f"```json\n{_VALID_JSON}\n```"
    router = _router(_completion(fenced))

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is not None
    assert result.summary.decisions == ["chose plan A"]


@pytest.mark.asyncio
async def test_summarize_handoff_when_response_is_not_json_should_return_none() -> None:
    router = _router(_completion("I cannot summarize this."))

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is None


@pytest.mark.asyncio
async def test_summarize_handoff_when_response_violates_contract_should_return_none() -> None:
    malformed = json.dumps(
        {"decisions": "not a list", "artifacts_refs": [], "blockers": [], "next_questions": []}
    )
    router = _router(_completion(malformed))

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is None


@pytest.mark.asyncio
async def test_summarize_handoff_when_llm_router_raises_should_return_none_not_propagate() -> None:
    router = _router(side_effect=LLMProviderUnavailableError(detail="down"))

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is None


@pytest.mark.asyncio
async def test_summarize_handoff_should_call_router_without_provider_chain() -> None:
    """T2.2 — process-wide call, never per-agent (mirror hybrid_router's escalation)."""
    router = _router(_completion(_VALID_JSON))

    await summarize_handoff({"status": "ok"}, llm_router=router, node_id="a", settings=_settings())

    _, kwargs = router.complete.await_args
    assert "provider_chain" not in kwargs
    assert kwargs["temperature"] == 0.0
    assert kwargs["model"] == "claude-haiku-4-5"
    assert kwargs["max_tokens"] == 512
    assert kwargs["timeout_s"] == 20.0


# ─── Revue du 2026-09-12 — les trous que « never raises » ne couvrait pas ───


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("usage"),
        TypeError("unexpected response shape"),
        ValueError("bad request"),
        AttributeError("'NoneType' object has no attribute 'content'"),
    ],
)
@pytest.mark.asyncio
async def test_summarize_handoff_when_router_raises_a_fatal_non_llm_error_should_return_none(
    exc: Exception,
) -> None:
    """P-1 — the hole `except LLMError` left wide open.

    `LLMRouter.complete` does `raise` — bare, re-raising the ORIGINAL
    exception — for anything `classify_error` buckets as "fatal", and that
    bucket is `TypeError`/`ValueError`/`AttributeError`/`KeyError`/
    `pydantic.ValidationError` (`shared/llm/error_classifier.py`). None of
    them subclass `LLMError`, so a provider adapter tripping on an unexpected
    response shape travelled straight out of this function, out of
    `execute_agent_node` BEFORE its `return node_update`, and LangGraph
    discarded the whole superstep: the node's own completion — already paid
    for — lost, and re-billed on resume. Exactly the AC1 clause-5 promise
    ("N'ARRÊTE JAMAIS le run") inverted.
    """
    router = _router(side_effect=exc)

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is None


@pytest.mark.asyncio
async def test_summarize_handoff_when_node_output_is_unserializable_should_return_none() -> None:
    """P-1 — `json.dumps` used to sit OUTSIDE the `try`. `default=str` rescues
    unserializable VALUES, never an unsupported KEY type."""
    router = _router(_completion(_VALID_JSON))

    result = await summarize_handoff(
        {("tuple", "key"): "value"},  # type: ignore[dict-item]
        llm_router=router,
        node_id="a",
        settings=_settings(),
    )

    assert result is None
    router.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_handoff_when_summary_is_entirely_empty_should_return_none() -> None:
    """P-3 — the most dangerous "success" this function could return.

    All four fields default to `[]`, so `{}` VALIDATES. That empty summary
    then replaced the upstream output wholesale and the consuming node ran on
    no upstream information at all — no warning, no metric, and a run that
    finishes `completed` on a wrong result. "Nothing to say" and "failed to
    say it" are indistinguishable here, so the safe reading wins: absence
    routes the consumer back to the raw output (AC2), which is strictly more
    information, never less.
    """
    router = _router(_completion(json.dumps({})))

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is None


@pytest.mark.asyncio
async def test_summarize_handoff_when_response_was_cut_off_should_return_none() -> None:
    """P-9 — a truncated answer whose JSON prefix happens to parse validates
    against a contract where every field defaults to `[]`, and would ship a
    summary that silently dropped most of its content."""
    truncated = json.dumps({"decisions": ["chose plan A"], "artifacts_refs": []})
    router = _router(
        Completion(
            text=truncated,
            model="claude-haiku-4-5",
            provider="anthropic",
            input_tokens=40,
            output_tokens=512,
            finish_reason="length",
            latency_ms=5.0,
        )
    )

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is None


@pytest.mark.asyncio
async def test_summarize_handoff_when_response_carries_an_extra_key_should_still_summarize() -> (
    None
):
    """BS-2 mitigation — `extra="forbid"` stays on the contract (T1.1), but a
    model answering the four correct keys PLUS a fifth is a stable behaviour
    for a given model, and losing the whole paid call to it meant FR53 could
    be off fleet-wide while still billing one call per node. Unknown keys are
    projected away here; the contract's shape is unchanged."""
    chatty = json.dumps(
        {
            "decisions": ["chose plan A"],
            "artifacts_refs": [],
            "blockers": [],
            "next_questions": [],
            "confidence": 0.9,
        }
    )
    router = _router(_completion(chatty))

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is not None
    assert result.summary == HandoffSummary(decisions=["chose plan A"])


@pytest.mark.asyncio
async def test_summarize_handoff_caps_the_input_it_ships_to_the_model() -> None:
    """P-9 — the mechanism whose whole purpose is to REDUCE token consumption
    was the most direct source of UNBOUNDED consumption: a node producing
    200 kB of JSON had all 200 kB shipped to the summarizer."""
    router = _router(_completion(_VALID_JSON))
    huge = {"blob": "x" * (MAX_SUMMARY_INPUT_CHARS * 2)}

    result = await summarize_handoff(huge, llm_router=router, node_id="a", settings=_settings())

    assert result is not None
    sent = router.complete.await_args.args[0][0].content
    assert len(sent) < MAX_SUMMARY_INPUT_CHARS * 1.1


@pytest.mark.asyncio
async def test_summarize_handoff_carries_the_calls_own_cost() -> None:
    """P-2 — `completion.cost_estimate_usd` used to be dropped on the floor,
    which is why the summary spend was unreconstructible from any surface."""
    priced = Completion(
        text=_VALID_JSON,
        model="claude-haiku-4-5",
        provider="anthropic",
        input_tokens=40,
        output_tokens=12,
        finish_reason="stop",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.00042"),
    )
    router = _router(priced)

    result = await summarize_handoff(
        {"status": "ok"}, llm_router=router, node_id="a", settings=_settings()
    )

    assert result is not None
    assert result.cost_usd == Decimal("0.00042")


@pytest.mark.asyncio
async def test_summarize_handoff_counts_every_failure_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """P-8 — one `_log.warning` per node was the only trace a misconfigured
    summary model left. `/handoff-stats` then returned `reduction_ratio_pct:
    null`, byte-identical to the legitimate "no node has a successor" case,
    so nothing could be alerted on."""
    import agentive_backend.features.workflow_engine.engine.handoff as handoff_module

    seen: list[str] = []
    monkeypatch.setattr(
        handoff_module.HANDOFF_SUMMARY_FAILURES_TOTAL,
        "labels",
        lambda **kw: SimpleNamespace(inc=lambda: seen.append(kw["reason"])),
    )

    await summarize_handoff(
        {"ok": 1},
        llm_router=_router(_completion("not json")),
        node_id="a",
        settings=_settings(),
    )
    await summarize_handoff(
        {"ok": 1},
        llm_router=_router(_completion(json.dumps({}))),
        node_id="a",
        settings=_settings(),
    )
    await summarize_handoff(
        {"ok": 1},
        llm_router=_router(side_effect=LLMProviderUnavailableError(detail="down")),
        node_id="a",
        settings=_settings(),
    )

    assert seen == ["unparsable_response", "empty_summary", "LLMProviderUnavailableError"]
