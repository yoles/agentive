"""``GET /api/v1/admin/health/llm`` — provider introspection.

Sprint 0 protection
-------------------
Bearer token auth lands in Story 1.7. Until then, the endpoint accepts
only loopback callers (``127.0.0.1``, ``::1``, ``localhost``,
``host.docker.internal``) — anything else is 403'd. The TODO marker
points to the auth wiring follow-up.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agentive_backend.api.deps import get_llm_router
from agentive_backend.shared.config import settings
from agentive_backend.shared.llm import LLMRouter

router = APIRouter(tags=["admin"])

_LOOPBACK_HOSTS = frozenset(
    {
        "127.0.0.1",
        "::1",
        "localhost",
        "host.docker.internal",
        # Starlette TestClient sets `request.client.host = "testclient"`
        # — accept it so test rigs can exercise the success path.
        "testclient",
    }
)


def _is_local_request(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    host = client.host or ""
    return host in _LOOPBACK_HOSTS


@router.get("/health/llm")
async def llm_health(
    request: Request,
    router_obj: LLMRouter = Depends(get_llm_router),
) -> dict[str, Any]:
    # TODO(Story 1.7) — replace loopback gate with `Authorization: Bearer`
    # auth middleware once it is wired.
    if not _is_local_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="loopback only until auth ships in Story 1.7",
        )

    chain = router_obj.default_chain
    # P17 — distinguish synthesized dev MockProviders from real adapters.
    # An ops caller hitting this endpoint to verify config used to see
    # ``configured: true`` for a placeholder mock — we now expose
    # ``kind`` and only set ``configured`` when the underlying provider
    # is a real adapter.
    return {
        "providers": [
            {
                "name": name,
                "kind": "mock" if provider.provider_name == "mock" else "real",
                "configured": provider.provider_name != "mock",
                "default_chain_position": (chain.index(name) if name in chain else None),
            }
            for name, provider in router_obj.providers.items()
        ],
        "default_chain": list(chain),
        "environment": settings.environment,
    }
