"""Unit tests — :class:`MiseEnPlaceService.run_checks` (Story 4.5 T8.2).

Mock-driven (``AgentTemplateToolRepo``/``ToolServerRepo``/``NamespaceRepo``/
``DryRunService`` mocked) — mirrors ``tests/unit/workflow_engine/
test_dry_run_service.py``. The Postgres-real + real-MCP-server integration
path lives in ``tests/integration/workflow_engine/test_mise_en_place_e2e.py``
(T8.5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from agentive_backend.features.workflow_engine.mise_en_place import (
    MiseEnPlaceService,
    MiseEnPlaceSettings,
)
from agentive_backend.infra.mcp.client import MCPDiscoveryTimeoutError
from agentive_backend.shared.exceptions import NotFoundError


def _settings(
    *,
    tool_ping_timeout_s: float = 3.0,
    check_timeout_s: float = 15.0,
    budget_cap_usd: Decimal | None = None,
    anthropic_api_key_present: bool = True,
    openai_api_key_present: bool = True,
    mock_llm_provider_active: bool = False,
) -> MiseEnPlaceSettings:
    return MiseEnPlaceSettings(
        tool_ping_timeout_s=tool_ping_timeout_s,
        check_timeout_s=check_timeout_s,
        budget_cap_usd=budget_cap_usd,
        anthropic_api_key_present=anthropic_api_key_present,
        openai_api_key_present=openai_api_key_present,
        mock_llm_provider_active=mock_llm_provider_active,
    )


def _template(
    *,
    template_id: UUID | None = None,
    llm_model: str | None = None,
    namespace: str | None = None,
    archetype: str = "producteur",
    optin: bool | None = None,
    provider_chain: list[str] | None = None,
) -> SimpleNamespace:
    """`archetype`/`optin` mirror the runtime Push Memory gate: only a
    Contrôleur needs `optin=True` for its namespace to be live (Story 3.5
    AC2). Default `producteur` keeps every pre-existing test on the
    "namespace is live" path."""
    config: dict[str, Any] = {}
    if llm_model is not None:
        config["llm_model"] = llm_model
    if provider_chain is not None:
        config["provider_chain"] = provider_chain
    if namespace is not None:
        push_memory: dict[str, Any] = {"namespace": namespace}
        if optin is not None:
            push_memory["optin"] = optin
        config["push_memory"] = push_memory
    return SimpleNamespace(id=template_id or uuid4(), config=config, archetype=archetype)


def _tool(*, server_id: UUID, name: str = "my-tool") -> SimpleNamespace:
    return SimpleNamespace(server_id=server_id, name=name)


def _discovered(*names: str) -> list[SimpleNamespace]:
    """What `discover_tools` returns — only `.name` is read by the check."""
    return [SimpleNamespace(name=n) for n in names]


def _server(*, server_id: UUID | None = None, name: str = "srv") -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id or uuid4(),
        name=name,
        transport="stdio",
        connection_config={"command": "true", "args": []},
    )


def _dry_run_response(*, cost_estimate_usd: str | None) -> SimpleNamespace:
    return SimpleNamespace(cost_estimate_usd=cost_estimate_usd)


def _make_service(
    *,
    settings: MiseEnPlaceSettings | None = None,
    tools_by_template: dict[UUID, list[tuple[SimpleNamespace, Any]]] | None = None,
    servers_by_id: dict[UUID, SimpleNamespace] | None = None,
) -> tuple[MiseEnPlaceService, AsyncMock, AsyncMock, AsyncMock, AsyncMock]:
    """Returns (service, template_tool_repo, tool_server_repo, namespace_repo, dry_run_service)."""
    tools_by_template = tools_by_template or {}
    servers_by_id = servers_by_id or {}

    @asynccontextmanager
    async def _with_tenant(_tenant_id: Any) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace()

    template_tool_repo = AsyncMock()
    template_tool_repo.with_tenant = _with_tenant

    async def _list_by_template(_session: Any, template_id: UUID) -> list[tuple[Any, Any]]:
        return tools_by_template.get(template_id, [])

    template_tool_repo.list_by_template_in_session = AsyncMock(side_effect=_list_by_template)

    tool_server_repo = AsyncMock()

    async def _require_server(_session: Any, server_id: UUID) -> SimpleNamespace:
        return servers_by_id[server_id]

    tool_server_repo.require_by_id_in_session = AsyncMock(side_effect=_require_server)

    namespace_repo = AsyncMock()
    dry_run_service = AsyncMock()
    dry_run_service.dry_run = AsyncMock(return_value=_dry_run_response(cost_estimate_usd=None))

    service = MiseEnPlaceService(
        template_tool_repo=template_tool_repo,
        tool_server_repo=tool_server_repo,
        namespace_repo=namespace_repo,
        dry_run_service=dry_run_service,
        settings=settings or _settings(),
    )
    return service, template_tool_repo, tool_server_repo, namespace_repo, dry_run_service


# ─── mcp_tools_reachable ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_tools_passes_when_no_tool_assigned() -> None:
    service, *_ = _make_service()
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template()},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_tools_passes_when_assigned_server_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid4()
    server_id = uuid4()
    server = _server(server_id=server_id, name="my-server")
    service, *_ = _make_service(
        tools_by_template={template_id: [(_tool(server_id=server_id), None)]},
        servers_by_id={server_id: server},
    )

    import agentive_backend.features.workflow_engine.mise_en_place as mep_module

    monkeypatch.setattr(
        mep_module, "discover_tools", AsyncMock(return_value=_discovered("my-tool"))
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_id)},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_tools_fails_when_server_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    template_id = uuid4()
    server_id = uuid4()
    server = _server(server_id=server_id, name="flaky-server")
    service, *_ = _make_service(
        tools_by_template={template_id: [(_tool(server_id=server_id), None)]},
        servers_by_id={server_id: server},
    )

    import agentive_backend.features.workflow_engine.mise_en_place as mep_module

    monkeypatch.setattr(
        mep_module,
        "discover_tools",
        AsyncMock(side_effect=MCPDiscoveryTimeoutError(timeout=3.0)),
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_id)},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is False
    assert "flaky-server" in check.detail
    assert check.suggested_action is not None
    assert "flaky-server" in check.suggested_action


@pytest.mark.asyncio
async def test_check_tools_dedupes_by_server_across_templates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two templates whose tools both point at the same MCP server must
    ping it exactly once (AC1's "dédupliquer par server_id")."""
    template_a, template_b = uuid4(), uuid4()
    server_id = uuid4()
    server = _server(server_id=server_id, name="shared-server")
    service, *_ = _make_service(
        tools_by_template={
            template_a: [(_tool(server_id=server_id), None)],
            template_b: [(_tool(server_id=server_id), None)],
        },
        servers_by_id={server_id: server},
    )

    import agentive_backend.features.workflow_engine.mise_en_place as mep_module

    discover_mock = AsyncMock(return_value=_discovered("my-tool"))
    monkeypatch.setattr(mep_module, "discover_tools", discover_mock)

    await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_a), "b": _template(template_id=template_b)},
        task_input={},
        tenant_id=None,
    )
    assert discover_mock.await_count == 1


