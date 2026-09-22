"""End-to-end — Dev Lead, prise de demande et décomposition (Story 5.1).

Postgres réel (testcontainers) + checkpointing LangGraph réel + LLM mocké.
Couvre les trois AC :

* **AC1** — le provisioning (`scripts.seed_dev`) crée le namespace, le
  template `Dev Lead` depuis l'archétype Orchestrateur, et le workflow
  d'entrée ; une seconde exécution ne crée rien.

Depuis la Story 5.2 le DAG d'entrée porte DEUX nodes
(`dev_lead → code_researcher`), donc chaque run de ce module consomme deux
réponses du `MockProvider` et l'accusé nomme deux agents.
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
import itertools
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.seed_dev import (
    ARCHITECT_ANALYST_NODE_ID,
    CODE_PRODUCER_NODE_ID,
    CODE_RESEARCHER_NODE_ID,
    DEV_ENTRY_WORKFLOW_NAME,
    DEV_LEAD_NODE_ID,
    DevDepartmentSeeder,
    SeedReport,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.infra.mcp.client import DEFAULT_DISCOVERY_TIMEOUT_S
from agentive_backend.shared.config import settings
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
#: ⚠️ Doit rester AU-DESSUS du budget d'un appel d'outil
#: (`AGENTIVE_TOOL_CALL_TIMEOUT_S`, 30 s par défaut). Depuis cette story, un
#: run du DAG d'entrée spawne un vrai sous-processus MCP par appel — il n'y a
#: pas de pool (défer D61). À 20 s, `_wait_for_terminal` rendait un statut non
#: terminal et le test échouait sur « != completed » AVANT même que le timeout
#: d'outil ne se déclenche : un échec qui accuse le moteur là où la cause est
#: le budget du test (revue 5.2).
_POLL_TIMEOUT_S = 45.0
_POLL_INTERVAL_S = 0.1

#: Le corpus que le serveur de lecture de code peut réellement lire — la
#: PREMIÈRE racine autorisée, jamais `/app` en dur.
#:
#: ⚠️ Revue 5.2 : ce module codait `/app` en dur, sans repli ni `skipif`, alors
#: que le module de dogfooding avait explicitement prévu le cas — deux
#: politiques opposées sur la même contrainte, dans le même changeset. Hors
#: container, l'échec était `tool_failures > 0` avec le message « l'appel
#: d'outil a échoué — voir les logs du serveur », qui ne nomme pas la cause.
_CORPUS_FEATURES = Path(settings.dev_code_roots[0]) / "src" / "agentive_backend" / "features"
_CORPUS_AVAILABLE = _CORPUS_FEATURES.is_dir()


@pytest.fixture(autouse=True)
def _mcp_registration_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le provisioning REFUSE d'enregistrer un serveur MCP sans ce drapeau.

    Ce n'est pas une gêne de test à contourner, c'est le comportement que la
    Story 5.2 T3.4 exige. Depuis la revue de cette story, la garde vit dans
    `ToolHubService.connect_server` — au point où le sous-processus est spawné,
    et non plus dans chacun de ses appelants. Ici on se place du côté de
    l'opérateur qui a dit oui.

    Le refus lui-même est asserté ailleurs, et le renvoi d'origine pointait un
    fichier INEXISTANT (`tests/unit/agent_registry/test_seed_dev_servers.py`,
    troisième référence morte relevée par la revue). Les bons chemins :
    `tests/unit/tool_hub/test_service.py::test_connect_server_is_refused_when_registration_is_disabled`
    pour la porte elle-même, et
    `tests/scripts/test_seed_dev.py::test_the_registration_refusal_names_the_setting`
    pour sa traduction en message d'opérateur.
    """
    from agentive_backend.shared.config import settings as _settings

    monkeypatch.setattr(_settings, "mcp_allow_registration", True)


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


#: Une sortie de Code Researcher bien formée. Les chemins sont RÉELS et
#: existent dans le container (`/app` est le volume `./backend`), mais ce
#: module ne s'appuie pas dessus : la vérification « ces chemins existent
#: vraiment et viennent d'un outil » est l'objet de
#: `tests/integration/mcp/test_code_search_dogfooding.py`, sur le serveur réel.
#: Ici la sortie est pré-écrite, donc elle prouve que le contrat traverse le
#: moteur — pas que le modèle sait explorer.
_RESEARCHER_ANSWER = json.dumps(
    {
        "status": "done",
        "summary": "Relevé des patterns des modules existants",
        "relevant_files": [
            {
                "path": "/app/src/agentive_backend/features/tool_hub/router.py",
                "role": "routeur HTTP du module",
                "why": "montre la forme d'un routeur de feature",
            }
        ],
        "dependencies_graph": {"features/tool_hub": ["shared/repositories", "infra/mcp"]},
        "existing_patterns": [
            {
                "name": "router/service/schemas",
                "description": "chaque feature porte ces trois fichiers",
                "examples": ["/app/src/agentive_backend/features/tool_hub/service.py"],
            }
        ],
        "risk_areas": [
            {
                "area": "import-linter",
                "risk": "un module qui en importe un autre casse la CI",
                "evidence": "/app/.import-linter",
            }
        ],
    },
    ensure_ascii=False,
)


#: Une sortie d'Architect Analyst bien formée (Story 5.3).
#:
#: Les `approach.steps[].id` sont ce que le Code Producer doit citer dans
#: `code_diffs[].approach_ref` — c'est le chaînon de l'AC3, et le test qui le
#: vérifie ne lit PAS cette constante : il extrait les `id` de la sortie que le
#: MOTEUR a réellement produite pour le node `architect_analyst`.
_ANALYST_ANSWER = json.dumps(
    {
        "status": "done",
        "summary": "Suivre le gabarit router/service/schemas des modules existants",
        "approach": {
            "summary": "Créer le package sur le gabarit relevé, brancher le repo partagé.",
            "complexity": "medium",
            "steps": [
                {
                    "id": "a1",
                    "title": "Créer le package avec router/service/schemas",
                    "rationale": "douze modules portent déjà cette forme",
                },
                {
                    "id": "a2",
                    "title": "Brancher l'accès base par shared/repositories",
                    "rationale": "règle d'or #4",
                },
            ],
        },
        "tradeoffs": [
            {
                "option": "Suivre le gabarit existant",
                "pros": ["cohérence avec M2-M12"],
                "cons": ["peu de marge de manœuvre"],
                "chosen": True,
                "rationale": "la cohérence prime sur la liberté pour un module de plus",
            },
            {
                "option": "Repartir d'une structure neuve",
                "pros": ["liberté de conception"],
                "cons": ["divergence avec douze modules"],
                "chosen": False,
                "rationale": "aucun bénéfice qui paie la divergence",
            },
        ],
        "risks": [
            {
                "risk": "Violation d'un contrat import-linter",
                "severity": "high",
                "mitigation": "passer par shared/contracts, jamais par un import croisé",
            }
        ],
        "test_strategy": {
            "levels": ["unit", "integration"],
            "focus": ["le routeur refuse un payload inconnu"],
        },
    },
    ensure_ascii=False,
)

