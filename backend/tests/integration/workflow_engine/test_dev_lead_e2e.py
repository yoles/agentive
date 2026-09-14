"""End-to-end — Dev Lead, prise de demande et décomposition (Story 5.1).

Postgres réel (testcontainers) + checkpointing LangGraph réel + LLM mocké.
Couvre les trois AC :

* **AC1** — le provisioning (`scripts.seed_dev`) crée le namespace, le
  template `Dev Lead` depuis l'archétype Orchestrateur, et le workflow
  d'entrée ; une seconde exécution ne crée rien.
* **AC2** — `POST /workflows/{id}/runs` rend un `run_id` immédiatement, et la
  PREMIÈRE frame SSE porte l'accusé de réception, en moins de 2 s, y compris
  pour un client qui s'attache après le premier checkpoint.
* **AC3** — les 3 cas de référence (scaffolding / refactoring / bug fix)
  traversent le moteur et rendent un plan conforme au jeu fermé des rôles.

⚠️ Ce que ces tests NE prouvent PAS : la qualité du jugement du modèle. Les
sorties sont pré-écrites (`MockProvider`), donc ils prouvent que le contrat
survit au moteur — pas que le prompt produit une bonne décomposition. Ce
jugement-là est manuel, et son protocole est au runbook
(`docs/runbooks/pole-dev.md`).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from scripts.seed_dev import (
    DEV_LEAD_NODE_ID,
    DEV_LEAD_WORKFLOW_NAME,
    DevDepartmentSeeder,
    SeedReport,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.contracts.dev_roles import validate_delegation_plan
from agentive_backend.shared.correlation import new_correlation_id, set_correlation_id
from agentive_backend.shared.llm.router import LLMRouter
from agentive_backend.shared.llm.testing import MockProvider
from agentive_backend.shared.llm.types import Completion
from agentive_backend.shared.repositories import NamespaceRepo

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app
from .conftest import wire_execution_service, wire_execution_service_with_real_mise_en_place

pytestmark = pytest.mark.integration

_STREAM_TIMEOUT_S = 15.0
#: Le budget de l'AC2, appliqué tel quel.
_FIRST_FRAME_BUDGET_S = 2.0
#: Le 201 n'est PAS sous le budget de l'AC2 (Piège #7), mais il porte la Mise
#: en Place et, depuis cette story, la lecture d'historique de l'accusé. Un
#: plafond large sert de garde-fou de régression : il ne valide pas une
#: promesse, il empêche une dérive silencieuse sur le chemin que John voit.
_POST_BUDGET_S = 5.0
_POLL_TIMEOUT_S = 20.0
_POLL_INTERVAL_S = 0.1


def _plan(*, summary: str, subtasks: list[tuple[str, str, str]]) -> str:
    """Une sortie de Dev Lead bien formée, sérialisée comme le modèle le ferait."""
    return json.dumps(
        {
            "status": "done",
            "summary": summary,
            "plan": [
                {"id": sid, "title": title, "rationale": "parce que"}
                for sid, title, _role in subtasks
            ],
            "delegations": [
                {
                    "subtask_id": sid,
                    "target_role": role,
                    "instruction": title,
                    "rationale": "rôle le plus proche du besoin",
                }
                for sid, title, role in subtasks
            ],
        },
        ensure_ascii=False,
    )


#: Les 3 cas de référence de l'AC3.
_CASES: dict[str, tuple[str, str]] = {
    "scaffolding": (
        "Scaffold le module paiements pour Acme",
        _plan(
            summary="Créer un nouveau module paiements pour le projet Acme",
            subtasks=[
                ("s1", "Relever les patterns des modules existants", "code_researcher"),
                ("s2", "Proposer la structure du module", "architect_analyst"),
                ("s3", "Implémenter le squelette", "code_producer"),
                ("s4", "Écrire les tests", "test_engineer"),
            ],
        ),
    ),
    "refactoring": (
        "Refactorer le module paiements pour extraire la logique de reprise",
        _plan(
            summary="Refactoring du module paiements existant",
            subtasks=[
                ("s1", "Cartographier les dépendances actuelles", "code_researcher"),
                ("s2", "Évaluer l'impact et les risques", "architect_analyst"),
                ("s3", "Appliquer le refactoring", "code_producer"),
                ("s4", "Reviewer le diff", "code_reviewer"),
            ],
        ),
    ),
    "bugfix": (
        "Le webhook paiement renvoie 500 depuis hier soir",
        _plan(
            summary="Corriger une régression du webhook paiement",
            subtasks=[
                ("s1", "Localiser le code du webhook", "code_researcher"),
                ("s2", "Corriger la régression", "code_producer"),
                ("s3", "Ajouter un test de non-régression", "test_engineer"),
            ],
        ),
    ),
}


def _completion(text_: str) -> Completion:
    return Completion(
        text=text_,
        model="mock-model",
        provider="mock",
        input_tokens=120,
        output_tokens=80,
        finish_reason="stop",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0002"),
    )


async def _seed(session_factory: async_sessionmaker[AsyncSession]) -> SeedReport:
    """Le provisioning, hors requête HTTP — d'où le correlation_id explicite
    (posé par le middleware en production, par `scripts.seed_dev.main` en
    ligne de commande)."""
    set_correlation_id(new_correlation_id())
    return await DevDepartmentSeeder(session_factory=session_factory).run()


def _build_app(
    session_factory: async_sessionmaker[AsyncSession],
    checkpointer: Any,
    completions: list[Completion],
) -> Any:
    app = _make_app(session_factory=session_factory)
    app.state.workflow_checkpointer = checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", completions)}, default_chain=["mock"]
    )
    wire_execution_service(app)
    return app


async def _read_first_frame(response: httpx.Response) -> tuple[str, dict[str, Any]]:
    event_name = "message"
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            return event_name, json.loads(line.removeprefix("data:").strip())
    raise AssertionError("le flux SSE s'est fermé sans envoyer une seule frame")


async def _read_frames_until(
    response: httpx.Response, *, event: str, budget: int = 40
) -> tuple[str, dict[str, Any]]:
    """Lit le flux jusqu'à la frame ``event``, ou échoue en NOMMANT ce qu'il a vu.

    ``_read_first_frame`` ne rend que la première frame, qui est toujours un
    ``state`` (``_stream_run_events`` l'envoie à la connexion) : l'event
    ``started`` n'était donc lu par aucun test, et supprimer son
    ``acknowledgement`` ne cassait rien.
    """
    seen: list[str] = []
    event_name = "message"
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            seen.append(event_name)
            if event_name == event:
                return event_name, payload
            if len(seen) >= budget:
                break
    raise AssertionError(f"frame `{event}` jamais reçue — frames vues : {seen}")


async def _wait_for_terminal(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], run_id: str
) -> str:
    """Attend que le run quitte `running`.

    Obligatoire avant de rendre la main : `postgres_container` est de portée
    SESSION, et le balayage de `test_recovery_e2e.py` réclame TOUTE row encore
    `running`, quel que soit le test qui l'a laissée (même raison que dans
    `test_mise_en_place_e2e.py`).
    """
    from sqlalchemy import text

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _POLL_TIMEOUT_S
    while loop.time() < deadline:
        async with session_factory() as session:
            row = await session.execute(
                text("SELECT status FROM workflow_runs WHERE id = :rid"), {"rid": run_id}
            )
            status = row.scalar_one()
        if status != "running":
            return str(status)
        await asyncio.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"run {run_id} toujours `running` après {_POLL_TIMEOUT_S}s")


# ─── AC1 — provisioning ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seeding_provisions_the_namespace_the_template_and_the_entry_workflow(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    report = await _seed(app_session_factory)

    template_id = report.template_ids["dev_lead"]
    assert report.workflow_id is not None

    # Le namespace existe — sans lui, la Mise en Place refuserait chaque run.
    namespace = await NamespaceRepo(session_factory=app_session_factory).get_by_name("dev-metier")
    assert namespace is not None
    assert namespace.type == "metier"

    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get(
            f"/api/v1/agents/templates/{template_id}", headers=_auth_headers()
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        config = body["config"]

    # AC1 nomme DEUX surfaces de vérification ; la seconde n'était exercée
    # nulle part. `tools: []` est l'état assumé de Sprint 2 — l'endpoint doit
    # le DIRE, et c'est ce qui distingue « provisionné sans outils » de
    # « outils jamais considérés ».
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        tools_resp = await client.get(
            f"/api/v1/agents/templates/{template_id}/tools", headers=_auth_headers()
        )
        assert tools_resp.status_code == 200, tools_resp.text
        tools_body = tools_resp.json()
        assert tools_body["template_id"] == str(template_id)
        assert tools_body["assigned_tools"] == []

    assert body["archetype"] == "orchestrateur"
    assert body["name"] == "Dev Lead"
    # AC1 — « son system prompt est spécialisé orchestration Dev ».
    assert "orchestrateur du Pôle Dev" in config["system_prompt"]
    assert "code_researcher" in config["system_prompt"]
    # AC1 — « accès memory namespaces Dev », en configuration.
    assert config["push_memory"] == {"namespace": "dev-metier", "optin": False}
    assert set(config["output_contract"]["core"]) == {"plan", "delegations", "status"}


@pytest.mark.asyncio
async def test_seeding_twice_creates_nothing_new(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T2.3 — même template, même workflow, aucune création, aucun doublon.

    Le piège que ce test ferme : `update_template` INCRÉMENTE `version` sur la
    row existante dès qu'un `system_prompt` change, et la contrainte d'unicité
    porte sur `(name, version, tenant_id)`. Un provisioning qui chercherait
    `(name, version=1)` ne retrouverait plus le template et en créerait un
    second à chaque exécution.
    """
    first = await _seed(app_session_factory)
    second = await _seed(app_session_factory)

    assert second.created == []
    assert second.updated == []
    assert second.template_ids == first.template_ids
    assert second.workflow_id == first.workflow_id
    assert "agent-template Dev Lead" in second.unchanged

    # Les assertions ci-dessus portent sur ce que le seeder dit de LUI-MÊME.
    # « Zéro doublon » est une propriété de la BASE, et c'est la seule chose
    # qui distingue « idempotent » de « se déclare idempotent » — d'autant que
    # ce package n'a volontairement pas de fixture de purge, donc les rows
    # s'accumulent d'un test à l'autre.
    from sqlalchemy import text

    async with app_session_factory() as session:
        templates = await session.execute(
            text("SELECT count(*) FROM agent_templates WHERE name = :name"),
            {"name": "Dev Lead"},
        )
        assert templates.scalar_one() == 1
        workflows = await session.execute(
            text("SELECT count(*) FROM workflows WHERE name = :name"),
            {"name": DEV_LEAD_WORKFLOW_NAME},
        )
        assert workflows.scalar_one() == 1
        namespaces = await session.execute(
            text("SELECT count(*) FROM namespaces WHERE name = :name"),
            {"name": "dev-metier"},
        )
        assert namespaces.scalar_one() == 1