@pytest.mark.asyncio
async def test_check_tools_when_server_no_longer_exposes_assigned_tool_should_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe's result used to be discarded: a reachable server that had
    dropped the assigned tool passed a check named `mcp_tools_reachable`
    (review BS3)."""
    template_id = uuid4()
    server_id = uuid4()
    service, *_ = _make_service(
        tools_by_template={template_id: [(_tool(server_id=server_id, name="search"), None)]},
        servers_by_id={server_id: _server(server_id=server_id, name="my-server")},
    )

    import agentive_backend.features.workflow_engine.mise_en_place as mep_module

    # Reachable, answers discovery — but `search` is gone.
    monkeypatch.setattr(
        mep_module, "discover_tools", AsyncMock(return_value=_discovered("something-else"))
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_id)},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is False
    assert "my-server/search" in check.detail
    assert check.suggested_action is not None


@pytest.mark.asyncio
async def test_check_tools_when_server_exposes_nothing_should_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty discovery list was the starkest form of the bug: zero tools
    returned, check green."""
    template_id = uuid4()
    server_id = uuid4()
    service, *_ = _make_service(
        tools_by_template={template_id: [(_tool(server_id=server_id, name="search"), None)]},
        servers_by_id={server_id: _server(server_id=server_id, name="empty-server")},
    )

    import agentive_backend.features.workflow_engine.mise_en_place as mep_module

    monkeypatch.setattr(mep_module, "discover_tools", AsyncMock(return_value=[]))

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_id)},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is False
    assert "empty-server/search" in check.detail