#: Une sortie de Code Producer bien formée (Story 5.3).
#:
#: Les `approach_ref` valent `a1` et `a2` — les `id` que `_ANALYST_ANSWER`
#: déclare. Le test de l'AC3 vérifie cette correspondance en lisant les DEUX
#: sorties depuis le moteur, jamais depuis ces deux littéraux : une assertion
#: sur la constante serait circulaire, exactement le défaut que la revue de la
#: Story 5.7 a dû corriger sur son propre test « qui compte ».
_PRODUCER_ANSWER = json.dumps(
    {
        "status": "done",
        "summary": "Squelette du module, ses tests et son entrée de runbook",
        "code_diffs": [
            {
                "path": "backend/src/agentive_backend/features/m13/router.py",
                "diff": "+from fastapi import APIRouter\n+router = APIRouter()\n",
                "approach_ref": "a1",
                "rationale": "étape a1 : créer le package sur le gabarit relevé",
            },
            {
                "path": "backend/src/agentive_backend/features/m13/service.py",
                "diff": "+class M13Service:\n+    ...\n",
                "approach_ref": "a2",
                "rationale": "étape a2 : l'accès base passe par shared/repositories",
            },
        ],
        "tests": [
            {
                "path": "backend/tests/unit/m13/test_router.py",
                "level": "unit",
                "content": "def test_unknown_payload_is_refused() -> None: ...",
                "covers": ["a1"],
            }
        ],
        "docs_snippets": [{"target": "docs/runbooks/m13.md", "content": "# M13 — mise en service"}],
        "unaddressed": [],
    },
    ensure_ascii=False,
)


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


#: Le résumé de passage que le moteur demande ENTRE les deux nodes.
#:
#: Ce n'est pas un détail de fixture : dès qu'un DAG a une arête, `agent_node`
#: appelle le modèle une fois de plus pour condenser la sortie de l'émetteur
#: (Story 4.7), et c'est CE résumé — et non la sortie brute du Dev Lead — que
#: le Code Researcher lit dans `upstream_outputs`. Un run du DAG d'entrée coûte
#: donc TROIS appels LLM, pas deux. La forme est imposée par
#: `DEFAULT_HANDOFF_SUMMARY_SYSTEM_PROMPT`.
_HANDOFF_SUMMARY = json.dumps(
    {
        "decisions": [
            "Déléguer l'exploration du codebase à code_researcher : "
            "relever les patterns des modules existants"
        ],
        "artifacts_refs": [],
        "blockers": [],
        "next_questions": ["Quels modules servent de baseline ?"],
    },
    ensure_ascii=False,
)


#: Ce que le DAG consomme APRÈS le node `code_researcher` (Story 5.3).
#:
#: ⚠️ Aucun résumé de passage entre ces trois nodes, et ce n'est pas un oubli :
#: `architect_analyst` et `code_producer` déclarent
#: `include_raw_previous_output: true`, donc
#: `graph_builder._any_successor_reads_summaries` rend `False` pour leurs
#: émetteurs et le moteur ne DEMANDE PLUS le résumé — l'appel LLM est
#: économisé, pas seulement ignoré. Le seul résumé qui subsiste est celui du
#: Dev Lead, dont le successeur (`code_researcher`) lit encore les résumés.
_DOWNSTREAM_COMPLETIONS: list[str] = [_ANALYST_ANSWER, _PRODUCER_ANSWER]


