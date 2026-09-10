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
read-time decision ("is this chunk expired / archivable *now*");
:class:`DecayPolicy` owns the read-time *relevance* decision ("how much is a
chunk of this age still worth", Story 3.4 AC1).
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


# ─── EmbeddingBackend ─────────────────────────────────────────────


class EmbeddingBackend(StrEnum):
    """The 3 embedding backends a namespace can pick (Story 3.6 AC2/AC3).

    Unlike :class:`NamespaceType`, this has a single global default
    (:attr:`CLOUD`) rather than a per-type table, already carried by the DB
    column's ``server_default="cloud"`` and by ``NamespaceRepo.create``'s own
    default — so there is no ``_DEFAULT_..._BY_TYPE``-style table to keep in
    sync here.

    No DB ``CHECK`` constraint mirrors this enum (unlike ``NamespaceType``'s
    ``ck_namespace_type``), on purpose: AR19 wants future backends addable
    without a migration. The HTTP boundary (Pydantic, ``schemas.py``) is the
    only validation gate; a corrupt/unknown value already in the DB (hand-
    seeded namespace) raises a 503-ready error at read time
    (:meth:`~agentive_backend.shared.llm.embedding_router.EmbeddingRouter.resolve`)
    rather than degrading to ``cloud`` — unlike a malformed
    ``RetentionPolicy``/``DecayPolicy``, silently persisting a cloud vector
    under another model's namespace would break write/read symmetry
    (Décision John 2026-09-09, Story 3.6 code review).
    """

    LOCAL = "local"
    CLOUD = "cloud"
    VOYAGE = "voyage"


# ─── RetentionPolicy ──────────────────────────────────────────────

# Floor is 1, not 0: a zero-second lifetime is never a real retention
# intent, but it is a silently destructive one. `archive_after_seconds=0`
# makes `created_at <= now - 0` match every chunk in the namespace on the
# very next archival pass, and `default_ttl_seconds=0` expires a chunk at
# the instant it is written (code review Story 3.3, IG1).
SECONDS_MIN: Final[int] = 1

# Ceiling, 10 years. Not decoration: `namespaces.decay_policy` is free-shape
# JSONB, so a hand-seeded or legacy row can carry a numeric that decodes to
# `inf`, or an int past 1e308. Both slip through a one-sided `< SECONDS_MIN`
# test, and both bite on the READ path: `age / inf` is `0.0`, so a configured
# decay is silently neutralised while `decay_policy_valid` still reports
# `True`, and `age / huge_int` raises `OverflowError` from inside
# `decay_factor` — past the parsing guard, i.e. a 500 on a search. The HTTP
# layer already bounds these fields; the domain is the only gate left for rows
# ALREADY in the table (code review Story 3.4, P5).
SECONDS_MAX: Final[int] = 315_360_000


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


# ─── DecayPolicy ──────────────────────────────────────────────────


class DecayFunction(StrEnum):
    """The 4 temporal-decay shapes a namespace can pick (Story 3.4 AC1).

    Mirrors :class:`NamespaceType`'s style. Unlike it, there is no DB CHECK
    constraint to mirror — ``namespaces.decay_policy`` is free-shape JSONB,
    so :meth:`DecayPolicy.from_mapping` is the only gate.
    """

    NONE = "none"
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    STEP = "step"


# Per-function parameter table — the single source of truth for the
# cross-validation in `__post_init__`, the serialization in `to_mapping`,
# AND `DecayPolicyOverride`'s 422 rules at the HTTP boundary. Keeping them
# derived from one table is what makes "reject a foreign parameter" and
# "emit only my own parameters" two views of the same rule rather than
# several lists to keep in sync. Public (no leading underscore) precisely
# so `schemas.py` can import it instead of re-declaring an unguarded copy,
# whose `KeyError` on a new enum member would surface as a 500 where the
# schema promises a 422 (code review Story 3.4, P8).
DECAY_PARAMS_BY_FUNCTION: Final[dict[DecayFunction, frozenset[str]]] = {
    DecayFunction.NONE: frozenset(),
    DecayFunction.EXPONENTIAL: frozenset({"half_life_seconds"}),
    DecayFunction.LINEAR: frozenset({"horizon_seconds"}),
    DecayFunction.STEP: frozenset({"threshold_seconds", "factor"}),
}

assert set(DECAY_PARAMS_BY_FUNCTION) == set(DecayFunction), (
    "DECAY_PARAMS_BY_FUNCTION must have exactly one entry per DecayFunction "
    "member (same exhaustiveness guard as _DEFAULT_TTL_SECONDS_BY_TYPE)"
)

DECAY_PARAM_NAMES: Final[tuple[str, ...]] = (
    "half_life_seconds",
    "horizon_seconds",
    "threshold_seconds",
    "factor",
)


@dataclass(frozen=True, slots=True)
class DecayPolicy:
    """Typed view over the ``namespaces.decay_policy`` JSONB (Story 3.4 AC1).

    Twin of :class:`RetentionPolicy`: same frozen dataclass shape, same
    ``from_mapping``/``to_mapping`` as the only (de)serialization points,
    same "never read the clock in here — ``now`` is a parameter" rule.

    The default is :attr:`DecayFunction.NONE`, i.e. a constant factor of
    ``1.0``, i.e. ``final_score == similarity``. **No default is populated
    per namespace type** (unlike ``RetentionPolicy.default_for_type``):
    switching decay on by default would silently reorder every existing
    namespace's search results. An operator opts in namespace by namespace
    (see this story's Dev Notes § "aucun défaut de décroissance").

    Parameters are function-scoped and mutually exclusive:

    * ``exponential`` — ``half_life_seconds``, the age at which the factor
      is exactly ``0.5``. Expressed as a half-life rather than the epic's
      raw ``lambda`` (equivalent, ``lambda = ln(2) / half_life``) because
      an operator reasons in "after 30 days a chunk is worth half".
    * ``linear`` — ``horizon_seconds``, the age at which the factor reaches
      exactly ``0.0`` (and stays there).
    * ``step`` — ``threshold_seconds`` plus ``factor``: ``1.0`` up to and
      including the threshold, ``factor`` past it.
    * ``none`` — no parameter at all.
    """

    function: DecayFunction = DecayFunction.NONE
    half_life_seconds: int | None = None
    horizon_seconds: int | None = None
    threshold_seconds: int | None = None
    factor: float | None = None

    def __post_init__(self) -> None:
        allowed = DECAY_PARAMS_BY_FUNCTION[self.function]
        for name in DECAY_PARAM_NAMES:
            value = getattr(self, name)
            if name in allowed:
                if value is None:
                    raise DomainValidationError(
                        f"{name} is required for decay function {self.function.value!r}"
                    )
            elif value is not None:
                # Rejected, never ignored: a silently dropped parameter is
                # exactly the trap settled by code review Story 3.2 BS1 (an
                # empty `retention_policy` override was accepted and produced
                # a namespace with no retention at all). Here it would produce
                # a namespace whose configured decay never applies.
                raise DomainValidationError(
                    f"{name} is not a parameter of decay function "
                    f"{self.function.value!r}; remove it or change the function"
                )

        for name in ("half_life_seconds", "horizon_seconds", "threshold_seconds"):
            value = getattr(self, name)
            # Two-sided on purpose, and written as a chained comparison rather
            # than a cast: `inf`, `nan` and an int above 1e308 all fail it
            # without ever being converted to a float, so none of them can
            # reach `decay_factor` (code review Story 3.4, P5).
            if value is not None and not SECONDS_MIN <= value <= SECONDS_MAX:
                raise DomainValidationError(
                    f"{name} must be in [{SECONDS_MIN}, {SECONDS_MAX}], got {value}"
                )

        if self.factor is not None and not 0.0 <= self.factor <= 1.0:
            raise DomainValidationError(f"factor must be in [0.0, 1.0], got {self.factor}")

    @classmethod
    def from_mapping(cls, raw: Any | None) -> DecayPolicy:
        """The ONE place that reads the ``decay_policy`` dict into a VO.

        An empty/absent mapping is a valid "no decay" policy (that is what
        the column's ``DEFAULT '{}'`` means, and what every namespace
        created before Story 3.4 carries). An unknown ``function`` is a
        hard :class:`DomainValidationError` — callers on the read path
        catch it and fall back to :class:`DecayPolicy` (Story 3.4 AC3).
        """
        raw = raw or {}
        raw_function = raw.get("function", DecayFunction.NONE.value)
        try:
            function = DecayFunction(raw_function)
        except ValueError as exc:
            raise DomainValidationError(f"unknown decay function {raw_function!r}") from exc
        return cls(
            function=function,
            half_life_seconds=raw.get("half_life_seconds"),
            horizon_seconds=raw.get("horizon_seconds"),
            threshold_seconds=raw.get("threshold_seconds"),
            factor=raw.get("factor"),
        )

    def to_mapping(self) -> dict[str, Any]:
        """The ONE place that (re)serializes back to a JSONB-ready dict.

        :attr:`DecayFunction.NONE` emits ``{}`` (not ``{"function":
        "none"}``) so it round-trips with :meth:`from_mapping` AND matches
        the column default byte for byte — a namespace that never opted in
        and one that explicitly opted out are the same row.
        """
        if self.function is DecayFunction.NONE:
            return {}
        out: dict[str, Any] = {"function": self.function.value}
        for name in sorted(DECAY_PARAMS_BY_FUNCTION[self.function]):
            out[name] = getattr(self, name)
        return out

    def decay_factor(self, created_at: datetime, now: datetime) -> float:
        """The multiplier applied to a chunk's similarity — always ``[0, 1]``.

        ``now`` is a parameter, never ``datetime.now()``: the domain does
        not read the clock (same convention as
        :meth:`~.entities.MemoryChunk.is_expired`).

        The age is clamped at 0 so a ``created_at`` in the future (clock
        drift between the app server and Postgres' ``func.now()``) yields
        ``1.0`` rather than a factor above 1, which would let a
        future-dated chunk outrank a perfect match (same decision as
        ``scripts/_bench_common.py``, Story 1.3).
        """
        age = max(0.0, (now - created_at).total_seconds())

        match self.function:
            case DecayFunction.NONE:
                return 1.0
            case DecayFunction.EXPONENTIAL:
                assert self.half_life_seconds is not None  # guaranteed by __post_init__
                # `float()` is not cosmetic: typeshed types `float.__pow__` as
                # `Any` (a negative base with a fractional exponent yields a
                # complex), so returning it raw trips mypy's
                # `warn_return_any` on a function declared `-> float`.
                return float(0.5 ** (age / self.half_life_seconds))
            case DecayFunction.LINEAR:
                assert self.horizon_seconds is not None
                return max(0.0, 1.0 - age / self.horizon_seconds)
            case DecayFunction.STEP:
                assert self.threshold_seconds is not None
                assert self.factor is not None
                return 1.0 if age <= self.threshold_seconds else self.factor
            case _:  # pragma: no cover - unreachable while the match is total
                # mypy proves this branch dead today. It exists for the day a
                # 5th `DecayFunction` member is added and this `match` is not
                # updated: falling off the end would return `None` from a
                # `-> float`, and that `None` would reach `similarity * factor`
                # as a `TypeError` on the read path, whose whole posture is
                # "a read keeps serving" (code review Story 3.4, P13).
                raise DomainValidationError(
                    f"decay function {self.function!r} has no decay_factor rule"
                )


__all__ = [
    "DecayFunction",
    "DecayPolicy",
    "DomainValidationError",
    "EmbeddingBackend",
    "NamespaceType",
    "RetentionPolicy",
]
