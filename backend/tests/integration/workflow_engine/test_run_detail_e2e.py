"""Integration — `GET /api/v1/workflows/runs/{run_id}` (Story 5.7 AC1/AC2).

Postgres réel. Ce qui se joue ici et nulle part ailleurs : le `404` d'un run
d'un AUTRE tenant, qui n'est pas une comparaison en Python mais la RLS de la
migration initiale — une assertion unitaire ne prouverait rien de cette
propriété-là.

Les tests de bout en bout (un vrai run, la sortie du Chercheur, les chemins
vérifiés sur le disque, la pagination recollée) vivent dans
`test_dev_lead_e2e.py`, à côté du test dont la Story 5.7 T6.3 reprend le
gabarit.

⚠️ Ce package n'a AUCUNE fixture de purge : les tests y partagent l'historique
des runs précédents. Chaque test ci-dessous sème SON run et n'interroge que
lui — aucune assertion de comptage global.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.shared.config import settings

from .conftest import e2e_auth_headers as _auth_headers
from .conftest import make_e2e_app as _make_app

pytestmark = pytest.mark.integration

SeedRun = Callable[..., Awaitable[UUID]]

#: Une row `metrics` de la forme que `_aggregate_metrics` écrit, réduite aux
#: quatre compteurs que l'AC2 de la Story 5.2 nomme mot pour mot.
_METRICS: dict[str, Any] = {
    "total_duration_ms": 4200,
    "total_tokens": {"input": 300, "output": 120},
    "total_cost_usd": "0.0042",
    "per_node": {
        "code_researcher": {
            "duration_ms": 3100,
            "input_tokens": 200,
            "output_tokens": 90,
            "cost_usd": "0.0030",
            "model_used": "mock-model",
            "llm_attempts": 1,
            "tool_calls": 3,
            "tool_loop_iterations": 2,
            "tool_failures": 1,
            "tool_names": ["find_files", "read_file", "read_file"],
        }
    },
}


@pytest.fixture
async def seed_run(
    migrated_db: str,
    seed_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[SeedRun]:
    """Un couple `workflows` + `workflow_runs` avec contrôle explicite des
    colonnes que la route projette — mirror de `seed_run` dans
    `test_retention_repo_e2e.py`.

    Le workflow reste `tenant_id IS NULL` (visible de tous) même quand le run
    porte un tenant : c'est le run qui est le sujet de la garde, et lui donner
    un workflow invisible rendrait le `404` vrai pour la mauvaise raison.

    ⚠️ **L'ensemencement passe par `agentive_test_seed` (RLS contournée), pas
    par `agentive_app` avec un `set_config`.** Ce n'est pas un raccourci de
    confort : la première version le faisait, et elle EMPOISONNAIT le pool.
    Postgres restaure un `SET LOCAL` sur une GUC personnalisée à la CHAÎNE
    VIDE et non à `NULL` après le commit, donc la connexion rendue au pool
    faisait ensuite échouer la policy `tenant_isolation` sur
    `''::uuid` — `invalid input syntax for type uuid: ""` — pour toute requête
    suivante sur cette connexion, y compris celle de la route testée.
    Vérifié à la main en `psql`. Semer hors RLS teste la garde sans la
    contourner : la LECTURE, elle, passe bien par `agentive_app`.
    """
    workflow_ids: list[UUID] = []

    async def _seed(
        *,
        status: str = "completed",
        metrics: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        tenant_id: UUID | None = None,
        checkpoint_purged_at: Any = None,
    ) -> UUID:
        workflow_id = uuid4()
        run_id = uuid4()
        async with seed_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO workflows (id, name, version, dag, status) "
                    "VALUES (:id, :name, 1, CAST(:dag AS jsonb), 'active')"
                ),
                {
                    "id": str(workflow_id),
                    "name": f"run-detail-test-{workflow_id}",
                    "dag": json.dumps({"nodes": [], "edges": []}),
                },
            )
            await session.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(id, workflow_id, status, correlation_id, metrics, checkpoint, "
                    " tenant_id, checkpoint_purged_at, ended_at) "
                    "VALUES (:id, :wf, :status, :cid, CAST(:metrics AS jsonb), "
                    "        CAST(:checkpoint AS jsonb), :tenant, :purged, now())"
                ),
                {
                    "id": str(run_id),
                    "wf": str(workflow_id),
                    "status": status,
                    "cid": str(uuid4()),
                    "metrics": json.dumps(metrics if metrics is not None else {}),
                    "checkpoint": json.dumps(checkpoint) if checkpoint is not None else None,
                    "tenant": str(tenant_id) if tenant_id is not None else None,
                    "purged": checkpoint_purged_at,
                },
            )
            await session.commit()
        workflow_ids.append(workflow_id)
        return run_id

    yield _seed

    async with seed_session_factory() as session:
        # Purge depuis le finalizer, pas la fin du corps : une assertion qui
        # casse laisserait sinon des rows derrière elle dans un package qui
        # n'a aucune fixture de nettoyage (revue 4.15, finding 7).
        for workflow_id in workflow_ids:
            await session.execute(
                text("DELETE FROM workflow_runs WHERE workflow_id = :wf"),
                {"wf": str(workflow_id)},
            )
            await session.execute(
                text("DELETE FROM workflows WHERE id = :wf"), {"wf": str(workflow_id)}
            )
        await session.commit()


def _client(session_factory: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    """Une app SANS checkpointer câblé — l'état par défaut de `make_e2e_app`.

    C'est le cas nominal pour ces tests : ils portent sur la projection de la
    row, pas sur la lecture du fil LangGraph (couverte de bout en bout dans
    `test_dev_lead_e2e.py`). Ils exercent donc aussi, gratuitement, ce qu'une
    réponse dit quand le checkpointer est injoignable.
    """
    app = _make_app(session_factory=session_factory)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ─── AC1 — l'endpoint existe, et ce qu'il refuse ─────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_run_is_a_404_not_an_empty_200(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1, mot pour mot : « jamais un `200` avec un corps vide, qui ne
    distingue pas "pas trouvé" de "rien à dire" »."""
    async with _client(app_session_factory) as client:
        response = await client.get(f"/api/v1/workflows/runs/{uuid4()}", headers=_auth_headers())

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_a_run_of_another_tenant_is_a_404_never_a_403(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """Un `403` confirmerait l'existence de la row à quelqu'un qui n'a pas le
    droit de la voir.

    ⚠️ **Ce que ce test établit exactement** : un run porteur d'un `tenant_id`
    est invisible depuis le contexte par défaut, et l'invisibilité se traduit
    en `404`. Il n'établit PAS que le filtrage vient de la RLS plutôt que
    d'une comparaison en Python — les deux implémentations le feraient passer.
    La docstring d'origine le prétendait (« c'est précisément ce que ce test,
    et lui seul, peut établir »), et elle portait aussi un
    `assert status != 403` **vacue** après `assert status == 404`. Les deux
    sont corrigés : ce qui distingue réellement les deux implémentations est
    la mutation de la policy RLS, et une mutation n'est pas versionnée.

    Ce qui EST verrouillé ici et nulle part ailleurs : le run existe bel et
    bien (il vient d'être semé hors RLS), et la route le traite quand même
    comme inconnu — c'est-à-dire qu'aucun chemin de la route ne divulgue son
    existence, ni par un code, ni par un corps.
    """
    foreign_run = await seed_run(tenant_id=uuid4())

    async with _client(app_session_factory) as client:
        response = await client.get(
            f"/api/v1/workflows/runs/{foreign_run}", headers=_auth_headers()
        )

    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "/errors/not-found"
    # Le corps est celui d'un run INCONNU, mot pour mot : rien n'y distingue
    # « existe mais pas pour vous » de « n'existe pas ».
    assert body["detail"] == f"Workflow run '{foreign_run}' not found"


@pytest.mark.asyncio
async def test_the_tool_counters_of_story_5_2_finally_leave_over_http(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """La surface que l'AC2 de la Story 5.2 nomme mot pour mot, et qui n'était
    interrogeable qu'en SQL jusqu'à cette story."""
    run_id = await seed_run(metrics=_METRICS)

    async with _client(app_session_factory) as client:
        response = await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["status"] == "completed"
    assert body["started_at"] is not None
    assert body["ended_at"] is not None
    researcher = body["metrics"]["per_node"]["code_researcher"]
    assert researcher["tool_calls"] == 3
    assert researcher["tool_loop_iterations"] == 2
    assert researcher["tool_failures"] == 1
    assert researcher["tool_names"] == ["find_files", "read_file", "read_file"]


@pytest.mark.asyncio
async def test_applicative_progress_is_rendered_without_opening_the_stream(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """Les mêmes champs que la frame SSE `state`, pour un appelant en `curl`
    qui n'a jamais ouvert de flux."""
    run_id = await seed_run(
        status="error",
        checkpoint={
            "last_node_id": "code_researcher",
            "node_statuses": {"dev_lead": "success", "code_researcher": "error"},
            "last_error": "provider unavailable",
        },
    )

    async with _client(app_session_factory) as client:
        body = (
            await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())
        ).json()

    assert body["last_node_id"] == "code_researcher"
    assert body["node_statuses"] == {"dev_lead": "success", "code_researcher": "error"}
    assert body["last_error"] == "provider unavailable"


@pytest.mark.asyncio
async def test_a_run_with_no_metrics_renders_an_empty_object_not_a_missing_key(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    run_id = await seed_run()

    async with _client(app_session_factory) as client:
        body = (
            await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())
        ).json()

    assert body["metrics"] == {}
    assert body["mise_en_place"] is None
    assert body["acknowledgement"] is None


# ─── AC2 — d'où vient la sortie, et ce que la réponse en dit ─────────────


@pytest.mark.asyncio
async def test_an_unreachable_checkpointer_says_so_instead_of_reporting_no_output(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """Une panne de lecture et un run stérile rendaient le même corps, et
    c'est le lecteur qui payait la confusion.

    Les deux faits sont désormais dans DEUX champs : `node_outputs_source`
    dit d'où vient ce qu'on lit, `checkpointer_reachable` dit si la source
    faisant autorité a pu être consultée. Les confondre dans une seule valeur
    `unavailable` est ce qui avait conduit à l'assigner sur le mauvais
    critère — la présence d'un aperçu en base plutôt que la cause.
    """
    run_id = await seed_run()

    async with _client(app_session_factory) as client:
        body = (
            await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())
        ).json()

    assert body["checkpointer_reachable"] is False, "l'aveuglement doit être DIT"
    assert body["node_outputs_source"] == "none"
    assert body["node_outputs"] == []


@pytest.mark.asyncio
async def test_a_purged_run_falls_back_to_the_preview_and_dates_the_purge(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """Story 4.10 a purgé le fil LangGraph ; il ne reste que l'aperçu de 500
    caractères. La réponse doit le DIRE deux fois — par `source` et par
    `checkpoint_purged_at` — parce qu'un aperçu lu comme une sortie complète
    est exactement le défaut que l'AC2 interdit.
    """
    from datetime import UTC, datetime

    purged_at = datetime.now(UTC)
    truncated_preview = '{"status":"done","summary":"' + "x" * 470 + "…"
    run_id = await seed_run(
        checkpoint={"node_outputs_preview": {"code_researcher": truncated_preview}},
        checkpoint_purged_at=purged_at,
    )

    async with _client(app_session_factory) as client:
        body = (
            await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())
        ).json()

    assert body["node_outputs_source"] == "preview"
    assert body["checkpoint_purged_at"] is not None
    (entry,) = body["node_outputs"]
    assert entry["source"] == "preview"
    assert entry["truncated"] is True, "l'aperçu se lisait comme une sortie complète"
    assert entry["next_offset"] is None, "rien n'est reprenable : l'original a été purgé"
    # ⚠️ Cette app n'a PAS de checkpointer câblé (cf `_client`), donc ce corps
    # décrit une panne de lecture, pas une purge — et il le dit. La première
    # version de ce test assertait le contrat « fil purgé » sur exactement ce
    # câblage : la confusion que la revue a trouvée était gravée ici aussi.
    assert body["checkpointer_reachable"] is False


@pytest.mark.asyncio
async def test_a_node_with_no_recorded_output_is_a_404_on_the_per_node_route(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    run_id = await seed_run(
        checkpoint={"node_outputs_preview": {"code_researcher": '{"status":"done"}'}}
    )

    async with _client(app_session_factory) as client:
        response = await client.get(
            f"/api/v1/workflows/runs/{run_id}/nodes/architect_analyst/output",
            headers=_auth_headers(),
        )

    assert response.status_code == 404
    assert "architect_analyst" in response.json()["detail"]


@pytest.mark.asyncio
async def test_the_per_node_route_serves_the_preview_when_the_thread_is_gone(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    run_id = await seed_run(
        checkpoint={"node_outputs_preview": {"code_researcher": '{"status":"done"}'}}
    )

    async with _client(app_session_factory) as client:
        body = (
            await client.get(
                f"/api/v1/workflows/runs/{run_id}/nodes/code_researcher/output",
                headers=_auth_headers(),
            )
        ).json()

    assert body["output"]["source"] == "preview"
    assert body["output"]["output"] == '{"status":"done"}'
    assert body["limit"] == settings.run_node_output_max_chars


@pytest.mark.asyncio
async def test_a_limit_above_the_ceiling_is_refused_by_naming_the_setting(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """Le refus arrive AVANT toute lecture, et il nomme la variable — même
    posture que la garde `AGENTIVE_ALLOW_MCP_REGISTRATION` du provisioning.
    """
    run_id = await seed_run()
    too_much = settings.run_node_output_max_chars + 1

    async with _client(app_session_factory) as client:
        detail = await client.get(
            f"/api/v1/workflows/runs/{run_id}",
            headers=_auth_headers(),
            params={"limit": too_much},
        )
        per_node = await client.get(
            f"/api/v1/workflows/runs/{run_id}/nodes/code_researcher/output",
            headers=_auth_headers(),
            params={"limit": too_much},
        )

    for response in (detail, per_node):
        assert response.status_code == 422, response.text
        assert "AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS" in response.json()["detail"]


@pytest.mark.asyncio
async def test_the_route_is_behind_the_same_auth_guard_as_its_neighbours(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """T2.1 — « au même endroit que ses voisines et avec la même garde
    d'authentification ». Une route de lecture qui oublierait le middleware
    exposerait les sorties d'agents sans jeton."""
    run_id = await seed_run()

    async with _client(app_session_factory) as client:
        detail = await client.get(f"/api/v1/workflows/runs/{run_id}")
        per_node = await client.get(f"/api/v1/workflows/runs/{run_id}/nodes/code_researcher/output")

    assert detail.status_code == 401
    assert per_node.status_code == 401


def _client_with_checkpointer(
    session_factory: async_sessionmaker[AsyncSession], checkpointer: Any
) -> httpx.AsyncClient:
    """Une app dont le checkpointer EST câblé — l'état de production.

    Indispensable pour distinguer « le fil n'existe pas » de « je n'ai pas pu
    regarder » : sans checkpointer câblé, les deux se confondent, et c'est
    précisément dans cette confusion que la revue a trouvé le défaut dominant.
    """
    app = _make_app(session_factory=session_factory)
    app.state.workflow_checkpointer = checkpointer
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_a_purged_thread_is_told_apart_from_an_outage(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    seed_run: SeedRun,
) -> None:
    """Le constat dominant de la revue, épinglé.

    Même row, même aperçu, même absence de fil LangGraph. Ce qui change entre
    ce test et `test_a_purged_run_falls_back_to_the_preview_and_dates_the_purge`
    est le CÂBLAGE du checkpointer — et c'est exactement ce que la réponse
    doit refléter. Avant, les deux corps étaient identiques : un pool saturé
    se lisait « fil purgé, rien n'est reprenable », et un opérateur cessait de
    réessayer alors qu'une requête une seconde plus tard aurait tout rendu.
    """
    from datetime import UTC, datetime

    run_id = await seed_run(
        checkpoint={"node_outputs_preview": {"code_researcher": '{"status":"done"}'}},
        checkpoint_purged_at=datetime.now(UTC),
    )

    async with _client_with_checkpointer(app_session_factory, workflow_checkpointer) as client:
        body = (
            await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())
        ).json()

    assert body["checkpointer_reachable"] is True, "le checkpointer a répondu : il n'y a PAS de fil"
    assert body["node_outputs_source"] == "preview"
    assert body["checkpoint_purged_at"] is not None
    (entry,) = body["node_outputs"]
    assert entry["source"] == "preview"


@pytest.mark.asyncio
async def test_an_excerpt_is_paginable_even_though_it_is_an_excerpt(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    seed_run: SeedRun,
) -> None:
    """Second constat convergent des trois couches : `next_offset` décrit ce
    que le SERVEUR tient, jamais d'où le texte vient.

    Un aperçu coupé par le `limit` de l'appelant répondait « la suite n'est
    plus lisible » alors que `?offset=N` la servait. Ici on le vérifie sur la
    vraie surface HTTP, et on recolle.
    """
    excerpt = '{"summary":"' + "y" * 400 + '"}'
    run_id = await seed_run(checkpoint={"node_outputs_preview": {"code_researcher": excerpt}})

    async with _client_with_checkpointer(app_session_factory, workflow_checkpointer) as client:
        detail = (
            await client.get(
                f"/api/v1/workflows/runs/{run_id}",
                headers=_auth_headers(),
                params={"limit": 64},
            )
        ).json()
        (entry,) = detail["node_outputs"]
        assert entry["truncated"] is True
        assert entry["next_offset"] == 64, "une coupure faite par le serveur reste reprenable"

        rebuilt = entry["output"]
        offset = entry["next_offset"]
        while offset is not None:
            page = (
                await client.get(
                    f"/api/v1/workflows/runs/{run_id}/nodes/code_researcher/output",
                    headers=_auth_headers(),
                    params={"offset": offset, "limit": 64},
                )
            ).json()["output"]
            assert page["source"] == "preview"
            rebuilt += page["output"]
            offset = page["next_offset"]

    assert rebuilt == excerpt, "recoller les pages d'un aperçu doit rendre l'aperçu"


@pytest.mark.asyncio
async def test_the_purge_date_reads_the_same_on_both_routes(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    seed_run: SeedRun,
) -> None:
    """T2.2, appliquée à `checkpoint_purged_at` : la même colonne, sur deux
    routes de la même story, doit répondre la même chose pour une même row.
    Elle était inconditionnelle d'un côté et conditionnelle de l'autre."""
    from datetime import UTC, datetime

    run_id = await seed_run(
        checkpoint={"node_outputs_preview": {"n1": '{"status":"done"}'}},
        checkpoint_purged_at=datetime.now(UTC),
    )

    async with _client_with_checkpointer(app_session_factory, workflow_checkpointer) as client:
        detail = (
            await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())
        ).json()
        per_node = (
            await client.get(
                f"/api/v1/workflows/runs/{run_id}/nodes/n1/output", headers=_auth_headers()
            )
        ).json()

    assert detail["checkpoint_purged_at"] == per_node["checkpoint_purged_at"]
    assert per_node["checkpoint_purged_at"] is not None


@pytest.mark.asyncio
async def test_the_per_node_route_never_claims_a_node_is_empty_during_an_outage(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """Le `404` affirmait « has no recorded output » alors que rien n'avait pu
    être consulté — mot pour mot le défaut que la docstring de
    `_read_node_outputs` déclare interdit, et qui vivait dans ce message."""
    run_id = await seed_run()

    async with _client(app_session_factory) as client:
        response = await client.get(
            f"/api/v1/workflows/runs/{run_id}/nodes/code_researcher/output",
            headers=_auth_headers(),
        )

    assert response.status_code == 404
    body = response.json()
    assert "outage" in body["detail"], body["detail"]
    assert body["checkpointer_reachable"] is False


@pytest.mark.asyncio
async def test_the_per_node_route_always_carries_an_output_object(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    seed_run: SeedRun,
) -> None:
    """`output` était typé nullable pour un cas que la route rend `404` : une
    branche morte publiée dans l'OpenAPI, qu'un client défensif aurait codée
    sans jamais l'atteindre."""
    run_id = await seed_run(checkpoint={"node_outputs_preview": {"n1": '{"status":"done"}'}})

    async with _client_with_checkpointer(app_session_factory, workflow_checkpointer) as client:
        body = (
            await client.get(
                f"/api/v1/workflows/runs/{run_id}/nodes/n1/output", headers=_auth_headers()
            )
        ).json()

    assert body["output"] is not None
    assert body["checkpointer_reachable"] is True


@pytest.mark.asyncio
async def test_a_non_scalar_node_status_is_dropped_not_stringified(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """`str()` faisait sortir un `repr` Python (`"{'retries': 2}"`) dans un
    champ typé `dict[str, str]`, là où le commentaire d'à côté revendiquait un
    filtre `isinstance`."""
    run_id = await seed_run(
        checkpoint={"node_statuses": {"ok": "success", "bizarre": {"retries": 2}}}
    )

    async with _client(app_session_factory) as client:
        body = (
            await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())
        ).json()

    assert body["node_statuses"] == {"ok": "success"}


@pytest.mark.asyncio
async def test_a_secret_in_metrics_never_leaves_over_http(
    app_session_factory: async_sessionmaker[AsyncSession],
    seed_run: SeedRun,
) -> None:
    """NFR9 était proclamée sur les sorties de node et abandonnée sur
    `metrics`, dans la MÊME réponse — alors que `contract_problems`,
    `tool_names` et `model_used` sont d'origine LLM."""
    key = "sk-ant-" + "d" * 40
    run_id = await seed_run(
        metrics={"per_node": {"n1": {"contract_problems": [f"le prompt citait {key}"]}}}
    )

    async with _client(app_session_factory) as client:
        raw = (await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())).text

    assert key not in raw
    assert "[REDACTED]" in raw