def _dag_completions(plan_json: str) -> list[Completion]:
    """Les CINQ réponses qu'un run du DAG d'entrée consomme, dans l'ordre.

    `MockProvider` est FIFO, et l'ordre est : le node `dev_lead`, le résumé de
    passage de SON arête, puis `code_researcher`, `architect_analyst` et
    `code_producer`. Une liste plus courte fait échouer le dernier node sur une
    file vide — le run finit `error` et le test échoue très loin de son sujet.

    ⚠️ Story 5.3 — c'était TROIS avant que le DAG passe à quatre nodes. Le
    décompte ne suit pas le nombre de nodes : il suit le nombre de nodes PLUS
    le nombre d'arêtes dont la cible lit encore les résumés de passage.
    """
    return [
        _completion(plan_json),
        _completion(_HANDOFF_SUMMARY),
        _completion(_RESEARCHER_ANSWER),
        *(_completion(answer) for answer in _DOWNSTREAM_COMPLETIONS),
    ]


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

    # Story 5.2 AC1 — LA PREMIÈRE LISTE NON VIDE DU DÉPÔT, vue depuis l'API et
    # non depuis le composant. La 5.0 a livré la boucle d'outils, la 5.1 le
    # mécanisme d'assignation — et sa revue a trouvé que `tools: []` faisait
    # sortir `_assign_tools` avant toute résolution, donc qu'aucun test
    # n'atteignait le mécanisme. C'est ici qu'il est enfin traversé.
    researcher_id = report.template_ids["code_researcher"]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        researcher_detail = await client.get(
            f"/api/v1/agents/templates/{researcher_id}", headers=_auth_headers()
        )
        assert researcher_detail.status_code == 200, researcher_detail.text
        assert researcher_detail.json()["archetype"] == "chercheur"
        assert researcher_detail.json()["name"] == "Code Researcher"
        assert set(researcher_detail.json()["config"]["output_contract"]["core"]) == {
            "relevant_files",
            "dependencies_graph",
            "existing_patterns",
            "risk_areas",
        }

        researcher_tools = await client.get(
            f"/api/v1/agents/templates/{researcher_id}/tools", headers=_auth_headers()
        )
        assert researcher_tools.status_code == 200, researcher_tools.text
        assigned = researcher_tools.json()["assigned_tools"]
        assert {tool["name"] for tool in assigned} == {
            "list_directory",
            "read_file",
            "find_files",
            "search_content",
        }, f"liste d'outils assignés inattendue : {assigned}"

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
    assert second.server_ids == first.server_ids
    assert second.workflow_id == first.workflow_id
    assert "agent-template Dev Lead" in second.unchanged
    assert "agent-template Code Researcher" in second.unchanged
    # ⚠️ T7.4 exigeait « les DEUX nouveaux templates créés puis `unchanged` à
    # la seconde exécution », et ce test n'avait pas été étendu : seul
    # `second.created == []` les couvrait, implicitement. Les nommer est ce
    # qui distingue « rien n'a été créé » de « ces deux-là ont bien été
    # reconnus » — un template que le seeder ne retrouverait plus par son nom
    # satisferait la première affirmation en échouant la seconde.
    assert "agent-template Architect Analyst" in second.unchanged
    assert "agent-template Code Producer" in second.unchanged
    assert "serveur MCP dev-code-search" in second.unchanged

    # Les assertions ci-dessus portent sur ce que le seeder dit de LUI-MÊME.
    # « Zéro doublon » est une propriété de la BASE, et c'est la seule chose
    # qui distingue « idempotent » de « se déclare idempotent » — d'autant que
    # ce package n'a volontairement pas de fixture de purge, donc les rows
    # s'accumulent d'un test à l'autre.
    from sqlalchemy import text

    async with app_session_factory() as session:
        # Compté PAR NOM pour chacun des quatre : ce package n'a aucune
        # fixture de purge, et la même précaution n'avait été prise que pour
        # le Dev Lead — un doublon sur « Architect Analyst » serait donc resté
        # invisible.
        for template_name in (
            "Dev Lead",
            "Code Researcher",
            "Architect Analyst",
            "Code Producer",
        ):
            templates = await session.execute(
                text("SELECT count(*) FROM agent_templates WHERE name = :name"),
                {"name": template_name},
            )
            assert templates.scalar_one() == 1, f"doublon de template : {template_name}"
        workflows = await session.execute(
            text("SELECT count(*) FROM workflows WHERE name = :name"),
            {"name": DEV_ENTRY_WORKFLOW_NAME},
        )
        assert workflows.scalar_one() == 1
        # Story 5.2 T3.3 — `connect_server` n'a AUCUN chemin de re-découverte :
        # un nom déjà pris lève `ConflictError` avant même de spawner le
        # serveur. La seconde exécution doit donc relire et vérifier, pas
        # ré-enregistrer — et surtout ne pas laisser deux rows.
        servers = await session.execute(
            text("SELECT count(*) FROM tool_servers WHERE name = :name"),
            {"name": "dev-code-search"},
        )
        # Compté PAR NOM, et non globalement : ce package n'a aucune fixture
        # de purge (les rows survivent à toute la session), donc un compte
        # global deviendrait cumulatif dès qu'un autre module enregistre un
        # serveur — et l'échec accuserait l'idempotence du seeder.
        assert servers.scalar_one() == 1
        tools = await session.execute(
            text(
                "SELECT count(*) FROM tools t JOIN tool_servers s ON s.id = t.server_id "
                "WHERE s.name = :name"
            ),
            {"name": "dev-code-search"},
        )
        assert tools.scalar_one() == 4
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
    payload = dag if isinstance(dag, dict) else json.loads(dag)
    nodes = payload["nodes"]
    # Story 5.3 — la chaîne complète du pôle, dans son ordre naturel : on
    # explore avant d'analyser, on analyse avant de produire.
    expected_order = [
        DEV_LEAD_NODE_ID,
        CODE_RESEARCHER_NODE_ID,
        ARCHITECT_ANALYST_NODE_ID,
        CODE_PRODUCER_NODE_ID,
    ]
    assert [node["node_id"] for node in nodes] == expected_order
    for node in nodes:
        assert node["agent_template_id"] == str(report.template_ids[node["node_id"]])
    # Les edges, sans condition : chaque node s'exécute quoi que son amont ait
    # rendu — y compris un `failed`, cas où c'est justement l'aval qui peut
    # répondre à la `blocking_question`.
    assert payload["edges"] == [
        {"from_node_id": source, "to_node_id": target, "condition": None}
        for source, target in itertools.pairwise(expected_order)
    ]


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
    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(canned))

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
        # T5.5 (Story 5.3) — le DAG a DEUX nodes de plus qu'en 5.2, donc
        # l'accusé nomme deux agents de plus ET son ETA (somme des médianes PAR
        # NODE) passe de 2 à 4 minutes. Ce test épinglait déjà la phrase
        # exacte, et c'est ce qui rend la bascule visible plutôt que subie.
        ack = body["acknowledgement"]
        assert ack["message"] == (
            "Compris. Je mobilise Dev Lead, Code Researcher, Architect Analyst "
            "et Code Producer. ETA ~4 min."
        )
        assert ack["agents"] == [
            "Dev Lead",
            "Code Researcher",
            "Architect Analyst",
            "Code Producer",
        ]
        assert ack["eta_minutes"] == 4
        # ⚠️ `eta_source` est asserté ICI parce que le package n'a AUCUNE
        # fixture de purge : tous les tests du module partagent le même
        # `workflow_id` et voient l'historique des runs précédents. L'ancienne
        # assertion `>= 1` absorbait cette pollution ; `== 2` ne l'absorbe plus,
        # et 2 est exactement la valeur du repli heuristique (60 s par node).
        # Sans cette ligne, le test passait aussi bien avec un historique lu
        # qu'avec un repli silencieux — et basculait sous `-k`, `--lf` ou un
        # réordonnancement (revue 5.2).
        assert ack["eta_source"] == "heuristic"

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
    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(canned))

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
    provider = MockProvider("mock", _dag_completions(canned))
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

    # Exactement CINQ appels : un par node du DAG (quatre), plus UN seul résumé
    # de passage — celui du Dev Lead, dont le successeur `code_researcher` lit
    # encore les résumés. Les deux arêtes suivantes n'en produisent aucun,
    # leurs cibles déclarant `include_raw_previous_output: true`
    # (`graph_builder._any_successor_reads_summaries`). Aucun appel pour
    # l'accusé — c'est ce que ce test garde, et le décompte exact est ce qui le
    # rend capable de le prouver.
    assert len(provider.calls) == 5


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
    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(canned))

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

    # 180 s + 120 s sur les DEUX nodes du DAG — des valeurs qu'aucun repli ne
    # peut produire (le défaut est de 60 s par node, donc 2 min au total),
    # donc l'ETA rendue prouve la LECTURE. Poser l'historique d'un seul node
    # ferait retomber l'ETA en `heuristic` sur l'autre : `build_acknowledgement`
    # exige un échantillon pour CHAQUE node avant d'annoncer `history`.
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
                "m": json.dumps(
                    {
                        "per_node": {
                            # Story 5.3 — les QUATRE nodes, et c'est nécessaire :
                            # `build_acknowledgement` ne rend `eta_source:
                            # history` que si CHAQUE node du DAG a un
                            # échantillon. Un seul node sans mesure fait
                            # basculer tout l'accusé en `heuristic`.
                            DEV_LEAD_NODE_ID: {"duration_ms": 180_000},
                            CODE_RESEARCHER_NODE_ID: {"duration_ms": 120_000},
                            ARCHITECT_ANALYST_NODE_ID: {"duration_ms": 90_000},
                            CODE_PRODUCER_NODE_ID: {"duration_ms": 150_000},
                        }
                    }
                ),
            },
        )
        await session.commit()

    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(canned))
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
        # 180 + 120 + 90 + 150 s ⇒ 540 s ⇒ 9 min. Le repli heuristique aurait
        # rendu 4 min (quatre nodes au défaut de 60 s).
        assert ack["eta_minutes"] == 9
        assert "ETA ~9 min." in ack["message"]

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
                "m": json.dumps(
                    {
                        "per_node": {
                            # Story 5.3 — les QUATRE nodes, et c'est nécessaire :
                            # `build_acknowledgement` ne rend `eta_source:
                            # history` que si CHAQUE node du DAG a un
                            # échantillon. Un seul node sans mesure fait
                            # basculer tout l'accusé en `heuristic`.
                            DEV_LEAD_NODE_ID: {"duration_ms": 180_000},
                            CODE_RESEARCHER_NODE_ID: {"duration_ms": 120_000},
                            ARCHITECT_ANALYST_NODE_ID: {"duration_ms": 90_000},
                            CODE_PRODUCER_NODE_ID: {"duration_ms": 150_000},
                        }
                    }
                ),
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

    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(canned))
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
        assert ack["eta_minutes"] == 9

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

    **Le budget de ping n'est pas celui du conftest.** Ce test est le SEUL
    du package où `mcp_tools_reachable` spawne réellement le serveur
    `code_search` sandboxé : les autres câblages de la porte réelle pingent
    un binaire inexistant (échec immédiat) ou un template sans outil. Ce
    spawn coûte ~1,3 s sur un poste de dev rapide (import du module + poignée
    de main MCP + bwrap ou setrlimit), et le défaut de 2 s du conftest tombait
    en CI, sur un runner partagé et après cinq minutes de suite — la porte
    répondait 503 « injoignable » pour un serveur que le seeder venait
    d'enregistrer avec succès. On lui redonne ici le budget de la
    découverte à l'enregistrement, celui que le seeder vient d'utiliser sur
    la MÊME commande : ce test prouve le câblage des namespaces, pas le
    réglage du timeout de ping.
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    app = _make_app(session_factory=app_session_factory)
    app.state.workflow_checkpointer = workflow_checkpointer
    app.state.llm_router = LLMRouter(
        providers={"mock": MockProvider("mock", _dag_completions(canned))},
        default_chain=["mock"],
    )
    wire_execution_service_with_real_mise_en_place(
        app, tool_ping_timeout_s=DEFAULT_DISCOVERY_TIMEOUT_S
    )

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
    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(canned))

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
    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(rogue))

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
    app = _build_app(app_session_factory, workflow_checkpointer, _dag_completions(canned))

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