@pytest.mark.asyncio
async def test_the_entry_workflow_mounts_the_dev_lead_template(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy import text

    report = await _seed(app_session_factory)
    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT dag FROM workflows WHERE id = :wid"), {"wid": str(report.workflow_id)}
        )
        dag = row.scalar_one()
    nodes = dag["nodes"] if isinstance(dag, dict) else json.loads(dag)["nodes"]
    assert [node["node_id"] for node in nodes] == [DEV_LEAD_NODE_ID]
    assert nodes[0]["agent_template_id"] == str(report.template_ids["dev_lead"])


# ─── AC2 — prise de demande et accusé de réception ───────────────────


@pytest.mark.asyncio
async def test_a_natural_language_request_is_acknowledged_on_the_first_sse_frame(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC2 — le cœur de la story.

    « Scaffold le module paiements pour Acme » entre par l'API HTTP de
    l'Epic 4 (le Chat est différé, décision du 2026-09-14), le 201 rend un
    `run_id` sans attendre la fin du run, et la PREMIÈRE frame SSE porte
    « Compris. Je mobilise […]. ETA ~X min. » en moins de 2 s.
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]
    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(canned)])

    transport = httpx.ASGITransport(app=app)
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        post_started = loop.time()
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, run_resp.text
        body = run_resp.json()
        run_id = body["run_id"]

        # Piège #7 de la story : le budget de l'AC2 porte sur la première
        # frame APRÈS connexion, pas sur le 201 — mais la latence du 201 doit
        # être « mesurée AUSSI et consignée », parce que c'est ce que John voit
        # réellement en `curl`. Elle ne l'était pas, et cette story a justement
        # AJOUTÉ une lecture DB synchrone sur ce chemin : sans cette mesure,
        # une régression s'y installerait sans qu'aucun test ne bouge.
        post_elapsed = loop.time() - post_started
        assert post_elapsed < _POST_BUDGET_S, (
            f"201 rendu en {post_elapsed:.2f}s — le chemin du POST porte la Mise en Place "
            "ET la lecture d'historique de l'accusé"
        )

        # L'accusé est déjà sur le 201 : le dogfooding de Sprint 2 se fait en
        # client HTTP, pas dans une UI.
        ack = body["acknowledgement"]
        assert ack["message"] == "Compris. Je mobilise Dev Lead. ETA ~1 min."
        assert ack["agents"] == ["Dev Lead"]
        assert ack["eta_minutes"] >= 1

        started = loop.time()

        async def _first() -> tuple[str, dict[str, Any]]:
            async with client.stream(
                "GET",
                f"/api/v1/workflows/runs/{run_id}/events",
                headers=_auth_headers(),
            ) as response:
                assert response.status_code == 200
                return await _read_first_frame(response)

        event_name, payload = await asyncio.wait_for(_first(), timeout=_STREAM_TIMEOUT_S)
        elapsed = loop.time() - started

        assert event_name == "state"
        assert payload["acknowledgement"] == ack
        assert elapsed < _FIRST_FRAME_BUDGET_S, f"première frame en {elapsed:.2f}s"

        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"