@pytest.mark.asyncio
async def test_check_tools_when_server_exposes_extra_tools_should_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the ASSIGNED tools matter — a server exposing more than the
    template uses is perfectly normal and must not fail the check."""
    template_id = uuid4()
    server_id = uuid4()
    service, *_ = _make_service(
        tools_by_template={template_id: [(_tool(server_id=server_id, name="search"), None)]},
        servers_by_id={server_id: _server(server_id=server_id, name="rich-server")},
    )

    import agentive_backend.features.workflow_engine.mise_en_place as mep_module

    monkeypatch.setattr(
        mep_module,
        "discover_tools",
        AsyncMock(return_value=_discovered("search", "write", "delete")),
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_id)},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is True


# ─── memory_namespaces_accessible ───────────────────────────────────────


@pytest.mark.asyncio
async def test_check_namespaces_passes_when_none_referenced() -> None:
    service, _ttr, _tsr, namespace_repo, _drs = _make_service()
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template()},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "memory_namespaces_accessible")
    assert check.passed is True
    namespace_repo.require_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_namespaces_passes_when_namespace_exists() -> None:
    service, _ttr, _tsr, namespace_repo, _drs = _make_service()
    namespace_repo.require_by_name = AsyncMock(return_value=SimpleNamespace(name="client-x"))

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(namespace="client-x")},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "memory_namespaces_accessible")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_namespaces_fails_when_namespace_missing() -> None:
    service, _ttr, _tsr, namespace_repo, _drs = _make_service()
    namespace_repo.require_by_name = AsyncMock(
        side_effect=NotFoundError(detail="Namespace 'ghost' not found")
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(namespace="ghost")},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "memory_namespaces_accessible")
    assert check.passed is False
    assert "ghost" in check.detail
    assert check.suggested_action is not None
    assert "ghost" in check.suggested_action


@pytest.mark.asyncio
async def test_check_namespaces_when_controleur_did_not_opt_in_should_skip_its_namespace() -> None:
    """A Contrôleur without `optin` never opens its namespace at runtime
    (Story 3.5 AC2), so blocking the launch on that namespace's existence is
    a false positive (review BS1)."""
    service, _ttr, _tsr, namespace_repo, _drs = _make_service()
    namespace_repo.require_by_name = AsyncMock(
        side_effect=NotFoundError(detail="Namespace 'ghost' not found")
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(namespace="ghost", archetype="controleur", optin=False)},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "memory_namespaces_accessible")
    assert check.passed is True
    namespace_repo.require_by_name.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_namespaces_when_controleur_opted_in_should_check_its_namespace() -> None:
    service, _ttr, _tsr, namespace_repo, _drs = _make_service()
    namespace_repo.require_by_name = AsyncMock(
        side_effect=NotFoundError(detail="Namespace 'ghost' not found")
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(namespace="ghost", archetype="controleur", optin=True)},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "memory_namespaces_accessible")
    assert check.passed is False
    assert "ghost" in check.detail


@pytest.mark.asyncio
async def test_check_namespaces_when_non_controleur_has_optin_false_should_still_check() -> None:
    """`optin` is a Contrôleur-only opt-out. Everywhere else it is IGNORED
    and `{"namespace": "x", "optin": false}` means ENABLED — reading it as a
    global off-switch would silently stop verifying most namespaces."""
    service, _ttr, _tsr, namespace_repo, _drs = _make_service()
    namespace_repo.require_by_name = AsyncMock(
        side_effect=NotFoundError(detail="Namespace 'ghost' not found")
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(namespace="ghost", archetype="producteur", optin=False)},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "memory_namespaces_accessible")
    assert check.passed is False


@pytest.mark.asyncio
async def test_check_namespaces_when_controleur_optin_is_a_string_should_skip() -> None:
    """`is not True`, not `not optin`: `bool("false")` is True, the exact
    inversion Story 3.5's own review (P6) fixed at the runtime site. The two
    predicates must agree."""
    service, _ttr, _tsr, namespace_repo, _drs = _make_service()
    namespace_repo.require_by_name = AsyncMock(
        side_effect=NotFoundError(detail="Namespace 'ghost' not found")
    )
    template = _template(namespace="ghost", archetype="controleur")
    template.config["push_memory"]["optin"] = "false"

    report = await service.run_checks(
        workflow_id=uuid4(), templates={"a": template}, task_input={}, tenant_id=None
    )

    check = next(c for c in report.checks if c.code == "memory_namespaces_accessible")
    assert check.passed is True
    namespace_repo.require_by_name.assert_not_awaited()


# ─── budget_available ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_budget_always_passes_when_cap_unset() -> None:
    service, _ttr, _tsr, _nsr, dry_run_service = _make_service(
        settings=_settings(budget_cap_usd=None)
    )
    dry_run_service.dry_run.return_value = _dry_run_response(cost_estimate_usd="999999.0")

    report = await service.run_checks(
        workflow_id=uuid4(), templates={}, task_input={}, tenant_id=None
    )
    check = next(c for c in report.checks if c.code == "budget_available")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_budget_always_passes_when_cost_estimate_unresolved() -> None:
    service, _ttr, _tsr, _nsr, dry_run_service = _make_service(
        settings=_settings(budget_cap_usd=Decimal("1"))
    )
    dry_run_service.dry_run.return_value = _dry_run_response(cost_estimate_usd=None)

    report = await service.run_checks(
        workflow_id=uuid4(), templates={}, task_input={}, tenant_id=None
    )
    check = next(c for c in report.checks if c.code == "budget_available")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_budget_passes_when_cost_within_cap() -> None:
    service, _ttr, _tsr, _nsr, dry_run_service = _make_service(
        settings=_settings(budget_cap_usd=Decimal("50"))
    )
    dry_run_service.dry_run.return_value = _dry_run_response(cost_estimate_usd="10.00")

    report = await service.run_checks(
        workflow_id=uuid4(), templates={}, task_input={}, tenant_id=None
    )
    check = next(c for c in report.checks if c.code == "budget_available")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_budget_fails_when_cost_exceeds_cap() -> None:
    service, _ttr, _tsr, _nsr, dry_run_service = _make_service(
        settings=_settings(budget_cap_usd=Decimal("5"))
    )
    dry_run_service.dry_run.return_value = _dry_run_response(cost_estimate_usd="10.00")

    report = await service.run_checks(
        workflow_id=uuid4(), templates={}, task_input={}, tenant_id=None
    )
    check = next(c for c in report.checks if c.code == "budget_available")
    assert check.passed is False
    assert check.suggested_action is not None
    assert "AGENTIVE_DRY_RUN_BUDGET_CAP_USD" in check.suggested_action


@pytest.mark.asyncio
async def test_check_budget_passes_dry_run_service_the_same_task_input() -> None:
    # A cap MUST be configured: with none, `_check_budget` now short-circuits
    # before calling the Dry Run at all (review P5).
    service, *_repos, dry_run_service = _make_service(
        settings=_settings(budget_cap_usd=Decimal("50"))
    )
    workflow_id = uuid4()

    await service.run_checks(
        workflow_id=workflow_id,
        templates={},
        task_input={"seed": 1},
        tenant_id=None,
    )
    dry_run_service.dry_run.assert_awaited_once_with(
        workflow_id=workflow_id, task_input={"seed": 1}, tenant_id=None
    )


# ─── llm_providers_configured ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_llm_providers_passes_when_key_configured() -> None:
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=False)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6")},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_llm_providers_fails_when_key_missing() -> None:
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=False, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6")},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "anthropic" in check.detail
    assert check.suggested_action is not None
    assert "anthropic" in check.suggested_action


@pytest.mark.asyncio
async def test_check_llm_providers_ignores_model_absent_from_every_pricing_table() -> None:
    """Mirror Dry Run's `model_price_unresolved` posture — an unresolved
    model has no provider to check a key for, never a reason to fail."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=False, openai_api_key_present=False)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="some-unknown-model")},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is True