# ─── AC2 — la sortie est APPUYÉE sur des appels d'outils réels ───────


@pytest.mark.asyncio
async def test_the_code_researcher_really_calls_its_tools_during_a_run(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """Story 5.2 AC2 / T7.5 — **la preuve que la Story 5.0 n'a jamais eue**.

    Sa revue a trouvé que `node_tools` n'était câblé par aucun appelant de
    production alors que TOUS ses tests unitaires passaient : la boucle
    d'outils était livrée, l'exécuteur MCP était livré, et aucun template ne
    portait un seul outil. Ce test est le premier du dépôt où un run de
    workflow exécute réellement un outil.

    Tout est réel sauf le modèle : Postgres, le checkpointing LangGraph, le
    serveur MCP (sous-processus stdio sandboxé), la table `tools` peuplée par
    la découverte. Le `MockProvider` ne fait que DEMANDER l'outil — ce qu'il
    reçoit en retour vient du disque.

    Trois choses distinctes sont vérifiées, et la troisième est celle qui
    empêche ce test d'être circulaire :
      1. les outils ont été OFFERTS au modèle (`calls[...]["tools"]`) ;
      2. l'exécution est COMPTÉE dans `metrics.per_node` — la surface que
         l'AC2 nomme ;
      3. le résultat rendu au modèle porte des chemins qui EXISTENT vraiment
         sur le disque. Une fixture ne peut pas fabriquer ça.
    """
    from sqlalchemy import text

    from agentive_backend.shared.llm.types import ToolCall

    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    asking_for_a_tool = Completion(
        text="",
        model="mock-model",
        provider="mock",
        input_tokens=100,
        output_tokens=20,
        finish_reason="tool_use",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0001"),
        tool_calls=(
            ToolCall(
                id="call-1",
                name="find_files",
                arguments={
                    "pattern": "**/router.py",
                    "path": str(_CORPUS_FEATURES),
                    "max_results": 5,
                },
            ),
        ),
    )
    provider = MockProvider(
        "mock",
        [
            _completion(canned),  # node `dev_lead`
            _completion(_HANDOFF_SUMMARY),  # résumé de passage de l'arête
            asking_for_a_tool,  # node `code_researcher`, tour 1
            _completion(_RESEARCHER_ANSWER),  # node `code_researcher`, tour 2
            # Story 5.3 — le DAG continue : `architect_analyst` puis
            # `code_producer`, sans résumé entre eux (ils lisent le brut).
            *(_completion(answer) for answer in _DOWNSTREAM_COMPLETIONS),
        ],
    )

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

    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT metrics FROM workflow_runs WHERE id = :r"), {"r": run_id}
        )
        metrics = row.scalar_one()

    # (2) — la surface que l'AC2 nomme, mot pour mot.
    researcher = metrics["per_node"][CODE_RESEARCHER_NODE_ID]
    assert researcher["tool_calls"] > 0, (
        "le node n'a appelé aucun outil : `_load_node_tools` ne l'a pas résolu, "
        "ou la liste `tools:` du template est restée vide"
    )
    assert researcher["tool_names"] == ["find_files"]
    assert researcher["tool_failures"] == 0, (
        "l'appel d'outil a échoué. Sous le repli `setrlimit` (bwrap absent), "
        "`RLIMIT_CPU`/`RLIMIT_AS` peuvent tuer le sous-processus à l'import — "
        "vérifier les logs du serveur ET le backend de sandbox effectif."
    )
    assert researcher["tool_loop_iterations"] == 2

    # Le Dev Lead n'a AUCUN outil, et sa métrique le dit : la forme ne change
    # pas pour un node sans outils (Story 5.0 AC4).
    assert metrics["per_node"][DEV_LEAD_NODE_ID]["tool_calls"] == 0
    assert metrics["per_node"][DEV_LEAD_NODE_ID]["tool_names"] == []

    # (1) — les outils ont bien été OFFERTS, et seulement au node qui les a.
    calls = provider.calls
    assert calls[0]["tools"] is None, "le Dev Lead s'est vu offrir des outils qu'il n'a pas"
    # Revue 5.2 — `calls[1]` (résumé de passage) et `calls[3]` (tour 2) n'étaient
    # contrôlés par rien : un bug offrant les outils de lecture à l'appel de
    # résumé, où aucun contrat ne les attend, passait inaperçu.
    assert calls[1]["tools"] is None, "le résumé de passage s'est vu offrir des outils"
    offered = {tool.name for tool in calls[2]["tools"] or []}
    assert offered == {"list_directory", "read_file", "find_files", "search_content"}
    assert calls[2]["tools"], "les outils n'ont pas été offerts au Chercheur"
    assert {tool.name for tool in calls[3]["tools"] or []} == offered, (
        "le tour 2 de la boucle doit ré-offrir les mêmes outils"
    )

    # (3) — ce que le modèle a REÇU vient du disque. C'est ce qui distingue ce
    # test d'une fixture qui se relit elle-même : les chemins ci-dessous ont
    # été produits par un sous-processus lisant `/app`, pas écrits ici.
    tool_messages = [message for message in calls[3]["messages"] if message.role == "tool"]
    assert tool_messages, "le résultat de l'outil n'a pas été renvoyé au modèle"
    body = tool_messages[0].content
    assert "<tool_output>" in body, "le résultat d'outil n'est pas enveloppé (règle d'or #9)"
    returned = re.findall(rf"{re.escape(str(_CORPUS_FEATURES))}/[\w/]+\.py", body)
    assert returned, f"aucun chemin dans le résultat de l'outil : {body[:400]}"
    for path in returned:
        assert Path(path).is_file(), f"le serveur a rendu un chemin inexistant : {path}"


