"""Catalogue déclaratif des agents du Pôle Dev — Story 5.1 (FR43).

Les 9 agents du pôle vivent en YAML sous
``features/agent_registry/templates/dev/``, un fichier par agent — c'est
l'emplacement prescrit par ``architecture.md`` (§ Source layout, ligne 1402),
au nom de module réel ``agent_registry`` (cf ``docs/decisions/module-naming.md``).
La Story 5.1 livre ``dev_lead.yaml`` et le mécanisme ; les Stories 5.2 → 5.6
ajoutent leurs fichiers sans toucher à ce module.

Forme volontairement calquée sur :mod:`agentive_backend.features.agent_registry.archetypes`
— ``yaml.safe_load`` seul, Pydantic v2 ``extra="forbid"``, ``RuntimeError``
nommant le fichier fautif, mapping immuable en sortie. Cette forme a déjà été
revue (Story 2.1, P-12 / P-XX) ; la réécrire autrement serait du travail
neuf non revu pour un gain nul.

**Le prompt ne peut pas diverger du jeu de rôles.** Un ``system_prompt`` qui
contient le jeton ``${DEV_ROLES}`` le voit remplacé, au chargement, par la
liste rendue depuis :data:`~agentive_backend.shared.contracts.dev_roles.DEV_ROLES`.
Ajouter un rôle là-bas change le prompt ici sans édition. Le jeton est
``${...}`` et non ``{...}`` précisément parce que ces prompts contiennent des
accolades JSON (le contrat de sortie y est montré littéralement).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from agentive_backend.features.agent_registry.schemas import (
    ContractDefinition,
    ErrorPolicy,
    LLMModel,
    LLMParams,
    PushMemoryConfig,
    UpdateTemplateRequest,
)
from agentive_backend.shared.contracts.dev_roles import DEV_ROLE_DESCRIPTIONS, DEV_ROLES
from agentive_backend.shared.repositories.namespace_repo import NamespaceType

DEFAULT_CATALOG_DIR: Final[Path] = Path(__file__).parent / "templates" / "dev"

#: Le jeton rendu par :func:`render_dev_roles`. ``${...}`` et non ``{...}`` :
#: les prompts du pôle contiennent des accolades JSON.
DEV_ROLES_TOKEN: Final = "${DEV_ROLES}"

#: Tout jeton ``${NOM}`` restant après rendu est une faute de frappe, pas un
#: littéral : le chargement échoue plutôt que d'envoyer au modèle un prompt
#: portant un trou.
_REMAINING_TOKEN_RE: Final = re.compile(r"\$\{[^}]*\}")


class DevNamespaceRequirement(BaseModel):
    """Un namespace mémoire dont l'agent a besoin pour pouvoir démarrer.

    Ce n'est pas décoratif : ``mise_en_place._check_namespaces`` REFUSE le
    lancement d'un run dont un template référence un ``push_memory.namespace``
    inexistant (Story 4.5 AC2, échec permanent ⇒ 422). Le provisioning crée
    donc ces namespaces AVANT les templates.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    type: NamespaceType
    department: str | None = Field(default=None, max_length=100)