# ─── Story 4.6 T9.3 — the check must predict the CHAIN, not just the model ───


@pytest.mark.asyncio
async def test_check_llm_providers_when_chain_is_wholly_unavailable_should_fail() -> None:
    """The non-regression this story owes 4.5's fil rouge.

    Before Story 4.6 the runtime ignored `provider_chain` entirely, so
    checking `llm_model` alone was accurate. Now a template declaring
    `provider_chain: ["openai"]` runs on OpenAI whatever its `llm_model`
    says — and this exact shape used to PASS with only an Anthropic key,
    because the check looked at `claude-sonnet-4-6` and saw Anthropic.
    """
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=False)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={
            "a": _template(llm_model="claude-sonnet-4-6", provider_chain=["openai"]),
        },
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "openai" in check.detail


@pytest.mark.asyncio
async def test_check_llm_providers_when_chain_is_partly_available_should_pass() -> None:
    """A partially available chain is a DEGRADATION, not a failure: the
    runtime intersects and runs on the providers that are configured. Failing
    here would block a launch that works fine without its fallback leg —
    the opposite of what NFR12's chain is for."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=False)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={
            "a": _template(llm_model="claude-sonnet-4-6", provider_chain=["anthropic", "openai"]),
        },
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_llm_providers_when_chain_is_malformed_should_name_the_node() -> None:
    """A `provider_chain` the resolver cannot read (a bare string, an empty
    list, a non-string element) is ignored WHOLE, so the node runs on the
    process default chain — against providers its author never chose. That is
    the same class of problem as an incoherent chain, and it used to pass this
    gate without a word: `resolution.malformed` was computed and discarded.

    Reported by NODE id, because that is what the operator has to go and
    edit."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6", provider_chain=[])},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "unreadable provider_chain: a" in check.detail


