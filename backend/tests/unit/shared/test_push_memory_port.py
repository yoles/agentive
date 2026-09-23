"""``PushMemoryProvider`` Protocol contract — Story 3.5 T12.1.

Anti-regression on signature drift, mirror ``tests/unit/llm/
test_interface_contract.py`` (the existing pattern for the sibling
``Embedder``/``Completer`` Protocols)."""

from __future__ import annotations

import inspect

from agentive_backend.shared.memory.push_memory import (
    DEFAULT_SIMILARITY_THRESHOLD,
    PushMemoryProvider,
)


def test_default_similarity_threshold_matches_the_epic_constant() -> None:
    assert DEFAULT_SIMILARITY_THRESHOLD == 0.85


def test_relevant_chunks_is_async_with_canonical_signature() -> None:
    sig = inspect.signature(PushMemoryProvider.relevant_chunks)
    assert inspect.iscoroutinefunction(PushMemoryProvider.relevant_chunks)

    parameters = list(sig.parameters.keys())
    assert parameters == ["self", "namespace", "query", "similarity_threshold", "top_k"]


def test_protocol_runtime_checkable() -> None:
    """A class with the right shape passes ``isinstance(_, PushMemoryProvider)``
    structurally, without inheriting from the Protocol (ISP)."""

    class _Conforming:
        async def relevant_chunks(  # type: ignore[no-untyped-def]
            self, *, namespace, query, similarity_threshold, top_k
        ):
            raise NotImplementedError

    assert isinstance(_Conforming(), PushMemoryProvider)


def test_protocol_rejects_a_non_conforming_class() -> None:
    class _NotConforming:
        pass

    assert not isinstance(_NotConforming(), PushMemoryProvider)
