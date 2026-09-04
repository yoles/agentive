"""Unit: `_build_service` dependency guards (Story 3.1 T5.2).

Added by the Story 3.1 code review (P12): both 503 branches were prescribed
by the spec and implemented, but nothing protected them from a regression
(every e2e fixture pre-seeds `app.state`, so the guards never ran).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agentive_backend.features.memory_manager.router import _build_service
from agentive_backend.shared.exceptions import DependencyError


def _request(**state: Any) -> Any:
    """A minimal stand-in for `Request` (only `request.app.state` is read)."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def test_missing_session_factory_raises_503() -> None:
    with pytest.raises(DependencyError) as exc:
        _build_service(_request(embedder=object()))
    assert exc.value.status == 503
    assert exc.value.context["missing"] == ["session_factory"]


def test_missing_embedder_raises_503() -> None:
    with pytest.raises(DependencyError) as exc:
        _build_service(_request(session_factory=object()))
    assert exc.value.status == 503
    assert exc.value.context["missing"] == ["embedder"]


def test_fully_wired_state_builds_the_service() -> None:
    from agentive_backend.features.memory_manager.service import MemoryManagerService

    service = _build_service(_request(session_factory=object(), embedder=object()))
    assert isinstance(service, MemoryManagerService)
