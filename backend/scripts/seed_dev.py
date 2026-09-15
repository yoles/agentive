"""Provisioning du Pôle Dev — serveurs MCP, namespaces, agent-templates, workflow.

Story 5.1 (FR43/FR44), étendu par la Story 5.2 (FR51). Le stub de Sprint 0
s'assignait déjà nommément ce travail (« Story 5.1-5.6 : seed 9 agents Pôle
Dev ») ; le voici.

Usage::

    AGENTIVE_ALLOW_MCP_REGISTRATION=true make seed-dev
    # ou, dans le container backend :
    uv run python -m scripts.seed_dev

**Le drapeau est exigé, pas contourné.** ``AGENTIVE_ALLOW_MCP_REGISTRATION``
garde le ROUTEUR (``tool_hub/router.py``), pas le service que ce script
appelle, et il vaut ``false`` par défaut. Enregistrer un serveur MCP en
profitant d'être du bon côté de l'import serait la définition du
contournement : le provisioning refuse et NOMME le réglage (Story 5.2 T3.4).

**Ordre : les serveurs MCP AVANT les namespaces et les templates.** C'est la
même règle que « namespaces avant templates », une couche plus bas : un
template qui déclare un outil dont aucun serveur n'expose le nom fait échouer
le provisioning — après avoir créé le template. Provisionner les serveurs
d'abord, c'est refuser avant d'écrire.

**Idempotent, sous une condition qui est dite ici plutôt que vingt lignes plus bas** (revue 5.2) : tant que ni le DAG du workflow d'entrée ni les UUID de templates ne changent, une seconde exécution ne crée rien, ne duplique rien, et
n'émet aucun event d'audit gratuit : une config déjà conforme est rapportée
``unchanged``. La recherche du template se fait par NOM et non par
``(name, version=1)`` — ``update_template`` incrémente ``version`` sur la row
existante dès qu'un ``system_prompt`` change, donc chercher la version 1
aurait créé un second template à chaque re-exécution.

**Ordre imposé : les namespaces AVANT les templates.** Un template dont le
``push_memory.namespace`` n'existe pas fait REFUSER tout lancement de run par
la Mise en Place (``mise_en_place._check_namespaces``, Story 4.5 AC2 — échec
permanent, donc 422). Provisionner les templates d'abord livrerait des agents
qui ne peuvent pas démarrer.

**Un outil déclaré introuvable fait échouer le provisioning.** Jamais un
template silencieusement dépourvu d'outils : c'est indiscernable d'un template
sans outils, et c'est exactement le mensonge par omission que
``tool_invocations=[]`` a entretenu deux epics durant (D80, fermée par la
Story 5.0). Même refus si un nom est ambigu — deux serveurs MCP peuvent
exposer le même nom d'outil, et « prendre le premier » ferait dépendre
l'assignation de l'ordre de retour de Postgres.

Ce module n'appelle AUCUN LLM et ne lance aucun run : il configure.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentive_backend.features.agent_registry import (
    AgentTemplateService,
    TemplateToolAssignmentService,
    load_registry,
)
from agentive_backend.features.agent_registry.dev_catalog import (
    DevAgentDefinition,
    DevMcpServerDefinition,
    DevNamespaceRequirement,
    catalog_namespaces,
    load_dev_catalog,
    load_dev_servers,
)
from agentive_backend.features.tool_hub import ToolHubService
from agentive_backend.features.workflow_engine.schemas import (
    WorkflowEdgeRequest,
    WorkflowNodeRequest,
)
from agentive_backend.features.workflow_engine.service import WorkflowService
from agentive_backend.infra.db.models import Namespace
from agentive_backend.infra.db.session import get_engine, get_session_factory
from agentive_backend.infra.mcp.client import discover_tools
from agentive_backend.infra.mcp.policy import (
    apply_sandbox_policy,
    code_search_connection_config,
    is_code_search_server,
)
from agentive_backend.shared.correlation import new_correlation_id, set_correlation_id
from agentive_backend.shared.exceptions import ConflictError, ForbiddenError
from agentive_backend.shared.repositories import (
    AgentTemplateRepo,
    AgentTemplateToolRepo,
    NamespaceRepo,
    PromptRepo,
    ToolRepo,
    ToolServerRepo,
    WorkflowRepo,
)

logger = logging.getLogger(__name__)

#: Le nom que la Story 5.1 a donné au workflow d'entrée, quand il était
#: MONO-NODE. Conservé ici parce que le provisioning doit pouvoir le
#: reconnaître en base et dire ce qu'il en fait (cf
#: :meth:`DevDepartmentSeeder._provision_entry_workflow`).
DEV_LEAD_WORKFLOW_NAME_V1 = "Dev Lead — prise de demande"

#: Le workflow d'entrée du pôle. C'est le `workflow_id` sur lequel John poste
#: ses demandes en Sprint 2 (dogfooding en client HTTP — décision du
#: 2026-09-14).
#:
#: **Le nom porte une version, et c'est un arbitrage, pas une coquetterie.**
#: `create_workflow` est idempotent par empreinte SHA-256 de `{name, dag}`, et
#: `workflows.name` n'est PAS unique (`infra/db/models.py`). Passer le DAG de
#: un à deux nodes change l'empreinte : sans changer le nom, cette story
#: créerait une SECONDE ligne « Dev Lead — prise de demande » à côté de celle
#: de la Story 5.1, et le `workflow_id` que John a noté pointerait toujours sur
#: l'ancienne — un seed qui rapporte « créé » à chaque exécution et deux
#: homonymes que rien ne distingue. C'est le défer que la revue de la 5.1 a
#: nommément confié à cette story ; le silence était le seul choix interdit.
#:
#: Les deux autres options ont été écartées : retirer l'ancien workflow
#: demanderait un chemin de suppression qui n'existe nulle part dans le dépôt
#: (constat de la Story 4.15), et se contenter d'une recherche par nom
#: laisserait deux lignes homonymes en base, donc exactement l'ambiguïté qu'on
#: cherche à supprimer. Un nom versionné rend les deux workflows distincts
#: DANS la table, lisibles par un opérateur, et sans code de suppression.
DEV_ENTRY_WORKFLOW_NAME = "Pôle Dev — prise de demande (v2 : dev_lead → code_researcher)"
DEV_LEAD_NODE_ID = "dev_lead"
CODE_RESEARCHER_NODE_ID = "code_researcher"

#: Les clés de catalogue que le workflow d'entrée exige. Le `node_id` du
#: DAG et la `key` du catalogue coïncident délibérément : c'est ce qui rend
#: `metrics.per_node["code_researcher"]` lisible sans table de
#: correspondance, et c'est la clé que l'AC2 nomme.
_ENTRY_NODE_KEYS = ("dev_lead", "code_researcher")

#: Les clés de ``agent_templates.config`` que ce provisioning possède. Une clé
#: absente d'ici est laissée intacte (``update_template`` a une sémantique
#: PATCH au premier niveau) — c'est ce qui permet de comparer « ce que le
#: catalogue demande » à « ce qui est en base » sans écraser ce qu'un opérateur
#: aurait réglé à la main sur un autre champ.
_OWNED_CONFIG_KEYS = (
    "system_prompt",
    "input_contract",
    "output_contract",
    "llm_model",
    "llm_params",
    "error_policy",
    "push_memory",
)


#: Les fabriques d'``connection_config``, une par ``launcher`` du jeu fermé.
#: Une table plutôt qu'un `if` : ajouter un serveur interne dans une story
#: ultérieure se fait en ajoutant une entrée ici ET une valeur au ``Literal``
#: de ``DevMcpServerDefinition.launcher``, donc jamais par mégarde.
_LAUNCHERS = {"code_search": code_search_connection_config}


def _connection_config_for(server: DevMcpServerDefinition) -> dict[str, Any]:
    """La configuration de connexion d'un serveur du catalogue.

    Construite par ``infra/mcp/policy.py``, jamais déclarée dans le YAML : la
    commande, l'``argv`` et surtout les RACINES autorisées sont une règle de
    sécurité, et elles viennent de ``Settings``. Cette valeur est persistée
    pour qu'un opérateur qui lit ``tool_servers`` voie la commande réelle —
    mais elle est de toute façon RÉÉCRITE à chaque appel par
    ``apply_sandbox_policy``, donc un ``UPDATE`` dessus n'élargit rien.
    """
    factory = _LAUNCHERS.get(server.launcher)
    if factory is None:  # pragma: no cover — le `Literal` le rend inatteignable
        raise SeedError(f"launcher inconnu pour le serveur '{server.key}' : {server.launcher}")
    return factory()


class SeedError(RuntimeError):
    """Un prérequis manque — le provisioning s'arrête et le dit."""