@pytest.mark.asyncio
async def test_check_llm_providers_when_chain_serves_no_model_should_fail() -> None:
    """Both keys present, every declared provider registered — and the launch
    is still refused, because none of them serves the template's `llm_model`.

    The router hands index 0 the model verbatim, so this run would send
    `claude-sonnet-4-6` to OpenAI: a fatal 400 at the first node, with no
    fallback and no retry. The runtime drops the chain, but that does not
    rescue it (the process default has the same gap) — this check is what
    stops the launch."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6", provider_chain=["openai"])},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "claude-sonnet-4-6" in check.detail


@pytest.mark.asyncio
async def test_check_llm_providers_when_chain_only_needs_reordering_should_pass() -> None:
    """The same shape, but with the model's own provider present in the
    chain: the runtime rotates it to the head and runs normally, so the
    pre-flight must NOT refuse it. A check that predicts what the runtime
    does has to predict the rotation too."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={
            "a": _template(llm_model="claude-sonnet-4-6", provider_chain=["openai", "anthropic"])
        },
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is True


@pytest.mark.asyncio
async def test_check_llm_providers_when_chain_is_partly_available_should_not_claim_a_missing_key() -> (
    None
):
    """The check passes — a degraded chain still runs (decision T9) — but the
    message must not name a provider that has no key.

    It did: `resolved_providers` was fed from `resolution.configured` (every
    DECLARED provider) instead of `resolution.chain` (the ones the run will
    actually use), so the success line read "Every resolved LLM provider has
    a configured API key: anthropic, openai" with no OpenAI key anywhere. The
    check stated the opposite of what it had just measured."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=False)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={
            "a": _template(llm_model="claude-sonnet-4-6", provider_chain=["anthropic", "openai"])
        },
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is True
    configured_part, _, degraded_part = check.detail.partition("Degraded:")
    assert "openai" not in configured_part
    # …and the degradation is stated rather than concealed.
    assert "openai" in degraded_part


@pytest.mark.asyncio
async def test_check_llm_providers_when_the_models_provider_lacks_a_key_should_say_so() -> None:
    """`anthropic` IS in the declared chain — it was dropped for want of a
    key. Reporting that as "your provider_chain serves none of your llm_model"
    sent the operator to edit a template that is perfectly correct, while the
    real fix (set ANTHROPIC_API_KEY) went unmentioned. `resolution.dropped`
    held the answer and this branch discarded it."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=False, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={
            "a": _template(llm_model="claude-sonnet-4-6", provider_chain=["anthropic", "openai"])
        },
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "Missing API key" in check.detail
    assert "anthropic" in check.detail
    assert "serves none" not in check.detail


