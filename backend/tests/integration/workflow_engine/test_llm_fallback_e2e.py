"""End-to-end integration test — multi-provider LLM fallback inside a
workflow run (Story 4.6 T12.10, AC3).

The fallback MACHINERY is Story 1.6's and is already covered by
``tests/integration/llm/``. What was never covered — and what défer **D12**
was — is that a workflow run can reach it at all: ``execute_agent_node``
called ``LLMRouter.complete()`` without ``provider_chain=``, so a template's
declared chain was dead configuration. These tests exercise the whole path,
from ``POST /runs`` to the row and the outbox.

Two scenarios, and the second is the one that produces something new:

* provider A down, B healthy → the run COMPLETES on B, and
  ``workflow_engine.llm.fallback_triggered`` is in the outbox.
* both down → the run ends ``error``, the ``failed`` event carries
  ``error_type="LLMAllProvidersFailedError"`` (AC3's alerting hook), and the
  per-attempt breakdown that used to be discarded entirely is persisted,
  redacted, in ``checkpoint.last_error_attempts``.

No network: every provider here is a double, and the model names are real
entries of ``DEFAULT_MODEL_FALLBACK_MAP`` so the router can resolve the
equivalent model on the fallback provider.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.contracts.events import LLMFallbackTriggeredEvent
from agentive_backend.shared.event_bus import publish_and_commit
from agentive_backend.shared.llm.exceptions import LLMProviderUnavailableError
from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.types import Completion
from agentive_backend.shared.repositories import AgentTemplateRepo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app
from .conftest import wire_execution_service

pytestmark = pytest.mark.integration

_POLL_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.05

#: A real pair from `DEFAULT_MODEL_FALLBACK_MAP` — the router refuses to fall
#: back to a provider it has no equivalent model for (`LLMNoFallbackModelError`,
#: fatal), so an invented model name would test the wrong failure.
_PRIMARY_MODEL = "claude-sonnet-4-6"


class _RecordingProvider:
    """Answers, or raises, and counts — no network, no FIFO to exhaust."""

    def __init__(self, name: str, *, fails_with: Exception | None = None) -> None:
        self.provider_name = name
        self._fails_with = fails_with
        self.call_count = 0

    async def complete(self, messages: Any, *, model: str, **_kwargs: Any) -> Completion:
        self.call_count += 1
        if self._fails_with is not None:
            raise self._fails_with
        return Completion(
            text=f'{{"answered_by": "{self.provider_name}"}}',
            model=model,
            provider=self.provider_name,
            input_tokens=10,
            output_tokens=5,
            finish_reason="stop",
            latency_ms=5.0,
            cost_estimate_usd=Decimal("0.0001"),
        )


def _make_fallback_publisher(
    session_factory: async_sessionmaker[AsyncSession],
) -> Any:
    """Mirror ``app.lifespan._publish_fallback`` on a hand-built test app.

    ``make_e2e_app`` does not run the real lifespan, so nothing would wire
    ``on_fallback=`` and the event would simply never exist here — the
    assertion would then be testing the fixture, not the product. This
    reproduces the SAME wiring, through the SAME typed contract Story 4.6
    T4.3 introduced, so what is asserted downstream is the real payload
    shape rather than a dict this file invented.

    Deliberately not importing the lifespan's own closure: it is built around
    a real ``session_factory`` and a ``ContextVar`` dance for correlation
    binding that belongs to process startup, not to a test app.
    """

    async def _publish(ctx: Any) -> None:
        async with session_factory() as session:
            await publish_and_commit(
                session,
                LLMFallbackTriggeredEvent.event_type,
                LLMFallbackTriggeredEvent(
                    failed_provider=ctx.failed_provider,
                    next_provider=ctx.next_provider,
                    error_class=ctx.error_class,
                    error_type=ctx.error_type,
                    model_attempted=ctx.model_attempted,
                    model_fallback=ctx.model_fallback,
                    correlation_id=str(ctx.correlation_id),
                ).model_dump(mode="json"),
            )

    return _publish


async def _create_template(
    session_factory: async_sessionmaker[AsyncSession], *, config: dict[str, Any]
) -> Any:
    repo = AgentTemplateRepo(session_factory=session_factory)
    return await repo.create(name=f"tpl-{uuid4()}", archetype="producteur", config=config)


async def _poll_run(
    factory: async_sessionmaker[AsyncSession], run_id: str, *, until: set[str]
) -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    while loop.time() < deadline:
        async with factory() as session:
            result = await session.execute(
                text("SELECT status, checkpoint, metrics FROM workflow_runs WHERE id = :id"),
                {"id": run_id},
            )
            row = result.mappings().one()
        if row["status"] in until:
            return dict(row)
        await asyncio.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"run {run_id} never reached {until} within {_POLL_TIMEOUT_S}s (last: {row})")


async def _outbox_payloads(
    factory: async_sessionmaker[AsyncSession], event_type: str
) -> list[dict[str, Any]]:
    async with factory() as session:
        result = await session.execute(
            text("SELECT payload FROM outbox_events WHERE event_type = :t ORDER BY created_at"),
            {"t": event_type},
        )
        return [dict(row[0]) for row in result.all()]


async def _run_one_node_workflow(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    *,
    providers: dict[str, _RecordingProvider],
    chain: list[str],
    until: set[str],
) -> tuple[str, dict[str, Any]]:
    template = await _create_template(
        app_session_factory,
        config={
            "llm_model": _PRIMARY_MODEL,
            "provider_chain": chain,
            # Zero node retries: this test is about the CHAIN (Story 1.6),
            # not about the node-level retry loop (covered by
            # `test_agent_node.py`). Without this, both-providers-down would
            # take 1 + 3 chain traversals and a real backoff wait.
            "error_policy": {"on_timeout": "fail_fast"},
        },
    )
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers=dict(providers),
        # The PROCESS default is deliberately different from the template's
        # chain: if `provider_chain=` were still not passed (défer D12 not
        # closed), the run would quietly use this instead and the fallback
        # assertions below would be meaningless.
        default_chain=[chain[-1]],
        on_fallback=_make_fallback_publisher(app_session_factory),
    )
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": f"e2e-fallback-{uuid4()}",
                "nodes": [{"node_id": "a", "agent_template_id": str(template.id)}],
                "edges": [],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        run_resp = await client.post(
            f"/api/v1/workflows/{create_resp.json()['workflow_id']}/runs",
            headers=_auth_headers(),
            json={"input": {"q": "hello"}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = str(run_resp.json()["run_id"])
        row = await _poll_run(seed_session_factory, run_id, until=until)
    return run_id, row


@pytest.mark.asyncio
async def test_run_when_primary_provider_is_down_should_complete_on_the_fallback(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC3 — the chain is reached FROM a workflow run (défer D12 closed)."""
    down = _RecordingProvider(
        "anthropic", fails_with=LLMProviderUnavailableError(detail="503 from anthropic")
    )
    healthy = _RecordingProvider("openai")

    _run_id, row = await _run_one_node_workflow(
        app_session_factory,
        seed_session_factory,
        workflow_checkpointer,
        providers={"anthropic": down, "openai": healthy},
        chain=["anthropic", "openai"],
        until={"completed", "error"},
    )

    assert row["status"] == "completed", row
    assert down.call_count == 1
    assert healthy.call_count == 1
    # The completion genuinely came from the fallback provider: the router
    # resolved the OpenAI-equivalent model from `DEFAULT_MODEL_FALLBACK_MAP`
    # rather than re-sending the Anthropic one.
    assert row["metrics"]["per_node"]["a"]["model_used"] == "gpt-5"
    # One full chain traversal, no NODE retry — the fallback happened INSIDE
    # the single `complete()` call (Story 1.6's mechanism, not Story 4.6's).
    assert row["metrics"]["per_node"]["a"]["llm_attempts"] == 1

    fallbacks = await _outbox_payloads(
        seed_session_factory, "workflow_engine.llm.fallback_triggered"
    )
    assert fallbacks, "Story 1.6's fallback event never reached the outbox"
    latest = fallbacks[-1]
    assert latest["failed_provider"] == "anthropic"
    assert latest["next_provider"] == "openai"
    assert latest["error_type"] == "LLMProviderUnavailableError"
    assert latest["model_attempted"] == _PRIMARY_MODEL
    # Joined to the run by correlation_id, never by a `run_id` field — adding
    # one would make `shared/llm` depend on what a workflow run is.
    assert latest["correlation_id"]