@dataclass
class SeedReport:
    """Ce que l'exécution a fait, dans l'ordre où elle l'a fait."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    template_ids: dict[str, UUID] = field(default_factory=dict)
    server_ids: dict[str, UUID] = field(default_factory=dict)
    workflow_id: UUID | None = None

    def render(self) -> str:
        lines = [
            f"créés      : {len(self.created)}",
            *(f"  + {item}" for item in self.created),
            f"mis à jour : {len(self.updated)}",
            *(f"  ~ {item}" for item in self.updated),
            f"inchangés  : {len(self.unchanged)}",
            *(f"  = {item}" for item in self.unchanged),
        ]
        if self.workflow_id is not None:
            lines.append(f"workflow d'entrée : {self.workflow_id}")
        return "\n".join(lines)


def desired_config_subset(definition: DevAgentDefinition) -> dict[str, Any]:
    """Ce que le catalogue veut voir dans ``agent_templates.config``.

    Sérialisé par les mêmes DTO que ceux qu'``update_template`` consomme, donc
    comparable tel quel à la config persistée : c'est ce qui rend la détection
    « rien n'a changé » fiable plutôt qu'approximative.
    """
    # `exclude_none` NON : il est RÉCURSIF, et un `null` imbriqué peut être
    # signifiant. Les `None` de PREMIER niveau, eux, sont filtrés juste en
    # dessous : ils signifient « le catalogue ne possède pas ce champ ».
    #
    # Ce dict est comparé à ce que la base CONTIENT, pas à ce que le DTO
    # sérialise — et les deux ne coïncident pas partout. `PushMemorySettings.
    # to_mapping()` rend `{}` (et non `{"namespace": None, "optin": false}`)
    # quand Push Memory est éteint : comparer la forme du DTO donnerait une
    # inégalité perpétuelle, donc un `update_template` et un event d'audit à
    # CHAQUE exécution — l'exact contraire de l'idempotence annoncée. La
    # normalisation ci-dessous aligne le désiré sur la forme persistée.
    payload = definition.to_update_request().model_dump(mode="json")
    desired = {
        key: value
        for key, value in payload.items()
        if key in _OWNED_CONFIG_KEYS and value is not None
    }
    return {key: _as_persisted(key, value) for key, value in desired.items()}


def _as_persisted(key: str, value: Any) -> Any:
    """La forme sous laquelle ``value`` atterrit réellement dans ``config``.

    Aujourd'hui une seule clé diverge entre le DTO et la base : ``push_memory``
    éteint, que ``PushMemorySettings.to_mapping()`` réduit à ``{}``. La
    fonction existe pour que la prochaine divergence se déclare ici plutôt que
    de se manifester en ré-écriture silencieuse.
    """
    if (
        key == "push_memory"
        and isinstance(value, dict)
        and value.get("namespace") is None
        and value.get("optin") is False
    ):
        return {}
    return value


def config_is_current(stored: object, desired: dict[str, Any]) -> bool:
    """``True`` si chaque clé possédée est déjà à la valeur voulue."""
    if not isinstance(stored, dict):
        return False
    return all(stored.get(key) == value for key, value in desired.items())


def stale_owned_keys(stored: object, desired: dict[str, Any]) -> list[str]:
    """Les clés possédées qui sont EN BASE mais plus dans le catalogue.

    ``update_template`` a une sémantique PATCH : un champ absent de la requête
    est laissé intact, il n'est pas effacé. Retirer ``llm_model`` du YAML ne
    dépingle donc PAS le modèle — la valeur reste en base pour toujours, et
    sans ce contrôle le provisioning rapporterait ``inchangé`` sur une config
    périmée, ce qui est la forme de mensonge la plus coûteuse : un rapport
    vert sur un état faux.

    Le provisioning refuse plutôt que de corriger : effacer un champ que
    l'opérateur a peut-être réglé à la main serait décider à sa place, et il
    n'existe aujourd'hui aucun chemin d'API pour le remettre à ``null``.
    """
    if not isinstance(stored, dict):
        return []
    return sorted(
        key
        for key in _OWNED_CONFIG_KEYS
        if key not in desired and stored.get(key) not in (None, {}, [])
    )


class DevDepartmentSeeder:
    """Provisionne le Pôle Dev à partir du catalogue YAML.

    Passe par les SERVICES (``AgentTemplateService``,
    ``TemplateToolAssignmentService``, ``WorkflowService``) et non par du SQL
    direct : ce sont eux qui portent la validation, le versionnage des prompts
    et la publication atomique des events d'audit. Écrire les rows à la main
    donnerait un état indiscernable en base et invisible à l'audit.

    **Une exception, assumée et nommée : les namespaces.** Ils passent par
    ``NamespaceRepo`` et non par ``MemoryManagerService``, donc ils n'émettent
    PAS de ``NamespaceCreatedEvent``. La raison est que ce service exige un
    ``embedding_router`` — des providers d'embedding et leurs clés — pour
    créer une row qui ne porte aucun vecteur. Imposer cette dépendance à un
    script de provisioning serait un coût plus grand que le trou d'audit
    qu'il comble. Ce que le provisioning garantit à la place : chaque
    namespace créé apparaît dans le rapport rendu à l'opérateur. Si l'audit
    de ces créations devient un besoin, c'est le service qu'il faudra
    câbler — pas un event publié à la main ici, qui divergerait du sien.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        tenant_id: UUID | None = None,
    ) -> None:
        self._template_repo = AgentTemplateRepo(session_factory=session_factory)
        self._namespace_repo = NamespaceRepo(session_factory=session_factory)
        self._tool_repo = ToolRepo(session_factory=session_factory)
        self._assignment_repo = AgentTemplateToolRepo(session_factory=session_factory)
        self._workflow_repo = WorkflowRepo(session_factory=session_factory)
        self._tenant_id = tenant_id
        self._template_service = AgentTemplateService(
            registry=load_registry(),
            template_repo=self._template_repo,
            prompt_repo=PromptRepo(session_factory=session_factory),
        )
        self._tool_service = TemplateToolAssignmentService(
            template_repo=self._template_repo,
            tool_repo=self._tool_repo,
            assignment_repo=self._assignment_repo,
        )
        self._workflow_service = WorkflowService(
            workflow_repo=self._workflow_repo,
            template_repo=self._template_repo,
        )
        self._server_repo = ToolServerRepo(session_factory=session_factory)
        self._tool_hub_service = ToolHubService(
            server_repo=self._server_repo,
            tool_repo=self._tool_repo,
            template_repo=self._template_repo,
        )

    async def run(
        self,
        catalog: dict[str, DevAgentDefinition] | None = None,
        *,
        servers: Sequence[DevMcpServerDefinition] | None = None,
        report: SeedReport | None = None,
    ) -> SeedReport:
        """Provisionne tout le catalogue : serveurs MCP, puis namespaces, puis
        templates, puis workflow d'entrée.

        ``report`` est un mot-clé optionnel : le fournir laisse l'appelant
        garder le rapport PARTIEL si le provisioning s'interrompt en cours de
        route. Sans lui, un échec emporte la liste de ce qui a déjà été créé.
        """
        definitions = dict(catalog) if catalog is not None else dict(load_dev_catalog())
        report = report if report is not None else SeedReport()

        # Les serveurs MCP AVANT tout le reste. C'est la même règle d'ordre
        # que « namespaces avant templates », une couche plus bas : un
        # template qui déclare un outil dont aucun serveur n'expose le nom
        # fait échouer le provisioning (`_assign_tools`), et il le fait
        # APRÈS avoir créé le template. Provisionner les serveurs d'abord,
        # c'est refuser avant d'écrire.
        await self._provision_servers(
            servers if servers is not None else load_dev_servers(), report
        )
        await self._provision_namespaces(catalog_namespaces(definitions), report)
        for key in sorted(definitions):
            await self._provision_agent(definitions[key], report)
        await self._provision_entry_workflow(report)
        return report

    async def _provision_servers(
        self, servers: Sequence[DevMcpServerDefinition], report: SeedReport
    ) -> None:
        """Enregistre les serveurs MCP du catalogue (Story 5.2 T3).

        **Le drapeau, et l'endroit où il garde.** ``AGENTIVE_ALLOW_MCP_REGISTRATION``
        vaut ``false`` par défaut (``docker-compose.yml``). La revue de cette
        story a déplacé le contrôle dans ``ToolHubService.connect_server`` : il
        gardait le ROUTEUR seul, et cette story l'avait re-implémenté ICI —
        c'est-à-dire dans l'appelant, ce qui laissait le trou ouvert pour le
        suivant. Deux effets : le prochain script in-process ne peut plus
        passer dessous, et la porte ne se déclenche QUE lorsqu'un
        enregistrement a réellement lieu. Une seconde exécution qui se contente
        de vérifier l'existant n'a donc plus besoin du drapeau — ce qui rend
        son activation signifiante au lieu d'être une case à cocher permanente.

        **``connect_server`` ne sait pas re-découvrir.** Un nom déjà pris lève
        ``ConflictError`` AVANT même de spawner le serveur
        (``tool_hub/service.py``). La seconde exécution relit donc la row et
        vérifie que le serveur expose toujours les outils déclarés — mirror de
        ``_assert_namespace_matches`` : « il existe » et « il est ce que le
        catalogue demande » sont deux affirmations différentes, et c'est la
        seconde qui conditionne le démarrage des runs.
        """
        if not servers:
            return
        for server in servers:
            await self._provision_server(server, report)

    async def _provision_server(self, server: DevMcpServerDefinition, report: SeedReport) -> None:
        label = f"serveur MCP {server.name}"
        existing = await self._get_server_by_name(server.name)
        if existing is not None:
            self._assert_server_matches(server, existing)
            await self._assert_server_exposes(server, existing.id)
            report.unchanged.append(label)
            report.server_ids[server.key] = existing.id
            return
        # Hors du `try` : une `SeedError` « launcher inconnu » y était rattrapée
        # par le `except Exception` et reconditionnée en « la découverte a
        # échoué — vérifier que la commande démarre dans le container »,
        # envoyant l'opérateur inspecter Docker pour une table Python
        # incomplète (revue 5.2).
        connection_config = _connection_config_for(server)
        try:
            detail = await self._tool_hub_service.connect_server(
                name=server.name,
                transport=server.transport,
                connection_config=connection_config,
                tenant_id=self._tenant_id,
            )
        except ForbiddenError as exc:
            raise SeedError(
                f"{label} : enregistrement refusé — AGENTIVE_ALLOW_MCP_REGISTRATION vaut "
                "false. Relancer avec `AGENTIVE_ALLOW_MCP_REGISTRATION=true make seed-dev`, "
                "en connaissance de cause : sous le repli `setrlimit` (pas de bwrap), "
                "l'exécution d'outils MCP n'a AUCUNE isolation réseau ni filesystem, et "
                "`app.lifespan._enforce_mcp_sandbox_policy` refuse de démarrer en production "
                "dans cette combinaison. Une exécution qui n'enregistre RIEN (tout est déjà "
                "provisionné) n'a pas besoin du drapeau."
            ) from exc
        except ConflictError:
            # Course avec un autre provisioning — même traitement que pour les
            # namespaces et les templates : relire, puis VÉRIFIER, jamais
            # supposer conforme.
            raced = await self._get_server_by_name(server.name)
            if raced is None:
                raise SeedError(
                    f"{label} : enregistrement refusé en conflit, mais le serveur reste "
                    "introuvable. Rejouer le provisioning."
                ) from None
            await self._assert_server_exposes(server, raced.id)
            report.unchanged.append(label)
            report.server_ids[server.key] = raced.id
            return
        except Exception as exc:
            # `connect_server` traduit un échec de spawn en `DependencyError`
            # 503. C'est le point exact où un serveur « enregistrable sur le
            # papier » se révèle impossible à lancer, et il vaut mieux
            # l'apprendre ici qu'au premier `POST /runs`.
            command = connection_config.get("command", "?")
            args = " ".join(str(a) for a in connection_config.get("args", []))
            raise SeedError(
                f"{label} : la découverte a échoué ({type(exc).__name__}: {exc}). "
                "Rejouer la commande RÉELLEMENT enregistrée dans le container — c'est "
                "celle-là qui échoue, pas une invocation équivalente :\n"
                f"    {command} {args}\n"
                "Vérifier aussi que AGENTIVE_DEV_CODE_ROOTS pointe des répertoires "
                "existants."
            ) from exc
        await self._assert_server_exposes(server, detail.server_id)
        report.created.append(f"{label} ({detail.tools_count} outils découverts)")
        report.server_ids[server.key] = detail.server_id

    @staticmethod
    def _assert_server_matches(server: DevMcpServerDefinition, existing: Any) -> None:
        """« Il existe » ne dit pas « il est ce que le catalogue demande ».

        Revue 5.2 — seuls les NOMS d'outils étaient comparés. Une row
        enregistrée autrement (par le routeur HTTP, par un seed antérieur, en
        `sse`) et exposant quatre outils homonymes passait `unchanged`. Pire,
        elle échappait à `apply_sandbox_policy`, dont la reconnaissance porte
        sur la tête de l'`argv` : un serveur affichant les bons noms mais lancé
        par une autre commande n'aurait reçu AUCUN profil.
        """
        if existing.transport != server.transport:
            raise SeedError(
                f"serveur MCP {server.name} : transport en base `{existing.transport}`, "
                f"catalogue `{server.transport}`. Supprimer la row et rejouer le "
                "provisioning — le transport décide du sandbox appliqué."
            )
        if server.launcher == "code_search" and not is_code_search_server(
            existing.connection_config
        ):
            raise SeedError(
                f"serveur MCP {server.name} : la `connection_config` en base ne lance pas "
                "le serveur de lecture de code du dépôt. Elle échapperait donc à la "
                "politique de sandbox (`apply_sandbox_policy` reconnaît la tête de "
                "l'argv). Supprimer la row et rejouer le provisioning."
            )

    async def _get_server_by_name(self, name: str) -> Any:
        """La row ``tool_servers`` de ce nom, ou ``None``.

        ``ToolServerRepo`` n'expose que ``get_by_name_in_session`` — la
        transaction est donc ouverte ici plutôt qu'ajoutée au repo : ce script
        est le seul appelant hors session, et une méthode de plus sur un repo
        partagé se paie en surface à maintenir.
        """
        async with self._server_repo.with_tenant(self._tenant_id) as session:
            return await self._server_repo.get_by_name_in_session(
                session, name=name, tenant_id=self._tenant_id
            )

    async def _assert_server_exposes(self, server: DevMcpServerDefinition, server_id: UUID) -> None:
        """Refuse un serveur qui n'expose plus ce que le catalogue déclare.

        Sans ce contrôle, la seconde exécution rapporterait « inchangé » sur un
        serveur dont un outil a disparu — et l'assignation du template, elle,
        continuerait de pointer une row `tools` supprimée ou absente. Le
        symptôme apparaîtrait à la Mise en Place, au lancement du premier run,
        très loin de sa cause.

        **DEUX sources, et c'est la correction de la revue 5.2.** La version
        d'origine n'interrogeait que la table `tools`, c'est-à-dire les rows
        écrites par la PREMIÈRE découverte — alors que sa docstring, et le
        `mcp-servers.yaml`, revendiquaient « le seul endroit qui peut constater
        qu'un outil a disparu DU SERVEUR ». C'était précisément le cas qu'elle
        ne voyait pas : un binaire qui cesse d'exposer `search_content` garde
        ses rows. Le serveur est donc re-sondé pour de vrai, par le même chemin
        que la Mise en Place — un spawn par exécution du seed, ce qui est le
        bon endroit pour le payer.
        """
        async with self._tool_repo.with_tenant(self._tenant_id) as session:
            rows = await self._tool_repo.list_by_server_in_session(session, server_id)
        stored = {row.name for row in rows}
        self._refuse_missing(server, stored, source="la table `tools`")

        live = await self._probe_server(server)
        self._refuse_missing(server, live, source="le serveur lui-même (re-sondé)")

    async def _probe_server(self, server: DevMcpServerDefinition) -> set[str]:
        """Les outils que le serveur expose MAINTENANT."""
        profile, effective = apply_sandbox_policy(
            transport=server.transport,
            connection_config=_connection_config_for(server),
        )
        try:
            discovered = await discover_tools(
                transport=server.transport,
                connection_config=effective,
                profile=profile,
            )
        except Exception as exc:
            raise SeedError(
                f"le serveur MCP '{server.name}' est enregistré mais ne répond plus "
                f"({type(exc).__name__}: {exc}). La Mise en Place refuserait tout run : "
                "elle ping les serveurs avant chaque lancement."
            ) from exc
        return {tool.name for tool in discovered}

    @staticmethod
    def _refuse_missing(server: DevMcpServerDefinition, exposed: set[str], *, source: str) -> None:
        missing = sorted(set(server.tools) - exposed)
        if missing:
            raise SeedError(
                f"le serveur MCP '{server.name}' est enregistré mais {source} n'expose "
                f"plus : {', '.join(missing)} (exposés : {', '.join(sorted(exposed)) or 'aucun'}). "
                "Le template qui les déclare ne pourrait pas les assigner, et la Mise en Place "
                "refuserait tout run. Supprimer la row du serveur puis rejouer le provisioning "
                "pour forcer une nouvelle découverte — `connect_server` n'a aucun chemin de "
                "re-découverte."
            )

    async def _provision_namespaces(
        self, requirements: list[DevNamespaceRequirement], report: SeedReport
    ) -> None:
        for requirement in requirements:
            existing = await self._namespace_repo.get_by_name(
                requirement.name, tenant_id=self._tenant_id
            )
            if existing is not None:
                self._assert_namespace_matches(existing, requirement)
                report.unchanged.append(f"namespace {requirement.name}")
                continue
            try:
                await self._namespace_repo.create(
                    name=requirement.name,
                    ns_type=requirement.type,
                    department=requirement.department,
                    tenant_id=self._tenant_id,
                )
            except ConflictError:
                # Course avec un autre provisioning : le namespace existe
                # désormais. Le relire plutôt que de le supposer conforme —
                # « il existe » et « il est ce que le catalogue demande » sont
                # deux affirmations différentes, et c'est la seconde qui
                # conditionne le démarrage des runs.
                raced = await self._namespace_repo.get_by_name(
                    requirement.name, tenant_id=self._tenant_id
                )
                if raced is not None:
                    self._assert_namespace_matches(raced, requirement)
                report.unchanged.append(f"namespace {requirement.name}")
                continue
            report.created.append(f"namespace {requirement.name}")

    def _assert_namespace_matches(
        self, existing: Namespace, requirement: DevNamespaceRequirement
    ) -> None:
        """Refuse un namespace préexistant qui n'est pas celui demandé.

        ``_check_namespaces`` de la Mise en Place ne contrôle que l'EXISTENCE
        du nom : un ``dev-metier`` créé en ``type: client`` laisse donc partir
        les runs, et l'agent pousse sa mémoire dans un namespace du mauvais
        type sans que rien ne le signale. Le provisioning est le seul endroit
        qui voit les deux côtés — il lève ici, exactement comme
        ``_provision_agent`` lève sur une dérive d'archétype.
        """
        drift = [
            f"type '{existing.type}' au lieu de '{requirement.type}'"
            if existing.type != requirement.type
            else "",
            f"department {existing.department!r} au lieu de {requirement.department!r}"
            if existing.department != requirement.department
            else "",
        ]
        problems = [item for item in drift if item]
        if not problems:
            return
        raise SeedError(
            f"le namespace '{requirement.name}' existe déjà avec {', et '.join(problems)}. "
            "Le corriger ou le renommer avant de rejouer le provisioning : la Mise en Place "
            "ne contrôle que l'existence du nom, elle laisserait partir des runs qui "
            "écriraient au mauvais endroit."
        )

    async def _provision_agent(self, definition: DevAgentDefinition, report: SeedReport) -> None:
        label = f"agent-template {definition.name}"
        existing = await self._template_repo.get_latest_by_name(
            definition.name, tenant_id=self._tenant_id
        )
        if existing is None:
            try:
                created = await self._template_service.create_template(
                    name=definition.name,
                    archetype_id=definition.archetype,
                    tenant_id=self._tenant_id,
                )
            except ConflictError as exc:
                # Course avec un autre provisioning, ou reprise après un crash
                # entre la lecture et le COMMIT. `_provision_namespaces` gère
                # déjà cette course pour les namespaces ; ne pas la gérer ici
                # rendait un traceback nu et laissait un provisioning partiel
                # sans rapport.
                raced = await self._template_repo.get_latest_by_name(
                    definition.name, tenant_id=self._tenant_id
                )
                if raced is None:
                    raise SeedError(
                        f"{label} : création refusée en conflit, mais le template reste "
                        f"introuvable ({exc}). Rejouer le provisioning."
                    ) from exc
                existing = raced
            else:
                report.created.append(label)
                await self._template_service.update_template(
                    created.template_id,
                    definition.to_update_request(),
                    tenant_id=self._tenant_id,
                )
                await self._assign_tools(definition, created.template_id, report)
                report.template_ids[definition.key] = created.template_id
                return

        if existing is not None:
            if existing.archetype != definition.archetype:
                raise SeedError(
                    f"{label} existe déjà avec l'archétype '{existing.archetype}' alors que le "
                    f"catalogue déclare '{definition.archetype}'. Le changer reviendrait à "
                    "remplacer l'agent sous le même nom — renommer l'un des deux."
                )
            template_id = existing.id
            desired = desired_config_subset(definition)
            stale = stale_owned_keys(existing.config, desired)
            if stale:
                raise SeedError(
                    f"{label} porte en base des champs que le catalogue ne déclare plus : "
                    f"{', '.join(stale)}. `update_template` est un PATCH — il ne les effacera "
                    "pas, et les laisser ferait rapporter « inchangé » sur une config périmée. "
                    "Les redéclarer dans le YAML, ou remettre le template à plat."
                )
            if config_is_current(existing.config, desired):
                report.unchanged.append(label)
            else:
                await self._template_service.update_template(
                    template_id,
                    definition.to_update_request(),
                    tenant_id=self._tenant_id,
                )
                report.updated.append(label)

            await self._assign_tools(definition, template_id, report)
            report.template_ids[definition.key] = template_id

    async def _assign_tools(
        self, definition: DevAgentDefinition, template_id: UUID, report: SeedReport
    ) -> None:
        current = await self._current_tool_ids(template_id)
        if not definition.tools:
            if current:
                await self._tool_service.replace_template_tools(
                    template_id, [], tenant_id=self._tenant_id
                )
                report.updated.append(f"outils de {definition.name} (retirés)")
            else:
                # Dire « zéro outil, et c'est voulu ». Sans cette ligne, un
                # agent provisionné sans outils et un agent dont les outils
                # n'ont pas été considérés rendent le MÊME rapport — l'exacte
                # ambiguïté que D80 a coûté deux epics à lever.
                report.unchanged.append(f"outils de {definition.name} (aucun déclaré)")
            return

        duplicates = sorted({name for name in definition.tools if definition.tools.count(name) > 1})
        if duplicates:
            raise SeedError(
                f"{definition.name} déclare deux fois les mêmes outils : "
                f"{', '.join(duplicates)}. `replace_template_tools` recevrait deux fois le même "
                "id et violerait la contrainte d'unicité — et la comparaison par ensemble "
                "juste après masquerait le doublon en « inchangé »."
            )

        found = await self._tool_repo.list_by_names(definition.tools, tenant_id=self._tenant_id)
        # Revue 5.2 — `list_by_names` cherche par NOM sur toute la base, sans
        # filtrer par serveur. Si un serveur tiers expose `read_file` (un nom
        # volontairement générique, la docstring du repo le dit), le template
        # était assigné aux outils d'un serveur étranger sans qu'un seul
        # contrôle ne le remarque. On ne retient que ce que les serveurs du
        # CATALOGUE exposent — la table `tools` reste la source, le catalogue
        # décide du périmètre.
        if report.server_ids:
            provisioned = set(report.server_ids.values())
            found = {
                name: [tool for tool in tools if tool.server_id in provisioned]
                for name, tools in found.items()
            }
            found = {name: tools for name, tools in found.items() if tools}
        missing = [name for name in definition.tools if name not in found]
        if missing:
            raise SeedError(
                f"{definition.name} déclare des outils introuvables : {', '.join(sorted(missing))}. "
                "Ils doivent être exposés par un serveur du catalogue du pôle "
                "(`templates/dev/mcp-servers.yaml`) — un outil homonyme exposé par un "
                "serveur tiers ne compte pas. Enregistrer le serveur, puis rejouer le "
                "provisioning."
            )
        ambiguous = [name for name, tools in found.items() if len(tools) > 1]
        if ambiguous:
            raise SeedError(
                f"{definition.name} déclare des outils ambigus : {', '.join(sorted(ambiguous))} — "
                "exposés par plusieurs serveurs MCP. Le catalogue ne sait pas lequel choisir, et "
                "en choisir un ferait dépendre l'assignation de l'ordre de la base."
            )

        desired = [found[name][0].id for name in definition.tools]
        if set(desired) == set(current):
            report.unchanged.append(f"outils de {definition.name}")
            return
        await self._tool_service.replace_template_tools(
            template_id, desired, tenant_id=self._tenant_id
        )
        report.updated.append(f"outils de {definition.name}")

    async def _current_tool_ids(self, template_id: UUID) -> set[UUID]:
        """Les ids d'outils déjà assignés à ce template.

        ⚠️ Cette méthode était FAUSSE depuis la Story 5.1 et personne ne
        pouvait le voir : elle faisait ``row.tool_id`` alors que
        ``list_by_template_in_session`` rend des couples
        ``(Tool, assigned_at)``, donc un ``AttributeError`` sur un tuple. Elle
        n'était jamais atteinte parce que ``_assign_tools`` sort avant elle
        quand ``definition.tools`` est vide — et **aucun template du dépôt ne
        portait un seul outil**. C'est exactement le défaut que la revue de la
        5.1 a nommé (« le mécanisme est livré ET testé » alors qu'aucun test
        ne le traversait), et cette story est le premier endroit où le chemin
        est réellement emprunté.
        """
        async with self._assignment_repo.with_tenant(self._tenant_id) as session:
            rows = await self._assignment_repo.list_by_template_in_session(session, template_id)
        return {tool.id for tool, _assigned_at in rows}

    async def _provision_entry_workflow(self, report: SeedReport) -> None:
        """Le DAG à deux nodes sur lequel John poste ses demandes (Story 5.2 AC2).

        ``dev_lead → code_researcher``, sans condition d'edge : le Chercheur
        s'exécute quoi que le Dev Lead ait rendu, et lit son plan dans
        ``upstream_outputs``. Une condition aurait été possible
        (``output.status == 'done'`` est dans le ``core`` du Dev Lead, donc
        légale au regard de la validation d'edge de la Story 4.1), et elle est
        écartée délibérément : un plan `failed` porte une
        ``blocking_question``, et couper le DAG à cet endroit priverait
        l'opérateur du seul node qui pourrait lui dire ce que le codebase
        contient réellement — c'est-à-dire de quoi répondre à la question.

        ``create_workflow`` reste idempotent par empreinte (Story 4.8) : même
        nom + même DAG ⇒ la row d'origine est rendue. Ce qui est ajouté ici est
        le contrôle que l'empreinte NE COUVRE PAS — cf
        :meth:`_assert_entry_workflow_is_unique`.
        """
        missing = [key for key in _ENTRY_NODE_KEYS if key not in report.template_ids]
        if missing:
            plural = "s" if len(missing) > 1 else ""
            raise SeedError(
                f"clé{plural} d'agent absente{plural} du catalogue fourni : "
                f"{', '.join(missing)} — le workflow d'entrée du pôle n'a pas tous ses "
                "nodes à monter. Si `run(catalog=...)` a reçu un sous-ensemble volontaire, "
                "c'est l'appel qu'il faut corriger, pas le catalogue."
            )
        dev_lead_id = report.template_ids["dev_lead"]
        researcher_id = report.template_ids["code_researcher"]

        expected_nodes = {
            DEV_LEAD_NODE_ID: dev_lead_id,
            CODE_RESEARCHER_NODE_ID: researcher_id,
        }
        self._report_legacy_entry_workflow(await self._legacy_entry_workflows(), report)
        await self._assert_entry_workflow_can_be_created(expected_nodes)
        result = await self._workflow_service.create_workflow(
            name=DEV_ENTRY_WORKFLOW_NAME,
            nodes=[
                WorkflowNodeRequest(node_id=DEV_LEAD_NODE_ID, agent_template_id=dev_lead_id),
                WorkflowNodeRequest(
                    node_id=CODE_RESEARCHER_NODE_ID, agent_template_id=researcher_id
                ),
            ],
            edges=[
                WorkflowEdgeRequest(
                    from_node_id=DEV_LEAD_NODE_ID, to_node_id=CODE_RESEARCHER_NODE_ID
                )
            ],
            tenant_id=self._tenant_id,
        )
        report.workflow_id = result.workflow_id
        label = f"workflow «{DEV_ENTRY_WORKFLOW_NAME}»"
        if result.idempotent_replay:
            report.unchanged.append(label)
        else:
            report.created.append(label)

    async def _legacy_entry_workflows(self) -> list[Any]:
        return await self._workflow_repo.list_by_name(
            DEV_LEAD_WORKFLOW_NAME_V1, tenant_id=self._tenant_id
        )

    @staticmethod
    def _report_legacy_entry_workflow(legacy: list[Any], report: SeedReport) -> None:
        """Signale le workflow mono-node de la Story 5.1, TOUS les homonymes.

        Revue 5.2, deux corrections. (1) Ne cite plus `legacy[0].id` seul :
        `workflows.name` n'est pas unique, c'est tout le sujet du garde-fou
        d'à côté, et masquer les autres lignes ici serait contradictoire.
        (2) Ce rapport ne vit plus dans une méthode `_assert_*` — muter le
        rapport avant de potentiellement lever laissait un rapport partiel
        affirmant un état qui n'avait pas été atteint.
        """
        if not legacy:
            return
        ids = ", ".join(str(row.id) for row in legacy)
        plural = "s" if len(legacy) > 1 else ""
        report.unchanged.append(
            f"workflow{plural} «{DEV_LEAD_WORKFLOW_NAME_V1}» (Story 5.1, mono-node) — "
            f"laissé{plural} en place, toujours lançable{plural} : {ids}"
        )

    async def _assert_entry_workflow_can_be_created(self, expected_nodes: dict[str, UUID]) -> None:
        """Refuse de créer un SECOND workflow d'entrée — AVANT de le créer.

        ⚠️ **Corrigé par la revue de la Story 5.2 : ce contrôle arrivait un run
        trop tard.** Il ne levait que sur ``len(homonyms) > 1``. Or dans le
        scénario que T5.2 nomme mot pour mot — un template recréé, donc un UUID
        différent, donc une empreinte de DAG différente — il y a à cet instant
        **exactement un** homonyme : la garde passait, ``create_workflow``
        insérait la seconde ligne, et le rapport disait « créé ». Le refus
        n'arrivait qu'à l'exécution SUIVANTE, et il était alors définitif :
        plus aucun seed ne passait sans ``DELETE`` manuel, et il n'existe aucun
        chemin de suppression de workflow dans le dépôt (constat 4.15).

        Le contrôle porte donc maintenant sur le CONTENU : un homonyme dont le
        DAG monte les mêmes templates sur les mêmes nodes est la ligne
        idempotente attendue (``create_workflow`` la rendra en replay) ; tout
        autre homonyme est un doublon qu'il faut trancher à la main, et le
        provisioning s'arrête en nommant les deux.
        """
        homonyms = await self._workflow_repo.list_by_name(
            DEV_ENTRY_WORKFLOW_NAME, tenant_id=self._tenant_id
        )
        if not homonyms:
            return
        divergent = [row for row in homonyms if _dag_nodes_of(row) != expected_nodes]
        if len(homonyms) > 1 or divergent:
            ids = ", ".join(str(row.id) for row in homonyms)
            raise SeedError(
                f"{len(homonyms)} workflow(s) portent déjà le nom «{DEV_ENTRY_WORKFLOW_NAME}» "
                f"({ids}), dont {len(divergent)} dont le DAG ne monte pas les templates "
                "attendus. `workflows.name` n'est pas unique et l'idempotence de "
                "`create_workflow` porte sur l'empreinte du DAG : créer maintenant ajouterait "
                "une ligne de plus au lieu de mettre à jour. Supprimer les rows en trop à la "
                "main, ou repartir d'une base propre."
            )