@pytest.mark.asyncio
async def test_a_refused_path_inside_a_run_does_not_kill_the_run(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """Le mode de panne **(b)** du runbook, enfin prouvé (revue 5.2).

    `docs/runbooks/pole-dev.md` affirme : « Un chemin est refusé au runtime →
    le run ne meurt **PAS**. Le refus arrive au modèle comme un résultat
    d'erreur exploitable (`isError`), et l'agent peut corriger son chemin. »
    C'est la section la plus rassurante du runbook, et elle ne reposait sur
    aucun test : le seul test d'outils dans un run n'exerçait que le chemin
    heureux (`tool_failures == 0`).

    Ce que ce test établit, et que les unitaires ne peuvent pas : que le refus
    traverse le sous-processus, le transport MCP, `McpToolExecutor` et la
    boucle d'outils pour revenir au modèle sous forme de message `tool`
    exploitable — pendant que le run, lui, va jusqu'à `completed`.
    """
    from sqlalchemy import text

    from agentive_backend.shared.llm.types import ToolCall

    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    asking_for_a_forbidden_path = Completion(
        text="",
        model="mock",
        provider="mock",
        input_tokens=100,
        output_tokens=20,
        finish_reason="tool_use",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0001"),
        tool_calls=(ToolCall(id="call-1", name="read_file", arguments={"path": "/etc/passwd"}),),
    )
    provider = MockProvider(
        "mock",
        [
            _completion(canned),
            _completion(_HANDOFF_SUMMARY),
            asking_for_a_forbidden_path,
            _completion(_RESEARCHER_ANSWER),
            # Story 5.3 — le DAG continue : `architect_analyst` puis
            # `code_producer`, sans résumé entre eux (ils lisent le brut).
            *(_completion(answer) for answer in _DOWNSTREAM_COMPLETIONS),
        ],
    )

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
        # LE point : le run termine normalement malgré le refus.
        assert await _wait_for_terminal(client, app_session_factory, run_id) == "completed"

    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT metrics FROM workflow_runs WHERE id = :r"), {"r": run_id}
        )
        metrics = row.scalar_one()

    researcher = metrics["per_node"][CODE_RESEARCHER_NODE_ID]
    assert researcher["tool_calls"] > 0
    assert researcher["tool_failures"] == 1, "le refus doit être COMPTÉ, pas avalé"

    # Et il est revenu au modèle sous une forme qu'il peut exploiter.
    tool_messages = [message for message in provider.calls[3]["messages"] if message.role == "tool"]
    assert tool_messages, "le refus n'a pas été renvoyé au modèle"
    body = tool_messages[0].content
    assert "<tool_output>" in body, "le refus n'est pas enveloppé (règle d'or #9)"
    assert "racines autorisées" in body, (
        "le message ne dit pas au modèle POURQUOI le chemin est refusé — "
        "T1.4 exige un résultat d'erreur exploitable, pas seulement signalé"
    )