@pytest.mark.asyncio
async def test_check_llm_providers_when_several_things_are_wrong_should_report_them_all() -> None:
    """Three separate `return`s meant the first problem hid the rest: the
    operator fixed it, relaunched, and discovered the next — two round trips
    for information the check held all along."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=False)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={
            "broken": _template(llm_model="claude-sonnet-4-6", provider_chain=[]),
            "nokey": _template(llm_model="gpt-5"),
        },
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "Missing API key" in check.detail and "openai" in check.detail
    assert "unreadable provider_chain: broken" in check.detail


@pytest.mark.asyncio
async def test_check_llm_providers_when_a_declared_provider_does_not_exist_should_say_so() -> None:
    """Story 4.6, review lot 6 (P-O) — the one verdict an operator cannot
    act on.

    `resolve_provider_chain` is handed only the AVAILABLE set, so a name
    absent from it is indistinguishable, THERE, between "real provider, no
    key" and "not a provider at all". Both landed in `dropped`, and this
    check turned the whole tuple into "Missing API key for provider(s):
    mistral" — sending the operator to configure `MISTRAL_API_KEY`, a
    setting that does not exist and never will."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6", provider_chain=["mistral"])},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "unknown provider name: a (mistral)" in check.detail
    # The remediation the operator must NOT be given.
    assert "Missing API key" not in check.detail
    assert check.suggested_action is not None
    assert "anthropic, openai" in check.suggested_action


@pytest.mark.asyncio
async def test_check_llm_providers_when_an_unknown_name_sits_beside_a_usable_one() -> None:
    """Deliberately blocking, unlike the missing-key case beside it.

    An absent key is a legitimate per-environment state — staging runs
    without an OpenAI key on purpose — so a partially-available chain passes
    as `degraded`. A name outside `KNOWN_PROVIDERS` is in no environment's
    interest: the template claims a fallback leg that has never existed, and
    the operator believes they are covered."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={
            "a": _template(llm_model="claude-sonnet-4-6", provider_chain=["anthropic", "mistrl"])
        },
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "unknown provider name: a (mistrl)" in check.detail
    # And it is never described as a missing key, in either verdict half.
    # Matched against the message the code ACTUALLY builds — "Missing API key
    # for provider(s): …" — not against a paraphrase. The first version of
    # this line looked for "API key for mistrl", a substring that string can
    # never contain whatever the code does, so it held before the fix as
    # readily as after (review lot 10, T2).
    assert "mistrl" not in check.detail.split("unknown provider name")[0]
    assert "Missing API key" not in check.detail


# ─── mock-provider mode (review IG1) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_check_llm_providers_when_mock_provider_active_should_pass() -> None:
    """No key configured anywhere means `app.lifespan` wired `MockProvider`
    and the run WILL complete — a missing key predicts no failure, so
    blocking would make dev/CI without secrets impossible (review IG1)."""
    service, *_ = _make_service(
        settings=_settings(
            anthropic_api_key_present=False,
            openai_api_key_present=False,
            mock_llm_provider_active=True,
        )
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6")},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is True
    # Passing must never read as "a real provider was verified".
    assert "mock provider" in check.detail


@pytest.mark.asyncio
async def test_check_llm_providers_when_one_key_is_configured_should_still_block() -> None:
    """The mock fallback only exists when NO key is configured. With a
    partial configuration the router builds the real providers it can, so a
    template pointing at the unconfigured one must still fail — this is the
    direction the IG1 fix must not weaken."""
    service, *_ = _make_service(
        settings=_settings(
            anthropic_api_key_present=True,
            openai_api_key_present=False,
            mock_llm_provider_active=False,
        )
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="gpt-5")},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "openai" in check.detail


# ─── run_checks aggregation ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_checks_always_returns_exactly_four_checks() -> None:
    service, *_ = _make_service()
    report = await service.run_checks(
        workflow_id=uuid4(), templates={}, task_input={}, tenant_id=None
    )
    assert {c.code for c in report.checks} == {
        "mcp_tools_reachable",
        "memory_namespaces_accessible",
        "budget_available",
        "llm_providers_configured",
    }
    assert report.bypassed is False
    assert report.bypass_reason is None


# ─── run_checks never raises (review P1) ─────────────────────────────────


@pytest.mark.asyncio
async def test_run_checks_when_budget_check_raises_should_report_it_as_failed() -> None:
    """`DryRunService.dry_run` raises documented `NotFoundError`/`ValidationError`.
    Before the fix these escaped `run_checks` as the endpoint's HTTP error —
    no report at all, and no `force` escape hatch."""
    # A cap MUST be configured, otherwise `_check_budget` short-circuits
    # before ever calling the Dry Run (review P5) and nothing can raise.
    service, _tt, _ts, _ns, dry_run_service = _make_service(
        settings=_settings(budget_cap_usd=Decimal("50"))
    )
    dry_run_service.dry_run = AsyncMock(side_effect=NotFoundError(detail="workflow gone"))

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6")},
        task_input={},
        tenant_id=None,
    )

    assert len(report.checks) == 4
    check = next(c for c in report.checks if c.code == "budget_available")
    assert check.passed is False
    assert "NotFoundError" in check.detail
    assert check.suggested_action is not None
    assert report.all_passed is False


@pytest.mark.asyncio
async def test_run_checks_when_a_check_raises_should_still_run_the_others() -> None:
    """A broken check must not take its three siblings down with it — the
    other three still report their own real outcome."""
    service, _tt, _ts, namespace_repo, dry_run_service = _make_service(
        settings=_settings(
            anthropic_api_key_present=False,
            openai_api_key_present=False,
            # Needed for `_check_budget` to reach the Dry Run call (review P5).
            budget_cap_usd=Decimal("50"),
        )
    )
    dry_run_service.dry_run = AsyncMock(side_effect=RuntimeError("boom"))
    namespace_repo.require_by_name = AsyncMock(side_effect=NotFoundError(detail="no namespace"))

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(llm_model="claude-sonnet-4-6", namespace="missing-ns")},
        task_input={},
        tenant_id=None,
    )

    by_code = {c.code: c for c in report.checks}
    assert len(by_code) == 4
    assert by_code["budget_available"].passed is False  # raised -> coerced
    assert by_code["memory_namespaces_accessible"].passed is False  # genuinely missing
    assert by_code["llm_providers_configured"].passed is False  # genuinely no key
    assert by_code["mcp_tools_reachable"].passed is True  # nothing assigned


@pytest.mark.asyncio
async def test_run_checks_when_tool_server_lookup_raises_should_report_it_as_failed() -> None:
    """An orphaned `Tool.server_id` used to surface as a 404 'Tool server not
    found' on `POST /runs` — a lie about which resource was missing."""
    template_id = uuid4()
    server_id = uuid4()
    service, _tt, tool_server_repo, *_ = _make_service(
        tools_by_template={template_id: [(_tool(server_id=server_id), None)]},
        servers_by_id={},
    )
    tool_server_repo.require_by_id_in_session = AsyncMock(
        side_effect=NotFoundError(detail="tool server gone")
    )

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_id, llm_model="claude-sonnet-4-6")},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is False
    assert "NotFoundError" in check.detail