def _dag_nodes_of(row: Any) -> dict[str, UUID]:
    """``{node_id: agent_template_id}`` du DAG persisté, pour comparaison."""
    dag = row.dag if isinstance(row.dag, dict) else {}
    nodes = dag.get("nodes")
    if not isinstance(nodes, list):
        return {}
    out: dict[str, UUID] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("node_id")
        template_id = node.get("agent_template_id")
        if isinstance(node_id, str) and template_id is not None:
            try:
                out[node_id] = UUID(str(template_id))
            except ValueError:  # pragma: no cover — DAG malformé
                continue
    return out


async def _main_async() -> int:
    set_correlation_id(new_correlation_id())
    session_factory = get_session_factory()
    seeder = DevDepartmentSeeder(session_factory=session_factory)
    report = SeedReport()
    try:
        await seeder.run(report=report)
    except SeedError as exc:
        # Le rapport PARTIEL d'abord : chaque namespace et chaque template
        # créé avant l'échec l'a été dans sa propre transaction, donc il
        # existe. Ne montrer que le message d'erreur laisserait l'opérateur
        # sans la liste de ce qu'il a déjà en base.
        _print_partial(report)
        logger.error("Provisioning du Pôle Dev interrompu : %s", exc)
        return 1
    except Exception as exc:
        # `SeedError` couvre les prérequis que ce module sait nommer. Le
        # reste — `RuntimeError` du loader de catalogue, `ValidationError`
        # d'un archétype inconnu, `OperationalError` si Postgres est
        # injoignable — rendait un traceback nu là où le module promet un
        # message d'opérateur. Le type est nommé, la cause reste dans les
        # logs de debug.
        _print_partial(report)
        logger.error("Provisioning du Pôle Dev interrompu (%s) : %s", type(exc).__name__, exc)
        logger.debug("trace complète", exc_info=True)
        return 1
    finally:
        # asyncpg garde des connexions ouvertes sur l'engine global ; sans
        # `dispose()` la boucle se ferme dessous et rend un « Event loop is
        # closed » sur un provisioning par ailleurs réussi.
        await get_engine().dispose()
    print(report.render())
    return 0


def _print_partial(report: SeedReport) -> None:
    if report.created or report.updated or report.unchanged:
        print("--- état partiel avant interruption ---")
        print(report.render())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(asyncio.run(_main_async()))


if __name__ == "__main__":
    main()