@pytest.mark.asyncio
async def test_run_when_every_provider_is_down_should_fail_with_a_usable_breakdown(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC3 — chain exhausted. The run fails (expected), but it now fails
    LEGIBLY: `error_type` for alerting, and the per-attempt breakdown that
    `LLMAllProvidersFailedError` carries, which used to be discarded whole.
    """
    first = _RecordingProvider(
        "anthropic", fails_with=LLMProviderUnavailableError(detail="503 from anthropic")
    )
    second = _RecordingProvider(
        "openai", fails_with=LLMProviderUnavailableError(detail="503 from openai")
    )

    run_id, row = await _run_one_node_workflow(
        app_session_factory,
        seed_session_factory,
        workflow_checkpointer,
        providers={"anthropic": first, "openai": second},
        chain=["anthropic", "openai"],
        until={"completed", "error"},
    )

    assert row["status"] == "error", row
    assert first.call_count == 1
    assert second.call_count == 1

    failed = [
        payload
        for payload in await _outbox_payloads(
            seed_session_factory, "workflow_engine.workflow_run.failed"
        )
        if payload.get("run_id") == run_id
    ]
    assert len(failed) == 1, failed
    # The field an alerting consumer filters on, instead of parsing prose.
    assert failed[0]["error_type"] == "LLMAllProvidersFailedError"

    attempts = row["checkpoint"]["last_error_attempts"]
    assert [entry["provider"] for entry in attempts] == ["anthropic", "openai"]
    assert all(entry["error_class"] != "fatal" for entry in attempts)
    assert all(entry["error_type"] == "LLMProviderUnavailableError" for entry in attempts)


@pytest.mark.asyncio
async def test_run_when_chain_names_an_unregistered_provider_should_still_run(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """THE trap of this story, asserted end to end (préambule point 3).

    `LLMRouter.complete()` raises a bare `ValueError` — classified FATAL, so
    no fallback catches it — for any provider name it does not know. Passing
    a template's declared chain through unfiltered would therefore have
    killed every run in dev, in CI and under testcontainers, where the only
    registered provider is the mock. The intersection is what keeps this
    green.
    """
    healthy = _RecordingProvider("openai")
    # Counted BEFORE and AFTER rather than filtered by run: a fallback event
    # carries `correlation_id`, never `run_id` (adding one would make
    # `shared/llm` know what a workflow run is), and `postgres_container` is
    # session-scoped, so the table already holds other tests' events.
    fallbacks_before = len(
        await _outbox_payloads(seed_session_factory, "workflow_engine.llm.fallback_triggered")
    )

    _run_id, row = await _run_one_node_workflow(
        app_session_factory,
        seed_session_factory,
        workflow_checkpointer,
        providers={"openai": healthy},
        # `anthropic` is declared but NOT registered in this process.
        chain=["anthropic", "openai"],
        until={"completed", "error"},
    )

    assert row["status"] == "completed", row
    assert healthy.call_count == 1
    # No fallback happened: the unusable leg was filtered out BEFORE the
    # router ever saw it, rather than being attempted and failing mid-chain.
    fallbacks_after = len(
        await _outbox_payloads(seed_session_factory, "workflow_engine.llm.fallback_triggered")
    )
    assert fallbacks_after == fallbacks_before
