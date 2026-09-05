"""Framework-free value objects for the Memory Manager domain core.

Pure Python (frozen dataclasses + :class:`enum.StrEnum`) — no Pydantic, no
SQLAlchemy. Introduced *before* the feature is implemented, on purpose: the
audit (Étape 4 §4.5) recommends scoping the memory **retention / expiry rule
as a domain concept up front**, so the eventual ``memory_manager`` service
never reproduces the "anemic model" (rule living in SQL or an imperative
service) that the Agent Registry had to unwind.

Division of responsibility (audit §4.5):
    the *service* triggers (orchestration, clock), the *domain* decides (rule).
:class:`RetentionPolicy` owns the write-time decision ("when should a chunk
expire / become archivable"); :class:`~.entities.MemoryChunk` owns the
read-time decision ("is this chunk expired / archivable *now*").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final


class DomainValidationError(ValueError):
    """Raised when a value object is constructed with invalid data.

    Framework-free stand-in — the domain layer stays unaware of the HTTP /
    RFC 7807 machinery in ``shared.exceptions``. Service-layer callers
    translate this into the appropriate boundary error.
    """


# ─── NamespaceType ────────────────────────────────────────────────


class NamespaceType(StrEnum):
    """The 4 namespace kinds.

    Mirrors the DB ``ck_namespace_type`` CHECK constraint
    (``infra/db/models.Namespace``). A purely-domain reflection of the same
    value set — the domain layer cannot depend on the ORM.
    """

    CLIENT = "client"
    METIER = "metier"
    OPERATIONNELLE = "operationnelle"
    CONTEXTUELLE = "contextuelle"


# ─── RetentionPolicy ──────────────────────────────────────────────

SECONDS_MIN: Final[int] = 0


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Typed view over the ``namespaces.retention_policy`` JSONB.

    Both fields are optional; ``None`` means "no automatic lifecycle":

    * ``default_ttl_seconds`` — default lifetime applied to a chunk that does
      not carry its own ``ttl_seconds`` override. ``None`` → chunks never
      expire by default.
    * ``archive_after_seconds`` — age at which a (still-live) chunk becomes
      eligible for archival. ``None`` → chunks are never auto-archived.
    """

    default_ttl_seconds: int | None = None
    archive_after_seconds: int | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("default_ttl_seconds", self.default_ttl_seconds),
            ("archive_after_seconds", self.archive_after_seconds),
        ):
            if value is not None and value < SECONDS_MIN:
                raise DomainValidationError(f"{name} must be >= {SECONDS_MIN}, got {value}")

    @classmethod
    def from_mapping(cls, raw: Any | None) -> RetentionPolicy:
        """The ONE place that reads the ``retention_policy`` dict into a VO."""
        raw = raw or {}
        return cls(
            default_ttl_seconds=raw.get("default_ttl_seconds"),
            archive_after_seconds=raw.get("archive_after_seconds"),
        )

    def to_mapping(self) -> dict[str, Any]:
        """The ONE place that (re)serializes back to a JSONB-ready dict.

        Only keys whose field is non-``None`` are emitted (progressive-build
        semantics — an empty policy round-trips to ``{}``).
        """
        out: dict[str, Any] = {}
        if self.default_ttl_seconds is not None:
            out["default_ttl_seconds"] = self.default_ttl_seconds
        if self.archive_after_seconds is not None:
            out["archive_after_seconds"] = self.archive_after_seconds
        return out

    def expires_at(
        self, created_at: datetime, *, ttl_override_seconds: int | None = None
    ) -> datetime | None:
        """Return the absolute expiry instant for a chunk created at
        ``created_at``, or ``None`` if the chunk never expires.

        A per-chunk ``ttl_override_seconds`` wins over the policy default
        (mirrors ``MemoryChunk.ttl_seconds`` being nullable at the row level).
        """
        ttl = ttl_override_seconds if ttl_override_seconds is not None else self.default_ttl_seconds
        if ttl is None:
            return None
        if ttl < SECONDS_MIN:
            raise DomainValidationError(f"ttl_override_seconds must be >= {SECONDS_MIN}, got {ttl}")
        return created_at + timedelta(seconds=ttl)

    def archivable_at(self, created_at: datetime) -> datetime | None:
        """Return the instant a chunk created at ``created_at`` becomes
        eligible for archival, or ``None`` if the policy never archives."""
        if self.archive_after_seconds is None:
            return None
        return created_at + timedelta(seconds=self.archive_after_seconds)

    @classmethod
    def default_for_type(cls, ns_type: NamespaceType) -> RetentionPolicy:
        """The per-type default TTL mandated by Story 3.2 AC1 (epics.md).

        ``archive_after_seconds`` is deliberately left ``None`` here — Story
        3.3 (automatic archival job) owns picking its own default, AC1 only
        specifies a TTL per type.
        """
        try:
            return cls(default_ttl_seconds=_DEFAULT_TTL_SECONDS_BY_TYPE[ns_type])
        except KeyError as exc:
            # `ns_type` is an enum member, so this can only fire if a new
            # `NamespaceType` value is added without updating the table
            # below — the module-level assertion catches that at import
            # time, this is the defense in depth for anything that slips
            # past it (code review Story 3.2, P7).
            raise DomainValidationError(
                f"no default retention configured for namespace type {ns_type!r}"
            ) from exc


# Story 3.2 AC1 (epics.md line 1011) — one default per namespace type.
_DEFAULT_TTL_SECONDS_BY_TYPE: Final[dict[NamespaceType, int | None]] = {
    NamespaceType.CONTEXTUELLE: 604_800,  # 7 jours
    NamespaceType.OPERATIONNELLE: 7_776_000,  # 90 jours
    NamespaceType.METIER: 31_536_000,  # 365 jours
    NamespaceType.CLIENT: None,  # illimité sauf override explicite à la création
}

assert set(_DEFAULT_TTL_SECONDS_BY_TYPE) == set(NamespaceType), (
    "_DEFAULT_TTL_SECONDS_BY_TYPE must have exactly one entry per NamespaceType "
    "member (code review Story 3.2, P7)"
)


__all__ = [
    "DomainValidationError",
    "NamespaceType",
    "RetentionPolicy",
]
