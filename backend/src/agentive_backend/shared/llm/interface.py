"""LLM provider Protocols — the contracts every adapter must satisfy.

Two segregated ports (ISP — audit §2.4)
---------------------------------------
The provider surface is split into two orthogonal Protocols so a consumer
depends only on the method it actually uses (Interface Segregation Principle):

:class:`Completer` — :meth:`Completer.complete`
    Provider-agnostic chat completion. Same signature across Anthropic,
    OpenAI, and any future provider. Returns a normalized
    :class:`agentive_backend.shared.llm.types.Completion`. This is what the
    ~90 % of consumers that just want text completion should depend on.

:class:`RawProviderAccess` — :meth:`RawProviderAccess.raw_provider_call`
    Escape hatch for provider-specific features that must NOT leak into
    the standard interface — Anthropic prompt caching (``cache_control``),
    OpenAI parallel tool calls, JSON mode, vision input, etc. Returns the
    SDK-native object without normalization. Callers opting into this
    method accept the lock-in tradeoff.

:class:`LLMProvider` composes both — the full contract every concrete adapter
implements and the router registers. Consumers that need only completion
depend on :class:`Completer`; only the raw escape-hatch path depends on
:class:`RawProviderAccess`.

Why a Protocol, not an ABC
--------------------------
- Structural typing matches the duck-typed behaviour we need.
- No need to inherit from a base class — adapters can live in any module.
- ``runtime_checkable`` lets the router validate at registration time
  without enforcing inheritance.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Protocol, runtime_checkable

from agentive_backend.shared.llm.types import ChatMessage, Completion


@runtime_checkable
class Completer(Protocol):
    """Narrow port — provider-agnostic chat completion only."""

    provider_name: ClassVar[str]
    """Stable lowercase identifier — ``"anthropic"``, ``"openai"``, ``"mock"``."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str,
        max_tokens: int,
        temperature: float = 0.7,
        system: str | None = None,
        stop: Sequence[str] | None = None,
        timeout_s: float = 30.0,
    ) -> Completion:
        """Return a normalized :class:`Completion` — same shape across providers.

        Caller responsibility for NFR9 (no API keys in logs/traces)
        ----------------------------------------------------------
        DO NOT pass API keys, OAuth tokens, or other secrets in
        ``messages[*].content`` or in ``system``. The redaction processor
        running in the structlog pipeline only catches well-known prefixes
        (``sk-ant-``, ``sk-proj-``, ``sk-``, ``pa-``); arbitrary secrets
        in user-controlled content escape unredacted into completion text
        and downstream events. If an upstream tool output contains a
        secret, the caller MUST redact it before passing it through this
        method. Anthropic / OpenAI providers can echo prompts back inside
        their error messages, which would propagate the leak.
        """
        ...


@runtime_checkable
class RawProviderAccess(Protocol):
    """Narrow port — the SDK-native escape hatch only."""

    async def raw_provider_call(self, **provider_specific_kwargs: Any) -> Any:
        """Return the SDK-native response — caller owns the format.

        Use this for provider-specific features (Anthropic ``cache_control``,
        OpenAI ``parallel_tool_calls``, …) that would dilute the standard
        interface.
        """
        ...


@runtime_checkable
class LLMProvider(Completer, RawProviderAccess, Protocol):
    """Full contract for a concrete LLM provider adapter — ``complete`` +
    the raw escape hatch. Composition of :class:`Completer` and
    :class:`RawProviderAccess`; adapters satisfy it structurally."""