@pytest.mark.asyncio
async def test_the_acknowledgement_survives_the_checkpoint_rewrite(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """T7.2 — le test qui attrape la régression que la colonne dédiée prévient.

    `_sync_checkpoint` REMPLACE `workflow_runs.checkpoint` en entier dès que
    le premier node atterrit. Un accusé de réception logé là aurait disparu
    exactement au moment où le rattrapage sert : un client qui s'attache après
    le premier node.
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES["bugfix"]
    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(canned)])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        run_id = run_resp.json()["run_id"]
        expected = run_resp.json()["acknowledgement"]

        # On attend que le node soit passé — donc que `checkpoint` ait été
        # réécrit — AVANT de s'attacher.
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"

        async with client.stream(
            "GET", f"/api/v1/workflows/runs/{run_id}/events", headers=_auth_headers()
        ) as response:
            event_name, payload = await _read_first_frame(response)

    assert event_name == "state"
    assert payload["status"] == "completed"
    assert payload["acknowledgement"] == expected


@pytest.mark.asyncio
async def test_the_acknowledgement_is_built_without_calling_the_llm(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC2/T4.5 — garantie structurelle, pas seulement mesurée.

    Le `MockProvider` ne porte qu'UNE réponse : celle du node. Si le calcul de
    l'accusé appelait le modèle, il la consommerait et le node échouerait sur
    une file vide (`MockProvider` lève quand elle est épuisée).
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES["refactoring"]
    provider = MockProvider("mock", [_completion(canned)])
    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(providers={"mock": provider}, default_chain=["mock"])
    wire_execution_service(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"

    # Exactement un appel : celui du node. Aucun pour l'accusé.
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_the_started_event_carries_the_same_acknowledgement_as_the_201(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """T5.2 — « les deux chemins doivent dire la même chose ».

    Aucun test ne touchait l'event `started` : `_read_first_frame` s'arrête à
    la première frame, et `_stream_run_events` garantit que c'est un `state`.
    Supprimer le kwarg `acknowledgement=` de `WorkflowRunStartedEvent` ne
    cassait donc RIEN — c'est précisément la mutation que la table du Dev
    Agent Record ne contenait pas.

    L'assertion porte sur la row `outbox_events`, PAS sur une frame SSE, et
    c'est délibéré : `publish()` n'écrit que la row, et la diffusion dépend du
    worker. Un run servi par `MockProvider` peut terminer avant que le client
    s'attache, auquel cas l'event `started` n'est jamais diffusé — une
    première version de ce test lisait le flux et échouait une passe sur
    trois. La row, elle, est écrite dans la MÊME transaction que l'INSERT du
    run : c'est le fait que T5.2 engage, et il est déterministe.
    """
    from sqlalchemy import text

    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]
    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(canned)])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, run_resp.text
        body = run_resp.json()
        run_id = body["run_id"]

        async with app_session_factory() as session:
            row = await session.execute(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE event_type = :t AND payload->>'run_id' = :r"
                ),
                {"t": "workflow_engine.workflow_run.started", "r": run_id},
            )
            payload = row.scalar_one()

        # UNE valeur, calculée une fois, rendue sur TROIS surfaces.
        assert payload["acknowledgement"] == body["acknowledgement"]
        assert payload["acknowledgement"]["message"].startswith("Compris. Je mobilise")

        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"

        # Et la troisième surface — la colonne, d'où part la frame de
        # rattrapage — porte exactement la même chose.
        async with app_session_factory() as session:
            stored = await session.execute(
                text("SELECT acknowledgement FROM workflow_runs WHERE id = :r"), {"r": run_id}
            )
            assert stored.scalar_one() == body["acknowledgement"]


@pytest.mark.asyncio
async def test_a_run_reads_its_eta_from_the_measured_history_of_past_runs(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """T4.3 — le chemin `eta_source="history"`, qui n'était asserté nulle part.

    `_build_acknowledgement` est le SEUL endroit où la lecture DB, le réglage
    et le cléage `node_id` ↔ `metrics.per_node` se rencontrent, et aucun test
    ne le touchait : les tests unitaires passent `node_ids` à la main, et les
    E2E n'assertaient jamais `eta_source`. Les deux branches rendant
    « ETA ~1 min » sur ces fixtures, rien ne les distinguait. Si les clés
    cessaient de correspondre, TOUS les ETA basculeraient en `heuristic` —
    définitivement, silencieusement, suite verte.

    L'historique est posé DIRECTEMENT en base plutôt que produit par un
    premier run, et ce n'est pas un raccourci : `agent_node` calcule
    `duration_ms = int((time.monotonic() - started) * 1000)`, donc un node
    servi par `MockProvider` termine sous la milliseconde et écrit `0` —
    valeur que `node_duration_samples` écarte (`duration_ms <= 0`). Un vrai
    node LLM met des secondes ; faire dépendre ce test de la vitesse du mock
    prouverait la vitesse du mock. Ce qui est vérifié ici est la seule chose
    qui puisse casser en production : que la clé écrite dans
    `metrics.per_node` est bien celle que `_build_acknowledgement` cherche.
    """
    from sqlalchemy import text

    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    # 180 s sur LE node du DAG — une valeur qu'aucun repli ne peut produire
    # (le défaut est de 60 s), donc l'ETA rendue prouve la LECTURE.
    async with app_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_runs (workflow_id, status, correlation_id, metrics, "
                "started_at, ended_at) VALUES (:wf, 'completed', :cid, CAST(:m AS jsonb), "
                "now(), now())"
            ),
            {
                "wf": str(report.workflow_id),
                "cid": str(new_correlation_id()),
                "m": json.dumps({"per_node": {DEV_LEAD_NODE_ID: {"duration_ms": 180_000}}}),
            },
        )
        await session.commit()

    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(canned)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, run_resp.text
        ack = run_resp.json()["acknowledgement"]

        assert ack["eta_source"] == "history", (
            "le node du DAG n'a pas retrouvé sa durée mesurée — les clés de "
            "`templates` et de `metrics.per_node` ont divergé"
        )
        # 180 s ⇒ 3 min. Le repli heuristique aurait rendu 1 min.
        assert ack["eta_minutes"] == 3
        assert "ETA ~3 min." in ack["message"]

        run_id = run_resp.json()["run_id"]
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"


@pytest.mark.asyncio
async def test_a_workflow_whose_recent_runs_all_failed_still_finds_its_measured_history(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """Le filtre `status` doit être poussé EN SQL, pas appliqué après coup.

    `limit` tronquait AVANT que `completed` ne soit filtré : vingt runs en
    échec récents suffisaient à masquer un historique mesuré arbitrairement
    long, et l'accusé retombait sur son heuristique en annonçant n'avoir
    aucune mesure. Ici le run mesuré est le plus ANCIEN, derrière assez
    d'échecs récents pour remplir la fenêtre.
    """
    from sqlalchemy import text

    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    async with app_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO workflow_runs (workflow_id, status, correlation_id, metrics, "
                "started_at, ended_at) VALUES (:wf, 'completed', :cid, CAST(:m AS jsonb), "
                "now() - interval '10 days', now() - interval '10 days')"
            ),
            {
                "wf": str(report.workflow_id),
                "cid": str(new_correlation_id()),
                "m": json.dumps({"per_node": {DEV_LEAD_NODE_ID: {"duration_ms": 180_000}}}),
            },
        )
        # Plus d'échecs récents que `AGENTIVE_ACK_HISTORY_LIMIT` (20).
        for index in range(25):
            await session.execute(
                text(
                    "INSERT INTO workflow_runs (workflow_id, status, correlation_id, metrics, "
                    "started_at, ended_at) VALUES (:wf, 'error', :cid, '{}'::jsonb, "
                    "now() - make_interval(secs => :age), now())"
                ),
                {
                    "wf": str(report.workflow_id),
                    "cid": str(new_correlation_id()),
                    "age": float(index),
                },
            )
        await session.commit()

    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(canned)])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, run_resp.text
        ack = run_resp.json()["acknowledgement"]

        assert ack["eta_source"] == "history", (
            "25 échecs récents ont masqué l'historique mesuré — le filtre de statut "
            "est appliqué APRÈS la troncature"
        )
        assert ack["eta_minutes"] == 3

        run_id = run_resp.json()["run_id"]
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"


@pytest.mark.asyncio
async def test_the_seeded_pole_starts_through_the_real_mise_en_place(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """T2.2 — l'affirmation « prouvé bout en bout », vraiment prouvée.

    Chaque run de ce module passe par `wire_execution_service`, donc par le
    stub `_AlwaysPassMiseEnPlaceService`. Et `test_a_missing_namespace_refuses
    _the_launch`, qui câble bien le service RÉEL, le fait contre un template
    FABRIQUÉ À LA MAIN.

    Rien ne vérifiait donc la seule chose qui compte pour l'ordre
    « namespaces avant templates » : que le `push_memory.namespace` du
    catalogue est bien celui que le seeder a créé, vu par la porte réelle.
    C'était inféré de deux assertions séparées.
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", [_completion(canned)])},
        default_chain=["mock"],
    )
    wire_execution_service_with_real_mise_en_place(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, (
            f"la Mise en Place RÉELLE a refusé le pôle tel que le seed le livre : {run_resp.text}"
        )
        body = run_resp.json()
        namespace_checks = [
            check
            for check in body["mise_en_place"]["checks"]
            if check["code"] == "memory_namespaces_accessible"
        ]
        assert namespace_checks and namespace_checks[0]["passed"] is True

        run_id = body["run_id"]
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"


# ─── AC3 — décomposition ─────────────────────────────────────────────


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.asyncio
async def test_each_reference_case_produces_a_coherent_delegation_plan(
    case: str,
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC3 — scaffolding, refactoring, bug fix.

    Ce que ce test prouve : la sortie du Dev Lead traverse le moteur SANS
    déformation (JSON parsé, pas de repli `raw_output`), et le plan rendu
    respecte le jeu fermé des rôles ainsi que la cohérence plan ↔ délégations.
    Ce qu'il ne prouve pas : que le modèle aurait produit CE plan — cf le
    préambule du module.
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES[case]
    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(canned)])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"

    tup = await workflow_checkpointer.aget_tuple({"configurable": {"thread_id": run_id}})
    assert tup is not None
    node_output = (
        (tup.checkpoint or {}).get("channel_values", {}).get("node_outputs", {})[DEV_LEAD_NODE_ID]
    )

    # Pas de repli `raw_output` : le moteur a bien parsé du JSON.
    assert "raw_output" not in node_output
    assert validate_delegation_plan(node_output) == []
    assert node_output["status"] == "done"

    # Le plan est bien celui du cas, pas une coïncidence de fixture : chaque
    # sous-tâche déclarée est présente et déléguée.
    assert {item["id"] for item in node_output["plan"]} == {
        delegation["subtask_id"] for delegation in node_output["delegations"]
    }


