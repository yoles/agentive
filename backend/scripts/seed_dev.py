"""Provisioning du Pôle Dev — namespaces, agent-templates, workflow d'entrée.

Story 5.1 (FR43/FR44). Le stub de Sprint 0 s'assignait déjà nommément ce
travail (« Story 5.1-5.6 : seed 9 agents Pôle Dev ») ; le voici.

Usage::

    make seed-dev
    # ou, dans le container backend :
    uv run python -m scripts.seed_dev

**Idempotent.** Une seconde exécution ne crée rien, ne duplique rien, et
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
    DevNamespaceRequirement,
    catalog_namespaces,
    load_dev_catalog,
)
from agentive_backend.features.workflow_engine.schemas import WorkflowNodeRequest
from agentive_backend.features.workflow_engine.service import WorkflowService
from agentive_backend.infra.db.models import Namespace
from agentive_backend.infra.db.session import get_engine, get_session_factory
from agentive_backend.shared.correlation import new_correlation_id, set_correlation_id
from agentive_backend.shared.exceptions import ConflictError
from agentive_backend.shared.repositories import (
    AgentTemplateRepo,
    AgentTemplateToolRepo,
    NamespaceRepo,
    PromptRepo,
    ToolRepo,
    WorkflowRepo,
)

logger = logging.getLogger(__name__)

#: Le workflow d'entrée du pôle : un seul node, le Dev Lead. C'est le
#: `workflow_id` sur lequel John poste ses demandes en Sprint 2 (dogfooding en
#: client HTTP — décision du 2026-09-14). Les Stories 5.2 → 5.6 le remplaceront
#: par un DAG multi-agents quand les rôles délégables existeront.
DEV_LEAD_WORKFLOW_NAME = "Dev Lead — prise de demande"
DEV_LEAD_NODE_ID = "dev_lead"

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


class SeedError(RuntimeError):
    """Un prérequis manque — le provisioning s'arrête et le dit."""


@dataclass
class SeedReport:
    """Ce que l'exécution a fait, dans l'ordre où elle l'a fait."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    template_ids: dict[str, UUID] = field(default_factory=dict)
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

    async def run(
        self,
        catalog: dict[str, DevAgentDefinition] | None = None,
        *,
        report: SeedReport | None = None,
    ) -> SeedReport:
        """Provisionne tout le catalogue, namespaces d'abord.

        ``report`` est un mot-clé optionnel : le fournir laisse l'appelant
        garder le rapport PARTIEL si le provisioning s'interrompt en cours de
        route. Sans lui, un échec emporte la liste de ce qui a déjà été créé.
        """
        definitions = dict(catalog) if catalog is not None else dict(load_dev_catalog())
        report = report if report is not None else SeedReport()

        await self._provision_namespaces(catalog_namespaces(definitions), report)
        for key in sorted(definitions):
            await self._provision_agent(definitions[key], report)
        await self._provision_entry_workflow(report)
        return report

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
        missing = [name for name in definition.tools if name not in found]
        if missing:
            raise SeedError(
                f"{definition.name} déclare des outils introuvables : {', '.join(sorted(missing))}. "
                "Enregistrer le serveur MCP qui les expose "
                "(POST /api/v1/tools/servers) avant de rejouer le provisioning."
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
        async with self._assignment_repo.with_tenant(self._tenant_id) as session:
            rows = await self._assignment_repo.list_by_template_in_session(session, template_id)
        return {row.tool_id for row in rows}

    async def _provision_entry_workflow(self, report: SeedReport) -> None:
        """Le workflow mono-node sur lequel John poste ses demandes (AC2).

        ``create_workflow`` est idempotent depuis la Story 4.8 (empreinte
        ``request_fingerprint`` : même nom + même DAG ⇒ la row d'origine est
        rendue, ``idempotent_replay=True``). Rien à ré-inventer ici.
        """
        dev_lead_id = report.template_ids.get("dev_lead")
        if dev_lead_id is None:
            raise SeedError(
                "le catalogue ne déclare aucun agent de clé 'dev_lead' — "
                "le workflow d'entrée du pôle n'a pas de node à monter."
            )
        result = await self._workflow_service.create_workflow(
            name=DEV_LEAD_WORKFLOW_NAME,
            nodes=[
                WorkflowNodeRequest(
                    node_id=DEV_LEAD_NODE_ID,
                    agent_template_id=dev_lead_id,
                )
            ],
            edges=[],
            tenant_id=self._tenant_id,
        )
        report.workflow_id = result.workflow_id
        label = f"workflow «{DEV_LEAD_WORKFLOW_NAME}»"
        if result.idempotent_replay:
            report.unchanged.append(label)
        else:
            report.created.append(label)


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
