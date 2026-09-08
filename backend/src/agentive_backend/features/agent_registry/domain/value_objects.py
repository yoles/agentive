"""Framework-free value objects for the Agent Registry domain core.

Pure Python (frozen dataclasses + :class:`enum.StrEnum`) — no Pydantic, no
SQLAlchemy. This is what makes these types unit-testable in microseconds
without a DB or an app context (audit "domaine anémique", Sprint 2 2026-07).

``.import-linter`` compliance: this module imports nothing from ``infra.*`` or
``shared.repositories.*`` and never touches ``sqlalchemy``, so Contract 2
(layered) and Contract 3 (no-direct-db-access) hold transitively.

Single parse / single serialize point
--------------------------------------
:meth:`AgentConfig.from_mapping` and :meth:`AgentConfig.to_mapping` are the
ONLY places that read/write the ``agent_templates.config`` JSONB shape. Every
scattered ``.get(key, default)`` that used to live in ``service.py`` is
replaced by these two methods.

Known trade-off (see the ADR ``docs/decisions/agent-registry-domain-core.md``):
``to_mapping()`` only emits the keys this module knows about. A full
from/to round-trip is therefore lossy for any *unknown* key. Today the only
writers of ``config`` (``ArchetypeDefinition.to_template_config()`` and the
template update path) never produce a key outside this set, so the round-trip
is lossless in practice — but the immutable instance ``snapshot`` (which must
freeze the stored bytes verbatim) deliberately does NOT round-trip through this
VO (see ``services.instance_service``).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final


class DomainValidationError(ValueError):
    """Raised when a value object is constructed with invalid data.

    Framework-free stand-in — the domain layer stays unaware of the HTTP /
    RFC 7807 machinery in ``shared.exceptions``. Service-layer callers translate
    this into the appropriate boundary error (Pydantic ``ValueError`` at the
    HTTP edge, or a ``ValidationError`` when relevant).
    """


# ─── Version ──────────────────────────────────────────────────────

MIN_VERSION: Final[int] = 1


@dataclass(frozen=True, slots=True)
class Version:
    """A 1-based, monotonically-incrementing agent-template version."""

    value: int

    def __post_init__(self) -> None:
        if self.value < MIN_VERSION:
            raise DomainValidationError(f"version must be >= {MIN_VERSION}, got {self.value}")

    def next(self) -> Version:
        """Return a new :class:`Version` incremented by one."""
        return Version(self.value + 1)

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return str(self.value)


# ─── Archetype ────────────────────────────────────────────────────


class Archetype(StrEnum):
    """The 8 universal archetypes.

    Mirrors ``archetypes.EXPECTED_ARCHETYPE_IDS`` and the DB
    ``ck_agent_template_archetype`` CHECK constraint. This is a third,
    purely-domain reflection of the same value set (the domain layer cannot
    depend on the YAML-loading Pydantic machinery in ``archetypes.py``). The
    duplication of the value set across the three sources is a pre-existing
    condition, noted but out of scope for this slice (see the ADR).
    """

    ORCHESTRATEUR = "orchestrateur"
    CHERCHEUR = "chercheur"
    ANALYSTE = "analyste"
    PRODUCTEUR = "producteur"
    STRATEGE = "stratege"
    CONTROLEUR = "controleur"
    VEILLEUR = "veilleur"
    COMMUNICATEUR = "communicateur"


# ─── Contract ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Contract:
    """Elastic ``{core, extras}`` contract shape.

    Unifies the three structurally-identical Pydantic twins that existed
    before this slice (``schemas.ContractDefinition``,
    ``schemas.ContractSkeletonView``, ``archetypes.ContractSkeleton``). The
    schema classes remain distinct (OpenAPI component names) but now convert
    through this single VO via their ``to_domain()`` helper.
    """

    core: Mapping[str, Any] = field(default_factory=dict)
    extras: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> Contract:
        raw = raw or {}
        return cls(core=dict(raw.get("core") or {}), extras=dict(raw.get("extras") or {}))

    def to_mapping(self) -> dict[str, Any]:
        return {"core": dict(self.core), "extras": dict(self.extras)}


# ─── LLMParams ────────────────────────────────────────────────────

DEFAULT_TEMPERATURE: Final[float] = 0.7
DEFAULT_MAX_TOKENS: Final[int] = 4096
TEMPERATURE_MIN: Final[float] = 0.0
TEMPERATURE_MAX: Final[float] = 2.0
MAX_TOKENS_MIN: Final[int] = 1
MAX_TOKENS_MAX: Final[int] = 200_000


@dataclass(frozen=True, slots=True)
class LLMParams:
    """Bounded LLM hyperparameters.

    Same bounds as the Pydantic twin (``schemas.LLMParams``) by design, kept as
    an INDEPENDENT defense layer: a value built from
    ``AgentConfig.from_mapping(db_row.config)`` never crossed the Pydantic HTTP
    boundary, so the domain re-validates. This deliberate duplication is
    documented in the ADR — it is not a DRY violation but two distinct trust
    boundaries (HTTP payload vs DB-sourced config).
    """

    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        if not (TEMPERATURE_MIN <= self.temperature <= TEMPERATURE_MAX):
            raise DomainValidationError(
                f"temperature must be in [{TEMPERATURE_MIN}, {TEMPERATURE_MAX}], "
                f"got {self.temperature}"
            )
        if not (MAX_TOKENS_MIN <= self.max_tokens <= MAX_TOKENS_MAX):
            raise DomainValidationError(
                f"max_tokens must be in [{MAX_TOKENS_MIN}, {MAX_TOKENS_MAX}], got {self.max_tokens}"
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> LLMParams:
        raw = raw or {}
        return cls(
            temperature=raw.get("temperature", DEFAULT_TEMPERATURE),
            max_tokens=raw.get("max_tokens", DEFAULT_MAX_TOKENS),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {"temperature": self.temperature, "max_tokens": self.max_tokens}


# ─── ErrorPolicy ──────────────────────────────────────────────────


class OnTimeoutPolicy(StrEnum):
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    FAIL_FAST = "fail_fast"
    FALLBACK_PROVIDER = "fallback_provider"


class BackoffStrategy(StrEnum):
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    CONSTANT = "constant"


DEFAULT_MAX_RETRIES: Final[int] = 3
MAX_RETRIES_MIN: Final[int] = 0
MAX_RETRIES_MAX: Final[int] = 10


@dataclass(frozen=True, slots=True)
class ErrorPolicy:
    """Declarative retry policy shape — NFR14.

    Sprint 1 persists the shape only; the active retry dispatcher is deferred
    (Story 4.6, out of this slice).
    """

    on_timeout: OnTimeoutPolicy = OnTimeoutPolicy.RETRY_WITH_BACKOFF
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL

    def __post_init__(self) -> None:
        if not (MAX_RETRIES_MIN <= self.max_retries <= MAX_RETRIES_MAX):
            raise DomainValidationError(
                f"max_retries must be in [{MAX_RETRIES_MIN}, {MAX_RETRIES_MAX}], "
                f"got {self.max_retries}"
            )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> ErrorPolicy:
        raw = raw or {}
        return cls(
            on_timeout=OnTimeoutPolicy(raw.get("on_timeout", OnTimeoutPolicy.RETRY_WITH_BACKOFF)),
            max_retries=raw.get("max_retries", DEFAULT_MAX_RETRIES),
            backoff_strategy=BackoffStrategy(
                raw.get("backoff_strategy", BackoffStrategy.EXPONENTIAL)
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "on_timeout": str(self.on_timeout),
            "max_retries": self.max_retries,
            "backoff_strategy": str(self.backoff_strategy),
        }


# ─── ProviderChain ────────────────────────────────────────────────


class ProviderId(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


@dataclass(frozen=True, slots=True)
class ProviderChain:
    """Ordered, de-duplicated list of LLM providers (fallback chain).

    Encapsulates the P-13 rule — "a provider present twice defeats the fallback
    strategy" — that previously lived only in the Pydantic ``field_validator``
    on ``UpdateTemplateRequest.provider_chain``.
    """

    providers: tuple[ProviderId, ...] = ()

    def __post_init__(self) -> None:
        if len(self.providers) != len(set(self.providers)):
            raise DomainValidationError("provider_chain must not contain duplicate providers")

    @classmethod
    def from_mapping(cls, raw: Sequence[str] | None) -> ProviderChain:
        if raw is None:
            return cls(())
        return cls(tuple(ProviderId(p) for p in raw))

    def to_mapping(self) -> list[str]:
        return [str(p) for p in self.providers]


# ─── PushMemorySettings ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PushMemorySettings:
    """Push Memory configuration for a template (Story 3.5 AC1/AC2, FR20).

    Absent = disabled — same posture as ``decay_policy`` absent meaning "no
    decay" (Story 3.4). No cross-field validation needed, mirrors
    :class:`ErrorPolicy`, the simplest existing VO in this file.
    """

    namespace: str | None = None
    optin: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> PushMemorySettings:
        raw = raw or {}
        return cls(namespace=raw.get("namespace"), optin=bool(raw.get("optin", False)))

    def to_mapping(self) -> dict[str, Any]:
        """Emit nothing for a template that never configured Push Memory —
        mirrors :meth:`ErrorPolicy.to_mapping`'s progressive-build semantics."""
        if self.namespace is None and self.optin is False:
            return {}
        return {"namespace": self.namespace, "optin": self.optin}


