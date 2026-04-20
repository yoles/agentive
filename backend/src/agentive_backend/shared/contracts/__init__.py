"""Versioned contracts — shared schemas between features.

**Stub Sprint 0** — filled progressively as features are built.

Feature modules that need to exchange data MUST declare a contract here,
versioned, rather than import each other directly.
"""

from __future__ import annotations


class ContractBase:
    """Placeholder base for versioned contracts — implemented Story 2.2+."""

    schema_version: str = "0.0.0"

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.schema_version == "0.0.0":
            raise NotImplementedError(
                f"Contract {cls.__name__} must declare schema_version (implemented Story 2.2)."
            )
