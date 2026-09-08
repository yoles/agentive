"""Unit tests — ``MemoryManagerPushMemoryProvider`` (Story 3.5 T12.4).

Mock-driven on ``MemoryManagerService.search`` (``AsyncMock``) : the search
behavior itself (ANN, decay rerank) is already covered by Story 3.1-3.4's
own tests. This adapter is tested in isolation for its two responsibilities:
threshold filtering + view mapping, and degrading to ``[]`` on the
underlying service's known failure modes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agentive_backend.features.memory_manager.push_memory import (
    MemoryManagerPushMemoryProvider,
)
from agentive_backend.features.memory_manager.schemas import MemorySearchResultView
from agentive_backend.shared.exceptions import (
    DependencyError,
    ForbiddenError,
    InternalError,
    NotFoundError,
)
from agentive_backend.shared.llm.exceptions import LLMError


def _result(
    score: float, *, chunk_id=None, namespace: str = "ns", content: str = "x"
) -> MemorySearchResultView:
    return MemorySearchResultView(
        chunk_id=chunk_id or uuid4(),
        content=content,
        score=score,
        similarity=score,
        decay_factor=1.0,
        namespace=namespace,
        created_at=datetime.now(UTC),
    )


def _make_provider(
    *, search_return=None, search_raises=None
) -> tuple[MemoryManagerPushMemoryProvider, AsyncMock]:
    service = AsyncMock()
    if search_raises is not None:
        service.search = AsyncMock(side_effect=search_raises)
    else:
        service.search = AsyncMock(return_value=search_return or [])
    provider = MemoryManagerPushMemoryProvider(memory_manager_service=service)
    return provider, service


@pytest.mark.asyncio
async def test_relevant_chunks_filters_strictly_above_threshold() -> None:
    """T12.4 — `score > threshold`, NOT `>=` : the boundary `score == 0.85`
    is excluded (AC1)."""
    provider, _service = _make_provider(search_return=[_result(0.86), _result(0.85), _result(0.5)])
    chunks = await provider.relevant_chunks(
        namespace="ns", query="q", similarity_threshold=0.85, top_k=20
    )
    assert len(chunks) == 1
    assert chunks[0].score == 0.86


@pytest.mark.asyncio
async def test_relevant_chunks_maps_to_the_narrow_view() -> None:
    chunk_id = uuid4()
    provider, service = _make_provider(
        search_return=[_result(0.9, chunk_id=chunk_id, namespace="ns", content="hello")]
    )
    chunks = await provider.relevant_chunks(
        namespace="ns", query="q", similarity_threshold=0.85, top_k=20
    )
    assert len(chunks) == 1
    view = chunks[0]
    assert view.chunk_id == chunk_id
    assert view.namespace == "ns"
    assert view.content == "hello"
    assert view.score == 0.9
    service.search.assert_awaited_once()
    kwargs = service.search.await_args.kwargs
    assert kwargs["namespace_name"] == "ns"
    assert kwargs["query"] == "q"
    assert kwargs["top_k"] == 20
    assert kwargs["rerank"] is True
    assert kwargs["include_archived"] is False


@pytest.mark.parametrize("exc", [NotFoundError, ForbiddenError, DependencyError, InternalError])
@pytest.mark.asyncio
async def test_relevant_chunks_degrades_to_empty_list_on_known_failures(exc) -> None:
    """T12.4 — a namespace's known failure modes never raise past the
    adapter (AC1 last point : Push Memory must never fail the caller)."""
    provider, _service = _make_provider(search_raises=exc(detail="boom", context={}))
    chunks = await provider.relevant_chunks(
        namespace="ns", query="q", similarity_threshold=0.85, top_k=20
    )
    assert chunks == []


@pytest.mark.asyncio
async def test_relevant_chunks_clamps_score_outside_unit_interval() -> None:
    """T6.2 — defensive clamp, protects only against a future reranker
    regression (score is already guaranteed in [0, 1] by construction)."""
    provider, _service = _make_provider(search_return=[_result(1.5)])
    chunks = await provider.relevant_chunks(
        namespace="ns", query="q", similarity_threshold=0.85, top_k=20
    )
    assert chunks[0].score == 1.0


@pytest.mark.asyncio
async def test_relevant_chunks_empty_when_nothing_matches() -> None:
    provider, _service = _make_provider(search_return=[])
    chunks = await provider.relevant_chunks(
        namespace="ns", query="q", similarity_threshold=0.85, top_k=20
    )
    assert chunks == []


@pytest.mark.asyncio
async def test_relevant_chunks_requires_a_shared_namespace() -> None:
    """IG1 (revue Story 3.5) : this reader has no department identity, so it
    asks the service to refuse any department-scoped namespace. Without this
    flag, pointing a template at another department's namespace was a
    one-line bypass of the Story 3.2 AC2 control that ``POST /memory/search``
    enforces via ``X-Acting-Department``."""
    provider, service = _make_provider(search_return=[_result(0.9)])

    await provider.relevant_chunks(namespace="ns", query="q", similarity_threshold=0.85, top_k=20)

    kwargs = service.search.await_args.kwargs
    assert kwargs["require_shared_namespace"] is True
    # No fabricated department identity is forwarded either: the Playground
    # has no department concept, and inventing one would be a fake
    # authorization rather than an absent one.
    assert kwargs["acting_department"] is None


@pytest.mark.asyncio
async def test_relevant_chunks_degrades_on_embedder_failure() -> None:
    """P4 (revue 3.5) : the single most likely failure mode used to escape.
    ``MemoryManagerService.search`` calls ``embed()`` unguarded, and every
    provider error (rate limit, timeout, auth, 400) is an
    ``LLMError(AgentiveError)``, a SIBLING of the four exceptions this
    adapter enumerated, not a subclass. The port's contract explicitly
    names "embedder down" as a degrade-to-empty case."""
    provider, _service = _make_provider(search_raises=LLMError("rate limited"))

    assert (
        await provider.relevant_chunks(
            namespace="ns", query="q", similarity_threshold=0.85, top_k=20
        )
        == []
    )


@pytest.mark.asyncio
async def test_relevant_chunks_never_raises_on_an_unexpected_error() -> None:
    """P4 : the enumerated list is the KNOWN set. The catch-all is what makes
    the "Never raises" docstring true for the announced consumers (Workflow
    Engine, Chat), which will not have the Playground's own safety net."""
    provider, _service = _make_provider(search_raises=RuntimeError("boom"))

    assert (
        await provider.relevant_chunks(
            namespace="ns", query="q", similarity_threshold=0.85, top_k=20
        )
        == []
    )


@pytest.mark.asyncio
async def test_relevant_chunks_filters_on_the_clamped_score() -> None:
    """P7 (revue 3.5) : the clamp is documented as a guard against a future
    reranker regression, which is exactly the case where the raw and the
    clamped value differ. Filtering on the raw one meant the value deciding
    inclusion was not the value the caller is shown."""
    provider, _service = _make_provider(search_return=[_result(-0.5), _result(1.5)])

    out = await provider.relevant_chunks(
        namespace="ns", query="q", similarity_threshold=0.85, top_k=20
    )

    # -0.5 clamps to 0.0 and is dropped ; 1.5 clamps to 1.0 and is kept.
    assert [v.score for v in out] == [1.0]
