"""Push Memory : schéma partagé (Story 3.5, FR20).

``MemorizedChunkView`` est un sous-ensemble volontairement étroit de
``features.memory_manager.schemas.MemorySearchResultView`` — non réutilisable
telle quelle par ``features.playground`` (``.import-linter`` Contract 1
interdit tout import direct de ``features.memory_manager`` depuis
``features.playground``). Seuls les champs dont Push Memory a besoin pour
injecter un chunk dans un prompt : pas de ``similarity``/``decay_factor``/
``archived_at``/``expires_at``.

Mirror exact du précédent posé par ``shared/contracts/review.py`` (Story 2.8,
D84) pour la même raison structurelle : deux features isolées par contrat
d'architecture qui doivent malgré tout échanger une forme de données.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemorizedChunkView(BaseModel):
    """Un chunk mémoire jugé pertinent, prêt à être injecté dans un prompt."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    namespace: str
    content: str
    score: float = Field(ge=0.0, le=1.0)


__all__ = ["MemorizedChunkView"]
