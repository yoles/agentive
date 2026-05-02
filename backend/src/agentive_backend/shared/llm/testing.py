"""Test helpers for the LLM layer — TEST-ONLY.

.. DANGER:: NEVER instantiate :class:`MockProvider` from production code.
   It is exported from ``agentive_backend.shared.llm.testing`` (a
   sub-module) so callers must opt in explicitly with
   ``from agentive_backend.shared.llm.testing import MockProvider``.
   The package-level ``__init__`` does NOT re-export it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from agentive_backend.shared.llm.types import ChatMessage, Completion


class MockProvider:
    """FIFO replay provider — drives router tests without network calls.

    Each ``complete`` (or ``raw_provider_call``) consumes the next entry
    of ``responses``. ``Completion`` entries are returned; ``Exception``
    entries are raised. Once the queue is exhausted, further calls raise
    :class:`ValueError` so test misconfigurations surface loudly.
    """

    provider_name: ClassVar[str] = "mock"  # overridden per-instance below

    def __init__(
        self,
        name: str,
        responses: Sequence[Completion | Exception],
        *,
        infinite_default: Completion | None = None,
    ) -> None:
        self.provider_name = name  # type: ignore[misc]  # shadow ClassVar with instance attr
        self._responses: list[Completion | Exception] = list(responses)
        self._calls: list[dict[str, Any]] = []
        # When set, ``_consume`` returns this value forever once the FIFO
        # queue is exhausted instead of raising ``ValueError``. Used by
        # the dev-mode synthesized mock in :mod:`agentive_backend.app.lifespan`
        # so the app keeps booting after 1024 dev-time calls (review fix-batch P20).
        self._infinite_default = infinite_default

    @property
    def calls(self) -> list[dict[str, Any]]:
        """Return a snapshot of the kwargs each call received (for assertions)."""
        return list(self._calls)

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
        self._calls.append(
            {
                "method": "complete",
                "messages": list(messages),
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "stop": list(stop) if stop is not None else None,
                "timeout_s": timeout_s,
            }
        )
        return self._consume()

    async def raw_provider_call(self, **kwargs: Any) -> Any:
        self._calls.append({"method": "raw_provider_call", **kwargs})
        return self._consume()

    def _consume(self) -> Completion:
        if not self._responses:
            if self._infinite_default is not None:
                return self._infinite_default
            raise ValueError(
                f"MockProvider({self.provider_name!r}) exhausted — "
                "more calls were made than queued responses"
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