# ─── AgentConfig — composite VO, single parse/serialize point ──────


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Composite VO over the ``agent_templates.config`` JSONB.

    Every field is optional because the JSONB shape is built PROGRESSIVELY:
    ``prompt_base``/``role``/``input_contract``/``output_contract`` are set at
    creation (from the archetype skeleton); ``system_prompt``, ``llm_model``,
    ``llm_params``, ``provider_chain``, ``error_policy`` only appear once a
    template update sets them.
    """

    prompt_base: str | None = None
    role: str | None = None
    system_prompt: str | None = None
    input_contract: Contract | None = None
    output_contract: Contract | None = None
    llm_model: str | None = None
    llm_params: LLMParams | None = None
    provider_chain: ProviderChain | None = None
    error_policy: ErrorPolicy | None = None
    push_memory: PushMemorySettings | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> AgentConfig:
        """The ONE place that reads the ``config`` dict into a typed VO."""
        raw = raw or {}
        return cls(
            prompt_base=raw.get("prompt_base"),
            role=raw.get("role"),
            system_prompt=raw.get("system_prompt"),
            input_contract=(
                Contract.from_mapping(raw["input_contract"]) if "input_contract" in raw else None
            ),
            output_contract=(
                Contract.from_mapping(raw["output_contract"]) if "output_contract" in raw else None
            ),
            llm_model=raw.get("llm_model"),
            # Story 2.8 P-02 — a bare ``"llm_params" in raw`` also matched a key
            # present with a ``null`` (or ``{}``, or a scalar) value, and
            # ``LLMParams.from_mapping`` then fabricated the defaults
            # ``(0.7, 4096)``. Two templates in that state compared equal, so
            # ``check_llm_diversity`` returned ``is_diverse=False`` instead of the
            # ``None`` its three-state contract promises for an unconfigured
            # template. Only a non-empty mapping counts as "configured".
            # The sibling optional VOs below keep the ``in raw`` form: same latent
            # hole, but no caller depends on telling absent from null for them.
            llm_params=(
                LLMParams.from_mapping(raw["llm_params"])
                if isinstance(raw.get("llm_params"), Mapping) and raw["llm_params"]
                else None
            ),
            provider_chain=(
                ProviderChain.from_mapping(raw["provider_chain"])
                if "provider_chain" in raw
                else None
            ),
            error_policy=(
                ErrorPolicy.from_mapping(raw["error_policy"]) if "error_policy" in raw else None
            ),
            push_memory=(
                PushMemorySettings.from_mapping(raw["push_memory"])
                if "push_memory" in raw
                else None
            ),
        )

    def to_mapping(self) -> dict[str, Any]:
        """The ONE place that (re)serializes back to a plain JSONB-ready dict.

        Only keys whose VO field is non-``None`` are emitted, mirroring the
        progressive-build semantics of the stored ``config`` (a template that
        has never been updated has no ``system_prompt`` key at all).
        """
        out: dict[str, Any] = {}
        if self.prompt_base is not None:
            out["prompt_base"] = self.prompt_base
        if self.role is not None:
            out["role"] = self.role
        if self.system_prompt is not None:
            out["system_prompt"] = self.system_prompt
        if self.input_contract is not None:
            out["input_contract"] = self.input_contract.to_mapping()
        if self.output_contract is not None:
            out["output_contract"] = self.output_contract.to_mapping()
        if self.llm_model is not None:
            out["llm_model"] = self.llm_model
        if self.llm_params is not None:
            out["llm_params"] = self.llm_params.to_mapping()
        if self.provider_chain is not None:
            out["provider_chain"] = self.provider_chain.to_mapping()
        if self.error_policy is not None:
            out["error_policy"] = self.error_policy.to_mapping()
        if self.push_memory is not None:
            out["push_memory"] = self.push_memory.to_mapping()
        return out

    def merge_updates(
        self,
        *,
        system_prompt: str | None = None,
        input_contract: Contract | None = None,
        output_contract: Contract | None = None,
        llm_model: str | None = None,
        llm_params: LLMParams | None = None,
        provider_chain: ProviderChain | None = None,
        error_policy: ErrorPolicy | None = None,
        push_memory: PushMemorySettings | None = None,
    ) -> AgentConfig:
        """Return a new ``AgentConfig`` with only the non-``None`` kwargs
        overridden — mirrors ``UpdateTemplateRequest``'s PATCH-like semantics
        (a keyword left ``None`` means "leave unchanged").

        ``prompt_base``/``role`` are never patched here: Sprint 1 has no
        endpoint that changes them after creation.
        """
        # Explicit per-field kwargs (rather than a heterogeneous dict unpacked
        # into ``replace``) so each override keeps its declared type under
        # ``mypy --strict``; a ``None`` argument means "leave the field as-is".
        return dataclasses.replace(
            self,
            system_prompt=self.system_prompt if system_prompt is None else system_prompt,
            input_contract=self.input_contract if input_contract is None else input_contract,
            output_contract=self.output_contract if output_contract is None else output_contract,
            llm_model=self.llm_model if llm_model is None else llm_model,
            llm_params=self.llm_params if llm_params is None else llm_params,
            provider_chain=self.provider_chain if provider_chain is None else provider_chain,
            error_policy=self.error_policy if error_policy is None else error_policy,
            push_memory=self.push_memory if push_memory is None else push_memory,
        )


__all__ = [
    "AgentConfig",
    "Archetype",
    "BackoffStrategy",
    "Contract",
    "DomainValidationError",
    "ErrorPolicy",
    "LLMParams",
    "OnTimeoutPolicy",
    "ProviderChain",
    "ProviderId",
    "PushMemorySettings",
    "Version",
]