class DevAgentDefinition(BaseModel):
    """Un agent du Pôle Dev, tel que déclaré en YAML."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=255)
    archetype: str = Field(min_length=1, max_length=50)
    system_prompt: str = Field(min_length=1, max_length=50_000)
    input_contract: ContractDefinition
    output_contract: ContractDefinition
    # Laissé à `None` par défaut, et c'est un choix : la whitelist `LLMModel`
    # de `schemas.py` est restée aux modèles de Sprint 1 alors que
    # `agent_node.DEFAULT_LLM_MODEL` vaut `claude-sonnet-4-6`. Ne pas fixer
    # le modèle laisse le défaut du moteur s'appliquer ; le fixer exige
    # d'élargir la whitelist ET son miroir Zod côté frontend (Story 5.4, qui
    # a besoin de modèles DIFFÉRENTS entre Contrôleur et Producteur — FR15).
    llm_model: LLMModel | None = None
    llm_params: LLMParams | None = None
    error_policy: ErrorPolicy | None = None
    push_memory: PushMemoryConfig | None = None
    namespaces: list[DevNamespaceRequirement] = Field(default_factory=list)
    #: Noms d'outils MCP à assigner, résolus au provisioning. Un nom
    #: introuvable fait ÉCHOUER le provisioning : un template silencieusement
    #: dépourvu d'outils est indiscernable d'un template sans outils, et
    #: c'est exactement le mensonge par omission que `tool_invocations=[]` a
    #: entretenu deux epics durant (D80).
    tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _push_memory_namespace_is_declared(self) -> DevAgentDefinition:
        """Un ``push_memory.namespace`` doit figurer dans le bloc ``namespaces``.

        C'est la seule porte que l'ordre « namespaces AVANT templates » ne
        couvrait pas : le provisioning ne crée que ce que ``namespaces``
        déclare, donc un ``push_memory`` pointant ailleurs livrait un
        provisioning « réussi » suivi d'un ``422`` à CHAQUE lancement de run
        (``_check_namespaces``, Story 4.5 AC2) — exactement la panne que cet
        ordre existe pour empêcher.
        """
        target = self.push_memory.namespace if self.push_memory is not None else None
        if target is None:
            return self
        declared = {requirement.name for requirement in self.namespaces}
        if target not in declared:
            raise ValueError(
                f"push_memory.namespace '{target}' n'est pas déclaré dans `namespaces` "
                f"(déclarés : {', '.join(sorted(declared)) or 'aucun'}). Le provisioning ne "
                "le créerait pas, et la Mise en Place refuserait tout run avec un 422."
            )
        return self

    def to_update_request(self) -> UpdateTemplateRequest:
        """La config complète, sous la forme que ``update_template`` attend.

        Passer par le DTO HTTP plutôt que d'écrire ``config`` à la main :
        c'est lui qui porte les bornes (``temperature``, ``max_tokens``,
        ``extra="forbid"``, ``push_memory.optin`` sans namespace refusé), et
        les dupliquer ici les ferait diverger au premier changement.
        """
        return UpdateTemplateRequest(
            system_prompt=self.system_prompt,
            input_contract=self.input_contract,
            output_contract=self.output_contract,
            llm_model=self.llm_model,
            llm_params=self.llm_params,
            error_policy=self.error_policy,
            push_memory=self.push_memory,
        )


def render_dev_roles() -> str:
    """Les 8 rôles délégables, une ligne chacun, ordre de :data:`DEV_ROLES`."""
    return "\n".join(f"- `{role}` : {DEV_ROLE_DESCRIPTIONS[role]}" for role in DEV_ROLES)


class _StrictLoader(yaml.SafeLoader):
    """``SafeLoader``, mais une clé dupliquée est une ERREUR.

    ``yaml.safe_load`` résout un doublon en « dernier gagnant », sans un mot :
    un ``system_prompt:`` déclaré deux fois — ce que produit un conflit de
    merge mal résolu — voit sa première moitié disparaître, et ``extra=
    "forbid"`` ne peut rien y faire puisque le doublon n'atteint jamais
    Pydantic. Le doublon ENTRE fichiers était détecté ; celui à l'intérieur
    d'un fichier ne l'était pas.
    """


def _no_duplicate_keys(
    loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    # `type: ignore[no-untyped-call]` : PyYAML n'expose pas d'annotations pour
    # `construct_object`. Le ignore est NOMMÉ et borné à ces deux appels plutôt
    # que posé sur le module.
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)  # type: ignore[no-untyped-call]
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(  # type: ignore[no-untyped-call]
            value_node, deep=deep
        )
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def _render_tokens(text: str, *, source: Path) -> str:
    rendered = text.replace(DEV_ROLES_TOKEN, render_dev_roles())
    leftover = _REMAINING_TOKEN_RE.search(rendered)
    if leftover is not None:
        raise RuntimeError(
            f"dev catalog init failed: unknown placeholder {leftover.group(0)} in {source}"
        )
    return rendered


def _render_deep(value: Any, *, source: Path) -> Any:
    """Rend les jetons dans TOUTE chaîne du document, à n'importe quelle profondeur.

    La substitution ne couvrait que ``system_prompt`` : un ``${DEV_ROLES}``
    posé dans la description d'un ``input_contract``, ou dans un champ ajouté
    par une story ultérieure, partait tel quel dans la config — le « prompt
    portant un trou » que la garde existe précisément pour empêcher, par le
    seul chemin qu'elle ne regardait pas.

    La garde de jeton résiduel suit le même élargissement : elle accepte
    désormais n'importe quel ``${...}`` (et non les seules MAJUSCULES), donc
    ``${devRoles}`` ou ``${Dev_Roles}`` sont refusés au lieu d'être livrés au
    modèle.
    """
    if isinstance(value, str):
        return _render_tokens(value, source=source)
    if isinstance(value, dict):
        return {key: _render_deep(item, source=source) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_deep(item, source=source) for item in value]
    return value


def load_dev_catalog(catalog_dir: Path | None = None) -> Mapping[str, DevAgentDefinition]:
    """Charge et valide le catalogue du Pôle Dev.

    Args:
        catalog_dir: Surcharge le répertoire par défaut (tests).

    Returns:
        Mapping immuable ``{key: DevAgentDefinition}``, trié par nom de
        fichier pour que l'ordre de provisioning soit reproductible.

    Raises:
        RuntimeError: répertoire absent, YAML illisible ou malformé, clé en
            double, ou jeton de substitution inconnu.
    """
    directory = catalog_dir or DEFAULT_CATALOG_DIR
    if not directory.is_dir():
        raise RuntimeError(f"dev catalog init failed: {directory} is not a directory")

    # `.yml` n'est PAS chargé — mais il n'est plus ignoré en silence. Un agent
    # d'une Story 5.x déposé sous cette extension disparaissait du catalogue
    # sans un mot, et si c'était le seul fichier l'erreur rendue était le
    # trompeur « no agent definition found ».
    strays = sorted(directory.glob("*.yml"))
    if strays:
        names = ", ".join(path.name for path in strays)
        raise RuntimeError(
            f"dev catalog init failed: {names} — le catalogue ne charge que `.yaml`. "
            "Renommer, plutôt que de laisser un agent absent sans diagnostic."
        )

    catalog: dict[str, DevAgentDefinition] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"dev catalog init failed: cannot read {path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            # `UnicodeDecodeError` est un `ValueError`, pas un `OSError` : il
            # échappait au wrapper et rendait un traceback qui ne nommait même
            # pas le fichier. Le catalogue est en prose française — un YAML
            # sauvé en Latin-1 n'a rien d'exotique.
            raise RuntimeError(
                f"dev catalog init failed: {path} is not valid UTF-8 ({exc}). "
                "Le catalogue est lu en UTF-8 strict — ré-encoder le fichier."
            ) from exc

        try:
            raw_data = yaml.load(raw_text, Loader=_StrictLoader)
        except yaml.YAMLError as exc:
            raise RuntimeError(
                f"dev catalog init failed: YAML parse error in {path}: {exc}"
            ) from exc

        if not isinstance(raw_data, dict):
            raise RuntimeError(
                f"dev catalog init failed: expected mapping at top level of {path}, "
                f"got {type(raw_data).__name__}"
            )

        raw_data = _render_deep(raw_data, source=path)

        try:
            definition = DevAgentDefinition.model_validate(raw_data)
        except PydanticValidationError as exc:
            # Narrow to the Pydantic error, exactly like `load_registry` —
            # a NameError/ImportError must keep its own traceback instead of
            # being relabelled "schema validation error".
            raise RuntimeError(
                f"dev catalog init failed: schema validation error in {path}: {exc}"
            ) from exc

        if definition.key in catalog:
            raise RuntimeError(
                f"dev catalog init failed: duplicate key '{definition.key}' in {path}"
            )
        # Le contrôle de doublon portait sur `key`, mais l'unicité en base
        # porte sur `name` : deux agents homonymes passaient, et le second
        # `_provision_agent` retrouvait la row du premier via
        # `get_latest_by_name` — écrasement silencieux de sa config, ou
        # `SeedError` d'archétype selon le hasard des archétypes déclarés.
        clash = next((other for other in catalog.values() if other.name == definition.name), None)
        if clash is not None:
            raise RuntimeError(
                f"dev catalog init failed: agents '{clash.key}' et '{definition.key}' "
                f"déclarent le même name '{definition.name}' ({path}). "
                "`agent_templates.name` est unique — le second écraserait le premier."
            )
        catalog[definition.key] = definition

    if not catalog:
        raise RuntimeError(f"dev catalog init failed: no agent definition found in {directory}")

    # Mirror `load_registry`'s P-12 posture — the catalog is read-only once
    # loaded, and `mappingproxy` makes that a TypeError rather than a
    # convention.
    return MappingProxyType(catalog)


def catalog_namespaces(
    catalog: Mapping[str, DevAgentDefinition],
) -> list[DevNamespaceRequirement]:
    """Tous les namespaces requis par le catalogue, dédupliqués par nom.

    Raises:
        RuntimeError: deux agents déclarent le même nom de namespace avec un
            ``type`` ou un ``department`` différent — le provisioning n'aurait
            aucune raison d'en préférer un, et créer le premier rencontré
            ferait dépendre le résultat de l'ordre alphabétique des fichiers.
    """
    by_name: dict[str, DevNamespaceRequirement] = {}
    for key in sorted(catalog):
        for requirement in catalog[key].namespaces:
            existing = by_name.get(requirement.name)
            if existing is None:
                by_name[requirement.name] = requirement
            elif existing != requirement:
                raise RuntimeError(
                    f"dev catalog init failed: conflicting declarations for namespace "
                    f"'{requirement.name}' ({existing!r} vs {requirement!r})"
                )
    return [by_name[name] for name in sorted(by_name)]


__all__ = [
    "DEFAULT_CATALOG_DIR",
    "DEV_ROLES_TOKEN",
    "DevAgentDefinition",
    "DevNamespaceRequirement",
    "catalog_namespaces",
    "load_dev_catalog",
    "render_dev_roles",
]
