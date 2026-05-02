# Repository Pattern — Usage & Debugging Runbook

**Owner:** shared/repositories — Story 1.5
**Companion ADR:** [`repository-pattern.md`](../decisions/repository-pattern.md)

## When to use a repository

ALL DB access from `features/m*/`, `api/`, and any future module **must**
go through a class in `agentive_backend.shared.repositories`. The
`import-linter` Contract 3 (`no-direct-db-access-from-features`) blocks
direct imports of `sqlalchemy` / `psycopg` / `asyncpg` from features.

## Canonical call pattern

```python
from agentive_backend.shared.repositories import MemoryChunkRepo

repo = MemoryChunkRepo(session_factory=session_factory)

# Read
chunk = await repo.get_by_id(chunk_id, tenant_id=tenant_id)

# Write
new_chunk = await repo.create(
    namespace_id=ns_id,
    content="...",
    tenant_id=tenant_id,
)
```

The `tenant_id` kwarg defaults to `None` for the single-tenant MVP. When
multi-tenant Growth lands (Sprint 4+), middleware binds the request's
tenant UUID and call sites switch from `None` to that UUID — no other
change.

## How to add a new repository

1. Create `backend/src/agentive_backend/shared/repositories/<entity>_repo.py`.
2. Subclass `BaseRepo`. Set the docstring to `"""Public API surface for <Entity>. ALL DB access must go through this class."""`.
3. Implement methods using `async with self.with_tenant(tenant_id) as session: …`. Never expose `AsyncSession` in the signature.
4. Re-export the class from `shared/repositories/__init__.py` (sorted alphabetically in `__all__`).
5. Add unit tests under `backend/tests/unit/repositories/test_<entity>_repo.py` (mock `session_factory`).
6. Add at least one happy-path integration test under `backend/tests/integration/repositories/test_<entity>_crud.py` (or extend `test_repos_crud.py`).
7. If the new entity is tenant-scoped and not yet covered, add a cross-tenant isolation test under `backend/tests/integration/repositories/test_isolation_<entity>.py`.
8. Update this runbook only if you introduce a new pattern (e.g. an INSERT-only repo like `AuditEventRepo`).

## Debugging RLS issues

### Symptom: query returns 0 rows but you know data exists

The RLS policy `tenant_isolation` on the table is filtering them out.
Likely causes:

- **Forgot `with_tenant(uuid)`** — the GUC `app.tenant_id` was never bound,
  so `current_setting('app.tenant_id', true)` returns NULL (the `true`
  argument means "missing OK"). The policy clause
  `tenant_id IS NULL OR tenant_id = current_setting(...)::uuid` then
  evaluates to NULL/UNKNOWN for tenant-bound rows (because
  `tenant_id = NULL::uuid` is NULL), and the WHERE filter treats NULL as
  false. **No exception is raised — the rows are silently invisible.**
- **Manually committed mid-block** — `set_config(... is_local=true)` is
  transaction-scoped. Calling `await session.commit()` inside your
  `with_tenant()` block resets the binding; subsequent queries on the
  same session lose the tenant context and behave as the case above.
- **Wrong tenant UUID bound** — the policy filters strictly. Verify the
  GUC matches the row's `tenant_id` column.

Diagnostic:

```sql
-- Inside the same session as the failing query:
SELECT current_setting('app.tenant_id', true);
```

If the result is NULL or empty, your `with_tenant()` either ran with
`None` or a `commit` happened too early.

### Symptom: `invalid input syntax for type uuid: ""`

This is rare and only happens if a caller manually executes
`SELECT set_config('app.tenant_id', '', true)` (an empty string, not
NULL) before running a query against a tenant-scoped table.
`with_tenant(None)` does NOT trigger this path — it skips `set_config`
entirely, leaving `current_setting()` to return NULL, which is handled
correctly by the policy short-circuit (see previous symptom).

If you hit this error, search for a stray `set_config('app.tenant_id', '', true)`
or a misuse of raw SQL bypassing the repo layer.

### Useful SQL forensics

```sql
-- Is RLS enabled + forced on a table?
SELECT relrowsecurity, relforcerowsecurity
FROM pg_class
WHERE relname = 'memory_chunks';

-- What policies apply to a table?
SELECT policyname, cmd, qual, with_check
FROM pg_policies
WHERE tablename = 'memory_chunks';

-- What grants does agentive_app have on a table?
SELECT grantee, privilege_type
FROM information_schema.role_table_grants
WHERE table_name = 'memory_chunks' AND grantee = 'agentive_app';
```

## Adding cross-tenant data in tests

Tests that need to plant data under multiple tenants in the **same** setup
must use the BYPASSRLS seed role provisioned in
`backend/tests/integration/repositories/conftest.py`:

```python
async def test_isolation_memory_chunks(seed_session_factory, app_session_factory):
    # 1. Seed two tenants via BYPASSRLS role
    async with seed_session_factory() as session:
        # raw INSERT — bypasses RLS so we can plant cross-tenant rows
        ...

    # 2. Query via the production runtime role — RLS applies
    repo = MemoryChunkRepo(session_factory=app_session_factory)
    rows = await repo.list_by_namespace(ns_id, tenant_id=tenant_a)
```

`agentive_test_seed WITH BYPASSRLS` is **never** present in production.
The role is created only by the test conftest. Do not add `BYPASSRLS` to
any production migration.

## When to deviate from the pattern

The only documented exception is `shared/event_bus/`, which uses raw
`text()` queries against `outbox_events` because it lives at the same
layer as `shared/repositories/` (peer, not consumer). Story 9.1 will
revisit whether to migrate the event_bus internals to consume `OutboxRepo`
once the audit consumer is in place.

If you find yourself wanting to bypass a repository, write the use case
in your PR description and reference this runbook — three or more such
cases would be a signal to refactor the base class.

## References

- ADR: [`docs/decisions/repository-pattern.md`](../decisions/repository-pattern.md).
- Story: `_bmad-output/implementation-artifacts/1-5-core-repositories-migrations.md`.
- Architecture FMEA Mitigation #1: `_bmad-output/planning-artifacts/architecture.md` lines 497-506.
- Initial migration RLS declarations: `backend/alembic/versions/20260419_000000_initial.py:441-468`.
