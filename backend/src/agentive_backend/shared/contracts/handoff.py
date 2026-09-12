"""Résumé de passage inter-node : schéma partagé (Story 4.7, FR53).

``HandoffSummary`` est un schéma de données pur (pas un event publié sur
``event_bus``, cf ``shared/contracts/events/``), produit par
``features.workflow_engine.engine.handoff.summarize_handoff`` et consommé par
le prompt du node suivant dans la même feature — il vit dans
``shared/contracts`` plutôt que dans ``features/workflow_engine`` uniquement
par convention de dépôt des schémas structurés inter-node (mirror
``ReviewComment``/``ControllerReview``), pas parce qu'une autre feature le
consomme aujourd'hui.

N'hérite volontairement pas de ``ContractBase`` (stub Sprint 0 jamais utilisé
project-wide, cf Dev Notes Story 2.8 #2 et ``shared/contracts/review.py``) :
suit le pattern Pydantic simple déjà en usage partout ailleurs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HandoffSummary(BaseModel):
    """Condensé structuré de l'output d'un node, pour le node suivant (FR53).

    Les 4 champs sont la forme la plus simple qui satisfait le nom littéral
    des 4 clés de l'épique (``decisions``, ``artifacts_refs``, ``blockers``,
    ``next_questions``) — pas de sous-objets typés, pas de zone flexible :
    rien dans les AC de Story 4.7 ne demande une extension, cf Dev Notes
    § Forme du contrat de cette story.
    """

    model_config = ConfigDict(extra="forbid")

    decisions: list[str] = Field(default_factory=list)
    artifacts_refs: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_questions: list[str] = Field(default_factory=list)


__all__ = ["HandoffSummary"]
