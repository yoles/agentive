"""Review conversationnelle : schémas partagés (Story 2.8, FR14).

``ReviewComment``/``ControllerReview`` sont des schémas de données pures
(pas des events publiés sur ``event_bus``, cf ``shared/contracts/events/``)
réutilisables par toute feature amenée à consommer une review structurée
d'agent Contrôleur (Playground aujourd'hui, Story 5.4 "Code Reviewer"
demain) sans import croisé entre ``features.*`` (``.import-linter``
Contract 1).

N'hérite volontairement pas de ``ContractBase`` (stub Sprint 0 jamais
utilisé project-wide, cf Dev Notes Story 2.8 #2) : suit le pattern Pydantic
simple déjà en usage partout ailleurs (``playground/schemas.py``,
``shared/contracts/events/*.py``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewComment(BaseModel):
    """Un commentaire localisé émis par un agent Contrôleur."""

    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1)
    severity: Literal["blocking", "suggestion", "question"]
    message: str = Field(min_length=1)
    suggested_fix: str | None = None


class ControllerReview(BaseModel):
    """Verdict structuré rendu par un agent Contrôleur (FR14)."""

    model_config = ConfigDict(extra="forbid")

    comments: list[ReviewComment]
    verdict: Literal["pass", "fail", "needs_fix"]


__all__ = ["ControllerReview", "ReviewComment"]