@pytest.mark.asyncio
async def test_an_incoherent_plan_reaching_the_engine_is_reported_not_swallowed(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC3, le côté NÉGATIF — celui qui manquait.

    L'assertion `validate_delegation_plan(node_output) == []` des trois cas de
    référence ne peut pas échouer : la fixture `_plan()` construit `plan` et
    `delegations` en itérant les MÊMES tuples, avec des rôles pris dans
    `DEV_ROLES`. Tout invariant vérifié y est vrai par construction — le test
    passerait si le validateur rendait `[]` inconditionnellement.

    Ici la sortie est délibérément incohérente et traverse le moteur RÉEL :
    rôle hors du jeu fermé, sous-tâche orpheline, délégation vers une
    sous-tâche inexistante. Le contrat doit les NOMMER — « le silence est le
    seul choix interdit » (T6.3).
    """
    report = await _seed(app_session_factory)
    rogue = json.dumps(
        {
            "status": "done",
            "summary": "Un plan que le modèle aurait pu produire un mauvais jour",
            "plan": [
                {"id": "s1", "title": "Explorer", "rationale": "r"},
                {"id": "s2", "title": "Personne ne me prend", "rationale": "r"},
            ],
            "delegations": [
                {"subtask_id": "s1", "target_role": "devops_wizard", "instruction": "i"},
                {"subtask_id": "s99", "target_role": "code_producer", "instruction": "i"},
            ],
        }
    )
    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(rogue)])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": "Peu importe"}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"

    from sqlalchemy import text

    tup = await workflow_checkpointer.aget_tuple({"configurable": {"thread_id": run_id}})
    assert tup is not None
    node_output = (
        (tup.checkpoint or {}).get("channel_values", {}).get("node_outputs", {})[DEV_LEAD_NODE_ID]
    )

    problems = validate_delegation_plan(node_output)
    assert problems != [], "une sortie incohérente a traversé le moteur sans être signalée"
    joined = " | ".join(problems)
    assert "devops_wizard" in joined
    assert "s99" in joined
    assert "s2" in joined

    # T6.3 tranché en revue : le moteur lui-même NOMME ces incohérences, il ne
    # les laisse plus passer en silence. Le constat vit dans `metrics`, pas
    # dans `node_outputs` — la sortie du node est la réponse de l'agent, y
    # injecter un diagnostic du moteur la ferait diverger de son contrat.
    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT metrics FROM workflow_runs WHERE id = :r"), {"r": run_id}
        )
        metrics = row.scalar_one()

    recorded = metrics["per_node"][DEV_LEAD_NODE_ID]["contract_problems"]
    assert recorded == problems


@pytest.mark.asyncio
async def test_a_coherent_plan_records_no_contract_problem(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """La contrepartie : la clé est ABSENTE quand il n'y a rien à dire.

    C'est ce qui fait de sa présence un signal. Une clé toujours là, à `[]`,
    obligerait à lire la valeur pour savoir s'il s'est passé quelque chose.
    """
    from sqlalchemy import text

    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]
    app = _build_app(app_session_factory, workflow_checkpointer, [_completion(canned)])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_resp = await client.post(
            f"/api/v1/workflows/{report.workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": objective}},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["run_id"]
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"

    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT metrics FROM workflow_runs WHERE id = :r"), {"r": run_id}
        )
        metrics = row.scalar_one()

    assert "contract_problems" not in metrics["per_node"][DEV_LEAD_NODE_ID]


# ─── Le prérequis qui dicte l'ordre du provisioning ──────────────────


@pytest.mark.asyncio
async def test_a_missing_namespace_refuses_the_launch(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
) -> None:
    """T2.2 — pourquoi le provisioning crée les namespaces AVANT les templates.

    Un template dont le ``push_memory.namespace`` n'existe pas fait REFUSER
    tout lancement de run par la Mise en Place (Story 4.5 AC2 : échec
    permanent, donc 422, et aucune row ``workflow_runs`` créée). Livrer les
    templates sans leurs namespaces donnerait des agents configurés et
    incapables de démarrer — un échec à l'usage plutôt qu'au provisioning.

    Ce test câble la Mise en Place RÉELLE : le stub par défaut du conftest
    laisse tout passer, ce qui en fait le mauvais outil pour tester la porte
    elle-même.
    """
    from uuid import uuid4

    from agentive_backend.shared.repositories import AgentTemplateRepo

    absent = f"namespace-absent-{uuid4()}"
    template = await AgentTemplateRepo(session_factory=app_session_factory).create(
        name=f"Dev Lead sans namespace {uuid4()}",
        archetype="orchestrateur",
        config={"system_prompt": "x", "push_memory": {"namespace": absent, "optin": False}},
    )

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", [])}, default_chain=["mock"]
    )
    wire_execution_service_with_real_mise_en_place(app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/api/v1/workflows",
            headers=_auth_headers(),
            json={
                "name": f"dev-lead-sans-namespace-{uuid4()}",
                "nodes": [{"node_id": "dev_lead", "agent_template_id": str(template.id)}],
                "edges": [],
            },
        )
        workflow_id = create_resp.json()["workflow_id"]
        run_resp = await client.post(
            f"/api/v1/workflows/{workflow_id}/runs",
            headers=_auth_headers(),
            json={"input": {"objective": "peu importe"}},
        )

    assert run_resp.status_code == 422, run_resp.text
    body = run_resp.text
    assert absent in body
    # Le message dit quoi faire, pas seulement que ça a échoué.
    assert "Créer le namespace" in body