@pytest.mark.asyncio
async def test_an_operator_can_read_the_researchers_output_over_http(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """Story 5.7 T6.3 — **le test qui compte**.

    Même gabarit que
    `test_the_code_researcher_really_calls_its_tools_during_a_run` juste
    au-dessus, à une différence près, et c'est toute la story : ce test-là lit
    la row `workflow_runs` en SQL, celui-ci lit l'endpoint. Jusqu'à la Story
    5.7, la seconde moitié n'existait pas — l'anti-scope de la 5.2 affirmait
    que la sortie du Chercheur « est lue par un humain » alors que cette
    lecture n'avait aucune surface.

    Quatre choses distinctes :
      1. les compteurs d'outils de l'AC2 de la 5.2 sortent par HTTP ;
      2. la sortie du Chercheur sort en ENTIER, et la réponse dit d'où elle
         vient (`checkpointer`) et qu'elle n'a pas été coupée ;
      3. la Mise en Place et l'accusé de réception que l'AC1 énumère sortent
         PEUPLÉS — ils n'avaient jusqu'ici que des assertions `is None` ;
      4. les chemins que la sortie cite sont ceux que le SOUS-PROCESSUS a
         rendus depuis le disque.

    ⚠️ **Le point 4 est celui que la revue a trouvé circulaire, et voici ce
    qu'il établit exactement.** La réponse finale du node est une constante de
    ce fichier (`_RESEARCHER_ANSWER`, servie par le `MockProvider`), donc
    vérifier que ses chemins existent ne prouverait que le soin de l'auteur du
    test. Le chaînon non circulaire est ailleurs : on extrait les chemins du
    MESSAGE D'OUTIL — produit par un sous-processus MCP qui a lu `/app` — et
    on exige que ce que l'endpoint rend soit couvert par eux. Une fixture ne
    peut pas fabriquer ce message.
    """
    from sqlalchemy import text

    from agentive_backend.features.workflow_engine.service import _CHECKPOINT_PREVIEW_MAX_CHARS
    from agentive_backend.shared.llm.types import ToolCall

    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    asking_for_a_tool = Completion(
        text="",
        model="mock-model",
        provider="mock",
        input_tokens=100,
        output_tokens=20,
        finish_reason="tool_use",
        latency_ms=5.0,
        cost_estimate_usd=Decimal("0.0001"),
        tool_calls=(
            ToolCall(
                id="call-1",
                name="find_files",
                arguments={
                    "pattern": "**/router.py",
                    "path": str(_CORPUS_FEATURES),
                    "max_results": 5,
                },
            ),
        ),
    )
    provider = MockProvider(
        "mock",
        [
            _completion(canned),  # node `dev_lead`
            _completion(_HANDOFF_SUMMARY),  # résumé de passage de l'arête
            asking_for_a_tool,  # node `code_researcher`, tour 1
            _completion(_RESEARCHER_ANSWER),  # node `code_researcher`, tour 2
            # Story 5.3 — le DAG continue : `architect_analyst` puis
            # `code_producer`, sans résumé entre eux (ils lisent le brut).
            *(_completion(answer) for answer in _DOWNSTREAM_COMPLETIONS),
        ],
    )

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

        detail = await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())

    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["checkpointer_reachable"] is True

    # (1) — les quatre compteurs de l'AC2 de la 5.2, enfin interrogeables
    # autrement qu'en SQL. C'est le critère 1 du protocole du § 7 bis, déclaré
    # éliminatoire et jusqu'ici inexécutable en suivant le runbook.
    researcher_metrics = body["metrics"]["per_node"][CODE_RESEARCHER_NODE_ID]
    assert researcher_metrics["tool_calls"] > 0
    assert researcher_metrics["tool_names"] == ["find_files"]
    assert researcher_metrics["tool_failures"] == 0
    assert researcher_metrics["tool_loop_iterations"] == 2

    # (2) — la sortie, en entier, avec sa provenance dite.
    assert body["node_outputs_source"] == "checkpointer"
    outputs = {entry["node_id"]: entry for entry in body["node_outputs"]}
    assert set(outputs) == {
        DEV_LEAD_NODE_ID,
        CODE_RESEARCHER_NODE_ID,
        ARCHITECT_ANALYST_NODE_ID,
        CODE_PRODUCER_NODE_ID,
    }
    researcher = outputs[CODE_RESEARCHER_NODE_ID]
    assert researcher["source"] == "checkpointer"
    assert researcher["truncated"] is False
    assert researcher["next_offset"] is None
    assert researcher["returned_chars"] == researcher["total_chars"]

    # L'aperçu applicatif N'AURAIT PAS SUFFI — établi contre ce que le MOTEUR
    # a réellement persisté, pas contre une constante de ce fichier : la row
    # porte un aperçu que `_preview()` a coupé (marqueur `…` en fin), là où
    # l'endpoint rend la sortie complète. C'est le constat qui a fait rejeter
    # l'option (a) de T1.2.
    async with app_session_factory() as session:
        row = await session.execute(
            text("SELECT checkpoint FROM workflow_runs WHERE id = :r"), {"r": run_id}
        )
        stored_preview = row.scalar_one()["node_outputs_preview"][CODE_RESEARCHER_NODE_ID]
    assert stored_preview.endswith("…"), "l'aperçu persisté n'a pas été coupé : rien à démontrer"
    assert len(stored_preview) == _CHECKPOINT_PREVIEW_MAX_CHARS + 1
    assert researcher["total_chars"] > len(stored_preview)

    # (3) — AC1 nomme `mise_en_place` et `acknowledgement` ; ils n'avaient
    # aucune couverture positive, seulement des assertions `is None`.
    assert body["mise_en_place"]["all_passed"] is True
    assert len(body["mise_en_place"]["checks"]) == 4
    assert body["acknowledgement"]["agents"] == [
        "Dev Lead",
        "Code Researcher",
        "Architect Analyst",
        "Code Producer",
    ]
    assert body["acknowledgement"]["eta_source"] in {"history", "heuristic"}
    assert body["acknowledgement"]["message"].startswith("Compris.")

    # (4) — LE chaînon non circulaire : ce que le sous-processus a rendu.
    tool_messages = [message for message in provider.calls[3]["messages"] if message.role == "tool"]
    assert tool_messages, "le résultat de l'outil n'a pas été renvoyé au modèle"
    from_disk = set(
        re.findall(rf"{re.escape(str(_CORPUS_FEATURES))}/[\w/]+\.py", tool_messages[0].content)
    )
    assert from_disk, "le serveur MCP n'a rendu aucun chemin : il n'y a rien à corroborer"

    payload = json.loads(researcher["output"])
    cited = [entry["path"] for entry in payload["relevant_files"]]
    assert cited, "la sortie ne cite aucun chemin : il n'y a rien à vérifier"
    assert set(cited) <= from_disk, (
        "l'endpoint rend des chemins que le sous-processus n'a jamais lus : "
        f"cités={cited}, rendus par l'outil={sorted(from_disk)}"
    )
    for path in cited:
        assert Path(path).is_file(), f"l'endpoint rend un chemin inexistant : {path}"