# ─── BaseException from the MCP probe (review P4) ────────────────────────


class _BaseLevelFailure(BaseException):
    """A `BaseException` that is NOT an `Exception` — the shape
    `discover_tools` can re-raise inside a `BaseExceptionGroup`."""


@pytest.mark.asyncio
async def test_check_tools_when_probe_raises_base_exception_should_count_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_id = uuid4()
    server_id = uuid4()
    service, *_ = _make_service(
        tools_by_template={template_id: [(_tool(server_id=server_id), None)]},
        servers_by_id={server_id: _server(server_id=server_id, name="cancelled-server")},
    )

    import agentive_backend.features.workflow_engine.mise_en_place as mep_module

    monkeypatch.setattr(mep_module, "discover_tools", AsyncMock(side_effect=_BaseLevelFailure()))

    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template(template_id=template_id, llm_model="claude-sonnet-4-6")},
        task_input={},
        tenant_id=None,
    )

    check = next(c for c in report.checks if c.code == "mcp_tools_reachable")
    assert check.passed is False
    assert "cancelled-server" in check.detail


# ─── default LLM model is checked too (review P2) ────────────────────────


@pytest.mark.asyncio
async def test_check_llm_providers_when_template_has_no_model_should_check_the_default() -> None:
    """A template without `llm_model` runs on `_DEFAULT_LLM_MODEL`
    (`agent_node.py` / `dry_run.py` both apply that fallback), so its
    provider's key must be checked — not skipped."""
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=False, openai_api_key_present=True)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template()},  # no `llm_model` in config
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is False
    assert "anthropic" in check.detail


@pytest.mark.asyncio
async def test_check_llm_providers_when_template_has_no_model_should_pass_with_the_key() -> None:
    service, *_ = _make_service(
        settings=_settings(anthropic_api_key_present=True, openai_api_key_present=False)
    )
    report = await service.run_checks(
        workflow_id=uuid4(),
        templates={"a": _template()},
        task_input={},
        tenant_id=None,
    )
    check = next(c for c in report.checks if c.code == "llm_providers_configured")
    assert check.passed is True
