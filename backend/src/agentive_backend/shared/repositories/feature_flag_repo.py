"""Public API surface for :class:`FeatureFlag`. ALL DB access must go through this class."""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agentive_backend.infra.db.models import FeatureFlag
from agentive_backend.shared.repositories.base import BaseRepo

# Sentinel used by :meth:`FeatureFlagRepo.set` to distinguish "caller did not
# provide ``description``" (preserve existing value on update) from "caller
# explicitly passed ``None``" (clear the description).
_UNSET: Final[Any] = object()


class FeatureFlagRepo(BaseRepo):
    """Public API surface for FeatureFlag. ALL DB access must go through this class.

    Feature flags are GLOBAL — the table has no ``tenant_id`` column, so
    these methods do not accept a ``tenant_id`` parameter. The methods open
    transactions via ``with_tenant(None)`` which skips the RLS binding.
    """

    async def get(self, name: str) -> FeatureFlag | None:
        async with self.with_tenant(None) as session:
            return await session.get(FeatureFlag, name)

    async def list_enabled(self) -> list[FeatureFlag]:
        async with self.with_tenant(None) as session:
            stmt = select(FeatureFlag).where(FeatureFlag.enabled.is_(True))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def set(
        self,
        *,
        name: str,
        enabled: bool,
        rollout_percentage: int = 0,
        description: str | None | object = _UNSET,
    ) -> None:
        """Upsert a feature flag.

        ``description`` is preserved on update unless explicitly provided
        (passing ``None`` clears it; omitting the kwarg leaves it untouched).
        """
        async with self.with_tenant(None) as session:
            insert_values: dict[str, Any] = {
                "name": name,
                "enabled": enabled,
                "rollout_percentage": rollout_percentage,
                "description": None if description is _UNSET else description,
            }
            update_values: dict[str, Any] = {
                "enabled": enabled,
                "rollout_percentage": rollout_percentage,
            }
            if description is not _UNSET:
                update_values["description"] = description
            stmt = pg_insert(FeatureFlag).values(**insert_values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[FeatureFlag.name],
                set_=update_values,
            )
            await session.execute(stmt)
