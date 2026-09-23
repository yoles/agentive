"""Les 8 rôles du Pôle Dev — jeu fermé partagé (Story 5.1, FR43/FR44).

Pourquoi dans ``shared/contracts/`` et pas dans ``features/agent_registry`` :
les Stories 5.2 → 5.6 livrent les agents qui portent ces rôles, et
``.import-linter`` Contract 1 (``independence``) leur interdit d'importer
``agent_registry`` comme il interdit à ``agent_registry`` de les importer.
``shared.contracts`` est la seule porte sanctionnée (CONVENTIONS.md règle
d'or #3).

Le Dev Lead (archétype Orchestrateur) décompose une demande et adresse
chaque sous-tâche à l'un de ces rôles. Le jeu est **fermé** : c'est ce qui
rend l'AC3 de la Story 5.1 (« les agents assignés logiques ») vérifiable
autrement qu'à l'œil.

``DEV_LEAD_ROLE`` n'en fait pas partie : le Dev Lead ne se délègue pas à
lui-même, et une délégation qui le nommerait serait une boucle, pas un plan.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final, Literal

#: Le rôle de l'orchestrateur lui-même — hors du jeu délégable.
DEV_LEAD_ROLE: Final = "dev_lead"

DevRole = Literal[
    "code_researcher",
    "architect_analyst",
    "code_producer",
    "code_reviewer",
    "test_engineer",
    "cicd_watcher",
    "doc_writer",
    "sprint_reporter",
]

#: Ordre déclaré = ordre de lecture dans le prompt du Dev Lead (le loader du
#: catalogue le rend, cf ``features/agent_registry/dev_catalog.py``), donc un
#: tuple et pas un ``frozenset`` : un ordre instable ferait varier le prompt
#: d'un démarrage à l'autre, et donc la sortie du modèle.
DEV_ROLES: Final[tuple[DevRole, ...]] = (
    "code_researcher",
    "architect_analyst",
    "code_producer",
    "code_reviewer",
    "test_engineer",
    "cicd_watcher",
    "doc_writer",
    "sprint_reporter",
)

#: Ce que chaque rôle sait faire, en une ligne — rendu dans le prompt du Dev
#: Lead pour qu'il route sur une description et pas sur un slug. Les libellés
#: reprennent les Stories 5.2 → 5.6 mot pour mot.
DEV_ROLE_DESCRIPTIONS: Final[dict[DevRole, str]] = {
    "code_researcher": (
        "explore le codebase, trouve les fichiers pertinents, analyse les dépendances"
    ),
    "architect_analyst": "analyse l'impact et propose une approche technique avec ses risques",
    "code_producer": "implémente en suivant les conventions du projet",
    "code_reviewer": "review la qualité avec un feedback conversationnel localisé et un verdict",
    "test_engineer": "génère les tests unitaires et d'intégration adaptés au framework du projet",
    "cicd_watcher": "surveille les pipelines CI/CD et alerte sur vulnérabilités ou échecs",
    "doc_writer": "génère la documentation technique (API docs ou ADR)",
    "sprint_reporter": "produit un résumé narratif périodique des tâches complétées",
}


def unknown_roles(candidates: Iterable[object]) -> list[str]:
    """Les valeurs de ``candidates`` qui ne sont pas un rôle Dev délégable.

    Rendue en liste **ordonnée et dédupliquée** pour qu'un message d'erreur
    soit reproductible. Une valeur non-``str`` est rendue via ``repr`` : un
    ``null`` ou un objet dans ``delegations[].target_role`` est une anomalie
    au même titre qu'un slug inconnu, et la faire disparaître du rapport
    serait exactement la dégradation silencieuse que ce dépôt refuse
    ailleurs (cf ``_to_lc_messages``, revue de la Story 5.0).
    """
    known = set(DEV_ROLES)
    seen: dict[str, None] = {}
    for candidate in candidates:
        if isinstance(candidate, str):
            if candidate in known:
                continue
            label = candidate
        else:
            # Le type est NOMMÉ, et c'est ce qui rend le label infalsifiable :
            # `repr` seul donnait `None` pour un `target_role: null`, donc le
            # même label que la chaîne littérale `"None"` — et la
            # déduplication en faisait disparaître un. Un slug ne peut pas
            # contenir « (NoneType) ».
            label = f"{candidate!r} ({type(candidate).__name__})"
        seen.setdefault(label, None)
    return sorted(seen)


def validate_delegation_plan(output: object) -> list[str]:
    """Les incohérences d'une sortie de Dev Lead, en clair. Vide = conforme.

    Les règles vérifiées sont les « RÈGLES DURES » énoncées au Dev Lead dans
    son propre prompt (``features/agent_registry/templates/dev/dev_lead.yaml``),
    **et elles le sont entièrement** :

    - ``status`` hors de ``done`` / ``failed`` ;
    - un ``target_role`` hors du jeu fermé ;
    - une délégation qui pointe une sous-tâche inexistante ;
    - une sous-tâche que personne ne prend, ou que PLUSIEURS prennent ;
    - ``failed`` ⇒ ``plan`` et ``delegations`` vides **et** une
      ``blocking_question`` non vide ;
    - ``done`` ⇒ au moins une sous-tâche.

    Cette docstring affirmait couvrir « exactement » les règles dures alors
    que la moitié ``blocking_question`` de la quatrième n'était pas vérifiée,
    qu'un ``done`` au plan vide passait, et qu'une double délégation était
    absorbée par un ``set``. Les trois sont couvertes ici.

    **Signale, ne corrige pas.** Réécrire un plan incohérent serait décider à
    la place de l'orchestrateur ; le laisser passer en silence serait la
    dégradation muette que ce dépôt refuse. Entre les deux, on nomme.

    **Consommateurs.** Les tests d'acceptation de la Story 5.1 (AC3), puis
    les Stories 5.2 → 5.6 : ce sont elles qui liront un plan pour en exécuter
    les délégations, et elles ne peuvent pas importer ``agent_registry``
    (Contract 1) — d'où ce module. Aucun appelant de production aujourd'hui,
    et c'est assumé : le run de Sprint 2 s'arrête sur le node Dev Lead, son
    plan est lu par un humain.

    Posture défensive assumée : ``output`` vient d'un ``_best_effort_json``
    sur du texte de modèle. Toute forme est possible, aucune ne lève.
    """
    problems: list[str] = []
    if not isinstance(output, dict):
        return [f"la sortie n'est pas un objet JSON (reçu {type(output).__name__})"]

    status = output.get("status")
    if status not in ("done", "failed"):
        problems.append(f"status invalide : {status!r} (attendu 'done' ou 'failed')")

    plan = output.get("plan")
    delegations = output.get("delegations")
    if not isinstance(plan, list):
        problems.append(f"plan absent ou mal formé (reçu {type(plan).__name__})")
        plan = []
    if not isinstance(delegations, list):
        problems.append(f"delegations absent ou mal formé (reçu {type(delegations).__name__})")
        delegations = []

    subtask_ids = {
        item["id"]
        for item in plan
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    }
    if len(subtask_ids) != len(plan):
        problems.append("chaque entrée de plan doit porter un `id` non vide et unique")

    targeted: set[str] = set()
    rogue = unknown_roles(item.get("target_role") for item in delegations if isinstance(item, dict))
    if rogue:
        problems.append(f"rôles hors du jeu fermé : {', '.join(rogue)}")
    for item in delegations:
        if not isinstance(item, dict):
            problems.append(f"délégation mal formée (reçu {type(item).__name__})")
            continue
        subtask_id = item.get("subtask_id")
        if not isinstance(subtask_id, str) or subtask_id not in subtask_ids:
            problems.append(f"délégation vers une sous-tâche inconnue : {subtask_id!r}")
            continue
        if subtask_id in targeted:
            # « Adresse chaque sous-tâche à EXACTEMENT un rôle » (prompt).
            # `targeted` étant un `set`, un doublon était absorbé sans un mot
            # — et les Stories 5.2 → 5.6, qui exécuteront ce plan, auraient
            # mis deux exécutants sur une même tâche.
            problems.append(
                f"sous-tâche déléguée plusieurs fois : {subtask_id!r} — "
                "chaque sous-tâche va à exactement un rôle"
            )
            continue
        targeted.add(subtask_id)

    orphans = sorted(subtask_ids - targeted)
    if orphans:
        problems.append(f"sous-tâches déléguées à personne : {', '.join(orphans)}")

    problems.extend(_status_problems(output, status, plan, delegations))
    return problems


def _status_problems(
    output: dict[str, Any], status: object, plan: list[Any], delegations: list[Any]
) -> list[str]:
    """Ce que le ``status`` déclaré engage sur le reste de la sortie.

    Extrait de :func:`validate_delegation_plan` pour tenir sous la borne de
    complexité — même règles, même ordre, aucun changement de comportement.
    """
    if status == "failed":
        problems = []
        if plan or delegations:
            problems.append("un status 'failed' doit rendre un plan et des délégations vides")
        # La règle dure du prompt a DEUX moitiés : « `plan` et `delegations`
        # sont des tableaux vides ET `blocking_question` est présente ». Seule
        # la première était vérifiée, donc un refus sans raison énoncée
        # passait pour conforme — et un test l'épinglait comme tel.
        question = output.get("blocking_question")
        if not isinstance(question, str) or not question.strip():
            problems.append(
                "un status 'failed' doit porter une `blocking_question` non vide — "
                "un refus sans question est un refus sans recours"
            )
        return problems
    if status == "done" and not plan:
        # Le prompt impose « Découpe en 2 à 6 sous-tâches ». Un `done` avec un
        # plan vide n'était contredit par rien : aucune borne de cardinalité
        # n'était vérifiée, à aucun bout.
        return [
            "un status 'done' doit porter au moins une sous-tâche — "
            "un plan vide n'est pas une décomposition"
        ]
    return []


__all__ = [
    "DEV_LEAD_ROLE",
    "DEV_ROLES",
    "DEV_ROLE_DESCRIPTIONS",
    "DevRole",
    "unknown_roles",
    "validate_delegation_plan",
]