@pytest.mark.asyncio
async def test_a_long_node_output_is_reassembled_page_by_page_over_http(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """Story 5.7 T6.4 — la troncature annoncée, sur la vraie surface HTTP.

    L'unitaire garde la fonction de découpe ; celui-ci garde la PROMESSE de
    l'AC2 (« la sortie complète est atteignable ») de bout en bout : un
    plafond volontairement minuscule, puis les pages recollées doivent rendre
    exactement ce que la route de détail annonce comme total.

    Sans ce test, `next_offset` serait une intention — c'est la formulation
    de T3.2, et elle s'applique à la surface autant qu'à la fonction.
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]

    provider = MockProvider(
        "mock",
        [
            _completion(canned),
            _completion(_HANDOFF_SUMMARY),
            _completion(_RESEARCHER_ANSWER),
            # Story 5.3 — le DAG continue : `architect_analyst` puis
            # `code_producer`, sans résumé entre eux (ils lisent le brut).
            *(_completion(answer) for answer in _DOWNSTREAM_COMPLETIONS),
        ],
    )

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

        # Un plafond de 64 caractères : la sortie du Chercheur en fait des
        # milliers, donc la coupure est certaine et doit être DITE.
        detail = await client.get(
            f"/api/v1/workflows/runs/{run_id}",
            headers=_auth_headers(),
            params={"limit": 64},
        )
        assert detail.status_code == 200, detail.text
        cut = next(
            entry
            for entry in detail.json()["node_outputs"]
            if entry["node_id"] == CODE_RESEARCHER_NODE_ID
        )
        assert cut["truncated"] is True, (
            "une coupure non annoncée fait croire au lecteur qu'il a tout vu"
        )
        assert cut["returned_chars"] == 64
        assert cut["next_offset"] == 64
        total = cut["total_chars"]

        # Ce qui est annoncé est reprenable : on recolle, et on compare au
        # total que la route de détail a annoncé.
        rebuilt = cut["output"]
        offset = cut["next_offset"]
        pages = 1
        while offset is not None:
            page_resp = await client.get(
                f"/api/v1/workflows/runs/{run_id}/nodes/{CODE_RESEARCHER_NODE_ID}/output",
                headers=_auth_headers(),
                params={"offset": offset, "limit": 64},
            )
            assert page_resp.status_code == 200, page_resp.text
            page = page_resp.json()["output"]
            assert page["offset"] == offset, "une page doit dire où elle commence"
            rebuilt += page["output"]
            offset = page["next_offset"]
            pages += 1

        assert len(rebuilt) == total
        assert pages > 1, "le plafond de 64 n'a rien coupé : le test ne prouve rien"
        assert json.loads(rebuilt) == json.loads(_RESEARCHER_ANSWER)


# ─── Story 5.3 — la chaîne Analyste → Producteur ─────────────────────


async def _run_the_full_chain(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    *,
    producer_answer: str = _PRODUCER_ANSWER,
) -> tuple[dict[str, Any], MockProvider]:
    """Lance un run du DAG à quatre nodes et rend (détail HTTP, provider).

    Le détail vient de l'endpoint de la Story 5.7 — la seule surface qu'un
    opérateur ait réellement, et celle que le protocole manuel utilise. Lire
    la base ici prouverait moins.
    """
    report = await _seed(app_session_factory)
    objective, canned = _CASES["scaffolding"]
    provider = MockProvider(
        "mock",
        [
            _completion(canned),
            _completion(_HANDOFF_SUMMARY),
            _completion(_RESEARCHER_ANSWER),
            _completion(_ANALYST_ANSWER),
            _completion(producer_answer),
        ],
    )
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
        detail = await client.get(f"/api/v1/workflows/runs/{run_id}", headers=_auth_headers())

    assert detail.status_code == 200, detail.text
    return detail.json(), provider


@pytest.mark.asyncio
async def test_the_producer_cites_approach_steps_the_analyst_actually_produced(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC3 — LE test de cette story, et le chaînon y est fermé des DEUX côtés.

    Les deux bouts sont lus dans la sortie que le MOTEUR a persistée et que
    l'endpoint de la Story 5.7 rend : les `id` d'étape viennent du node
    `architect_analyst`, les `approach_ref` du node `code_producer`.

    ⚠️ **Ce que la revue de la 5.3 a dû ajouter, et pourquoi.** La version
    d'origine s'arrêtait à `cited <= declared_steps`. C'est vrai, mais ça ne
    mord pas : les deux côtés sont des réponses du `MockProvider` écrites
    l'une pour l'autre, et le moteur se contente de les recopier dans
    `node_outputs`. AUCUNE mutation du code de production ne pouvait faire
    échouer cette comparaison pour la bonne raison — la mutation n°1 de la
    story la faisait bien tomber, mais par décalage de la file FIFO, avec un
    run qui finissait `error`. Un test qui tombe pour la mauvaise raison ne
    garde pas la propriété que son nom annonce.

    Le maillon manquant est posé ici : les étapes lues DEPUIS LE MOTEUR
    doivent se retrouver dans le prompt que le Producteur a réellement reçu.
    Si le régime de passage repassait au résumé, ou si la sortie de l'Analyste
    cessait d'atteindre son aval, cette assertion-ci tombe la première, et
    elle tombe en le disant.
    """
    body, provider = await _run_the_full_chain(app_session_factory, workflow_checkpointer)

    outputs = {entry["node_id"]: entry for entry in body["node_outputs"]}
    for node_id in (ARCHITECT_ANALYST_NODE_ID, CODE_PRODUCER_NODE_ID):
        # Lire `truncated` AVANT de parser — la sortie du Producteur porte des
        # diffs et des tests, donc c'est la plus grosse du DAG, et
        # `AGENTIVE_RUN_NODE_OUTPUT_MAX_CHARS` vaut 8 000 par défaut contre un
        # `max_tokens` de 16 000. Sans cette garde, une fixture qui grossirait
        # ferait échouer le test sur un `JSONDecodeError` très loin du sujet.
        assert outputs[node_id]["truncated"] is False, (
            f"{node_id} : la sortie est paginée, le JSON lu ici est un fragment — "
            "recoller les pages par `next_offset` avant d'asserter quoi que ce soit"
        )
    analyst = json.loads(outputs[ARCHITECT_ANALYST_NODE_ID]["output"])
    producer = json.loads(outputs[CODE_PRODUCER_NODE_ID]["output"])

    declared_steps = {step["id"] for step in analyst["approach"]["steps"]}
    assert declared_steps, "l'Analyste n'a déclaré aucune étape : il n'y a rien à référencer"

    # Le maillon amont : ce que le moteur a produit est ce que le Producteur a VU.
    producer_prompt = "".join(
        message.content for message in provider.calls[4]["messages"] if message.role == "user"
    )
    for step_id in sorted(declared_steps):
        assert step_id in producer_prompt, (
            f"l'étape {step_id}, que l'Analyste a RÉELLEMENT produite d'après la sortie "
            "lue depuis le moteur, n'apparaît pas dans le prompt du Producteur : il ne "
            "peut pas s'appuyer sur une approche qu'il n'a pas reçue"
        )

    # Le maillon aval : ce que le Producteur a cité vient de ce qu'il a vu.
    cited = {diff["approach_ref"] for diff in producer["code_diffs"]}
    assert cited, "le Producteur ne cite aucune étape : l'AC3 n'a rien à vérifier"
    assert cited <= declared_steps, (
        f"le Producteur cite des étapes que l'Analyste n'a pas produites : "
        f"{sorted(cited - declared_steps)}"
    )


@pytest.mark.asyncio
async def test_both_new_contracts_are_honoured_at_runtime_not_only_declared(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC1, second niveau — « déclaré » et « honoré » sont deux affirmations.

    La Story 5.2 avait déclaré quatre clés sans rien brancher dessus : son
    contrat était vérifiable à l'œil et par rien d'autre. Ici les deux
    nouveaux nodes portent réellement les clés de leur `output_contract.core`,
    et le moteur ne relève AUCUN `contract_problems` — l'absence de la clé
    étant elle-même le signal.
    """
    body, _provider = await _run_the_full_chain(app_session_factory, workflow_checkpointer)

    outputs = {entry["node_id"]: entry for entry in body["node_outputs"]}
    for node_id in (ARCHITECT_ANALYST_NODE_ID, CODE_PRODUCER_NODE_ID):
        # Cf. le test d'AC3 : `truncated` se lit AVANT le `json.loads`, sinon
        # une sortie paginée échoue en `JSONDecodeError` au lieu de dire
        # qu'elle est coupée.
        assert outputs[node_id]["truncated"] is False, (
            f"{node_id} : sortie paginée, ce JSON est un fragment"
        )
    analyst = json.loads(outputs[ARCHITECT_ANALYST_NODE_ID]["output"])
    producer = json.loads(outputs[CODE_PRODUCER_NODE_ID]["output"])

    assert {"approach", "tradeoffs", "risks", "test_strategy"} <= set(analyst)
    assert {"code_diffs", "tests", "docs_snippets"} <= set(producer)

    for node_id in (ARCHITECT_ANALYST_NODE_ID, CODE_PRODUCER_NODE_ID):
        assert "contract_problems" not in body["metrics"]["per_node"][node_id], (
            f"{node_id} : une sortie conforme ne doit produire aucun constat"
        )


@pytest.mark.asyncio
async def test_a_production_that_cites_no_approach_is_reported_not_swallowed(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC1 — la contrepartie : une sortie NON conforme est NOMMÉE.

    Sans ce test, le précédent prouverait seulement que le chemin nominal
    passe — et un validateur qui rendrait `[]` inconditionnellement le
    passerait aussi. C'est exactement la circularité que la revue de la Story
    5.1 a trouvée sur son test d'AC3.

    Le run reste `completed` : on marque, on ne refuse pas. Tuer un run sur une
    maladresse de format serait disproportionné quand le cas non parsable est
    déjà traité par le repli `raw_output`.
    """
    rogue = json.loads(_PRODUCER_ANSWER)
    for diff in rogue["code_diffs"]:
        del diff["approach_ref"]

    body, _provider = await _run_the_full_chain(
        app_session_factory,
        workflow_checkpointer,
        producer_answer=json.dumps(rogue, ensure_ascii=False),
    )

    problems = body["metrics"]["per_node"][CODE_PRODUCER_NODE_ID]["contract_problems"]
    assert problems
    assert any("approach_ref" in problem for problem in problems)
    assert body["status"] == "completed", "un contrat non honoré se signale, il ne tue pas le run"


@pytest.mark.asyncio
async def test_the_producer_reads_the_analysts_raw_output_not_a_handoff_summary(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """T1 — la preuve que l'arbitrage du régime de passage est réellement posé.

    Un résumé de passage rend `{decisions, artifacts_refs, blockers,
    next_questions}` : les `id` d'étape de l'Analyste n'y survivent PAS, et
    l'AC3 deviendrait invérifiable. Ce test regarde ce que le modèle a
    réellement reçu — `provider.calls`, le seul endroit où le prompt est
    observable — plutôt que ce que la configuration prétend.

    Il garde AUSSI le décompte d'appels, qui est l'autre moitié de
    l'arbitrage : deux arêtes ne produisent plus de résumé du tout, donc cinq
    appels et non sept.
    """
    _body, provider = await _run_the_full_chain(app_session_factory, workflow_checkpointer)

    assert len(provider.calls) == 5, (
        "attendu : 4 nodes + 1 seul résumé de passage (celui du Dev Lead). "
        "Sept appels voudraient dire que les résumés n'ont pas été supprimés."
    )

    producer_prompt = "".join(
        message.content for message in provider.calls[4]["messages"] if message.role == "user"
    )
    analyst_steps = [step["id"] for step in json.loads(_ANALYST_ANSWER)["approach"]["steps"]]
    for step_id in analyst_steps:
        assert f'"id": "{step_id}"' in producer_prompt, (
            f"l'étape {step_id} n'est pas arrivée jusqu'au Producteur — "
            "il a reçu un résumé, pas la sortie de l'Analyste"
        )
    # Le marqueur inverse : aucune des quatre clés d'un résumé de passage.
    assert "next_questions" not in producer_prompt


@pytest.mark.asyncio
async def test_the_upstream_reaching_the_producer_is_wrapped_exactly_once(
    app_session_factory: async_sessionmaker[AsyncSession],
    workflow_checkpointer: Any,
    outbox_worker: Any,
) -> None:
    """AC2 — « il wrappe tous les `<tool_output>` de Code Researcher ».

    Le moteur le fait déjà (`_build_user_message`) : ce que cette AC exige est
    donc une PREUVE sur ce node, et l'absence d'une seconde enveloppe. Une
    double enveloppe échapperait les `<` de la première et donnerait du bruit
    au modèle — piège #6 de la Story 5.1, repris par T5.3 de la 5.2.
    """
    _body, provider = await _run_the_full_chain(app_session_factory, workflow_checkpointer)

    producer_prompt = "".join(
        message.content for message in provider.calls[4]["messages"] if message.role == "user"
    )
    assert producer_prompt.count("<tool_output>") == 1
    assert producer_prompt.count("</tool_output>") == 1
    assert producer_prompt.count("<user_input>") == 1
    # La marque d'une double enveloppe : le `<` de la première, échappé par la
    # seconde. Son absence est ce qui distingue « enveloppé » de « enveloppé
    # deux fois ».
    assert "&lt;tool_output&gt;" not in producer_prompt
    # Et l'enveloppe porte bien la sortie amont, pas une coquille vide.
    assert "relevant_files" in producer_prompt

    # La borne qui remplace les résumés, mesurée là où elle s'applique.
    #
    # ⚠️ Ce que cette assertion établit et ce qu'elle n'établit pas : sur les
    # fixtures de ce fichier, le prompt du dernier node pèse quelques kilo-
    # octets, donc le plafond n'est PAS exercé — il est seulement prouvé que la
    # borne est celle qui gouverne ce chemin. Sur une exploration réelle, la
    # sortie du Chercheur est bien plus grosse et c'est ELLE que l'éviction
    # retirerait en premier (la plus grosse entrée d'abord). La vérification
    # sur un vrai run est une recette du runbook, § 6 ter, pas un test.
    from agentive_backend.features.workflow_engine.engine.agent_node import (
        MAX_UPSTREAM_OUTPUT_CHARS,
    )

    assert len(producer_prompt) < MAX_UPSTREAM_OUTPUT_CHARS
    assert "...[TRUNCATED]" not in producer_prompt


@pytest.mark.asyncio
async def test_the_two_new_templates_are_provisioned_with_their_tools_and_contracts(
    app_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AC1, premier niveau — vu depuis l'API, pas depuis le catalogue.

    C'est la surface que l'AC nomme (`GET /api/v1/agents/templates/{id}` et
    `.../tools`), et c'est la seule qui prouve que la déclaration a traversé
    le provisioning : la revue de la Story 5.1 a trouvé que `tools: []` faisait
    sortir `_assign_tools` avant toute résolution, donc qu'un mécanisme
    « livré » n'était atteint par aucun test.
    """
    report = await _seed(app_session_factory)
    app = _make_app(session_factory=app_session_factory)
    transport = httpx.ASGITransport(app=app)

    expected = {
        ARCHITECT_ANALYST_NODE_ID: (
            "analyste",
            {"approach", "tradeoffs", "risks", "test_strategy"},
            {"read_file", "search_content"},
        ),
        CODE_PRODUCER_NODE_ID: (
            "producteur",
            {"code_diffs", "tests", "docs_snippets"},
            {"read_file", "search_content", "find_files"},
        ),
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for key, (archetype, core, tools) in expected.items():
            template_id = report.template_ids[key]
            detail = await client.get(
                f"/api/v1/agents/templates/{template_id}", headers=_auth_headers()
            )
            assert detail.status_code == 200, detail.text
            body = detail.json()
            assert body["archetype"] == archetype
            assert set(body["config"]["output_contract"]["core"]) == core
            # T1 — la clé a bien traversé le DTO, le VO et le provisioning.
            assert body["config"]["include_raw_previous_output"] is True

            tools_resp = await client.get(
                f"/api/v1/agents/templates/{template_id}/tools", headers=_auth_headers()
            )
            assert tools_resp.status_code == 200, tools_resp.text
            assigned = tools_resp.json()["assigned_tools"]
            assert {tool["name"] for tool in assigned} == tools, (
                f"{key} : les outils déclarés n'ont pas atteint la base"
            )

    # AC2 — le Producteur déclare le namespace `métier` que son AC nomme.
    producer = report.template_ids[CODE_PRODUCER_NODE_ID]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get(f"/api/v1/agents/templates/{producer}", headers=_auth_headers())
    assert detail.json()["config"]["push_memory"] == {"namespace": "dev-metier", "optin": False}
