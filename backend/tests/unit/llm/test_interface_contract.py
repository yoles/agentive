"""LLMProvider Protocol contract — anti-regression on signature drift.

Adding a kwarg silently to ``complete()`` would force every adapter to
update simultaneously; these tests fail loudly when the canonical shape
changes so the change is deliberate.
"""

from __future__ import annotations

import inspect
from typing import get_type_hints

from agentive_backend.shared.llm.interface import LLMProvider


def test_complete_method_is_async_with_canonical_signature() -> None:
    sig = inspect.signature(LLMProvider.complete)
    assert inspect.iscoroutinefunction(LLMProvider.complete)

    parameters = list(sig.parameters.keys())
    assert parameters == [
        "self",
        "messages",
        "model",
        "max_tokens",
        "temperature",
        "system",
        "stop",
        "timeout_s",
    ]

    # Default values for the optional kwargs.
    assert sig.parameters["temperature"].default == 0.7
    assert sig.parameters["system"].default is None
    assert sig.parameters["stop"].default is None
    assert sig.parameters["timeout_s"].default == 30.0


def test_raw_provider_call_is_async_with_kwargs_passthrough() -> None:
    sig = inspect.signature(LLMProvider.raw_provider_call)
    assert inspect.iscoroutinefunction(LLMProvider.raw_provider_call)

    # Only `self` is positional; the rest is **kwargs.
    parameters = list(sig.parameters.values())
    assert parameters[0].name == "self"
    assert parameters[1].kind is inspect.Parameter.VAR_KEYWORD


def test_protocol_runtime_checkable() -> None:
    """A class with the right shape passes ``isinstance(_, LLMProvider)``."""

    class _Conforming:
        provider_name = "test"

        async def complete(  # type: ignore[no-untyped-def]
            self,
            messages,
            *,
            model,
            max_tokens,
            temperature=0.7,
            system=None,
            stop=None,
            timeout_s=30.0,
        ):
            raise NotImplementedError

        async def raw_provider_call(self, **kwargs):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    assert isinstance(_Conforming(), LLMProvider)


def test_provider_name_is_class_var() -> None:
    hints = get_type_hints(LLMProvider, include_extras=True)
    # ClassVar is preserved as a string in __annotations__ via get_type_hints.
    assert "provider_name" in hints
