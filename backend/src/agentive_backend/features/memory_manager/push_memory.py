"""``MemoryManagerPushMemoryProvider`` — the concrete adapter for
:class:`~agentive_backend.shared.memory.push_memory.PushMemoryProvider`
(Story 3.5 T6, FR20).

This file deliberately lives in ``features/memory_manager/`` and NOT in
``shared/`` : it carries real business logic (threshold filtering, view
mapping), and ``shared/`` is documented "NO business logic"
(``shared/__init__.py`` line 1). The port itself
(``shared.memory.push_memory.PushMemoryProvider``) stays framework-free ;
this module is the one and only concrete implementation, wired once in
``app/lifespan.py`` onto ``app.state.push_memory_provider``.
"""

from __future__ import annotations

from agentive_backend.features.memory_manager.service import MemoryManagerService
from agentive_backend.shared.contracts.memory import MemorizedChunkView
from agentive_backend.shared.exceptions import (
    DependencyError,
    ForbiddenError,
    InternalError,
    NotFoundError,
)
from agentive_backend.shared.llm.exceptions import LLMError
from agentive_backend.shared.logging import get_logger

_log = get_logger(__name__)


class MemoryManagerPushMemoryProvider:
    """Adapts :meth:`MemoryManagerService.search` to the narrow
    ``PushMemoryProvider`` port — Playground-facing threshold filtering +
    view mapping, degrading to an empty list on any known failure mode."""

    def __init__(self, *, memory_manager_service: MemoryManagerService) -> None:
        self._service = memory_manager_service

    async def relevant_chunks(
        self,
        *,
        namespace: str,
        query: str,
        similarity_threshold: float,
        top_k: int,
    ) -> list[MemorizedChunkView]:
        """Return the chunks of ``namespace`` above ``similarity_threshold``.

        Never raises : a missing/forbidden namespace, a dependency failure,
        or any other internal error of the underlying search is logged at
        warning level and degrades to an empty list — a Push Memory failure
        must never fail the caller's Playground run (Story 3.5 AC1).

        ``require_shared_namespace=True`` (revue IG1) : this reader has no
        department identity to present, so it may only read namespaces
        with no ``department``. A department-scoped namespace is refused
        (``ForbiddenError`` → empty list) : its chunks would end up inside a
        prompt echoed back to whoever triggered the run, so reading one here
        would bypass the Story 3.2 AC2 control that ``POST /memory/search``
        enforces via ``X-Acting-Department``.
        ``acting_department`` stays ``None`` rather than carrying a
        fabricated value : the Playground has no department concept to
        forward, and inventing one would be a fake authorization. Growth
        RBAC (Sprint 4) owns giving internal readers a real identity.
        """
        try:
            results = await self._service.search(
                namespace_name=namespace,
                query=query,
                top_k=top_k,
                rerank=True,
                tenant_id=None,
                acting_department=None,
                include_archived=False,
                require_shared_namespace=True,
            )
            # P4 (revue 3.5) : the mapping belongs INSIDE the try. It ran
            # outside, so a pydantic `ValidationError` on a null namespace
            # or an out-of-range score escaped a method whose contract is
            # "never raises".
            views: list[MemorizedChunkView] = []
            for r in results:
                # P7 (revue 3.5) : clamp BEFORE filtering, so the value that
                # decides inclusion is the value the caller is shown. The
                # clamp is justified as a guard against a future reranker
                # regression, which is precisely the case where the raw and
                # the clamped value differ.
                score = min(1.0, max(0.0, r.score))
                if score <= similarity_threshold:
                    continue
                views.append(
                    MemorizedChunkView(
                        chunk_id=r.chunk_id,
                        namespace=r.namespace,
                        content=r.content,
                        score=score,
                    )
                )
            return views
        except (
            NotFoundError,
            ForbiddenError,
            DependencyError,
            InternalError,
            # P4 : the single most likely failure mode was missing.
            # `MemoryManagerService.search` calls `embed()` unguarded, and
            # every provider error (rate limit, timeout, auth, 400) is an
            # `LLMError(AgentiveError)`, sibling of the four above, not a
            # subclass. An embedder hiccup therefore broke the port's
            # explicit "degrade on embedder down" contract.
            LLMError,
        ) as exc:
            _log.warning(
                "memory_manager.push_memory_search_failed",
                namespace=namespace,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return []
        except Exception as exc:
            # P4 : the enumerated list above is the KNOWN set ; this is what
            # makes "never raises" true for the announced consumers
            # (Workflow Engine, Chat), which will not have the Playground's
            # own `except Exception` net. Logged at exception level because,
            # unlike the branch above, reaching this one is a bug.
            _log.exception(
                "memory_manager.push_memory_search_unexpected_error",
                namespace=namespace,
                error_type=type(exc).__name__,
            )
            return []


__all__ = ["MemoryManagerPushMemoryProvider"]
