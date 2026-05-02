# Repository Pattern + RLS Tenant Binding

**Status:** Accepted
**Sprint:** Sprint 0 (Story 1.5)
**Date:** 2026-05-01

## Context

The Agentive backend is structured around 12 isolated `features/m*/` modules
that must never import each other directly (enforced by `import-linter`
Contract 1). Persistence is shared infrastructure: every feature reads and
writes the same Postgres database via SQLAlchemy 2.0 async.

We need a layer between features and the database that:

1. Forbids feature code from importing `AsyncSession` / `asyncpg` / `psycopg`
   directly — a single broken import would let a feature bypass our
   security model.
2. Binds the Postgres GUC `app.tenant_id` at the start of every transaction
   so the RLS policy `tenant_isolation` (declared on 10 tenant-scoped
   tables in the initial migration) takes effect.
3. Stays cheap to reason about — Sprint 0 ships with `tenant_id=None`
   everywhere, but the same code path will support multi-tenant Growth
   from Sprint 4 without rewrites.

## Decision

Adopt a **Repository Pattern** rooted at `agentive_backend.shared.repositories`:

- A common abstract `BaseRepo` exposes a single async context manager
  `with_tenant(tenant_id: UUID | None)` that opens a session, optionally
  binds `app.tenant_id` via `set_config()`, yields the session, and
  commits/rolls back on exit.
- One concrete repository class per aggregate
  (`UserRepo`, `MemoryChunkRepo`, `WorkflowRepo`, …). All public methods
  internally call `with_tenant()`. None expose `AsyncSession` in their
  signatures.
- `import-linter` Contract 3 (`no-direct-db-access-from-features`) forbids
  features from importing `sqlalchemy` / `psycopg` / `asyncpg`. CI
  enforces this on every push.

We use Postgres' `set_config('app.tenant_id', :tid, true)` (the third
argument means *transaction-local*, equivalent to `SET LOCAL`) instead of
the literal `SET LOCAL` syntax because the SQL command does not accept
bind placeholders for its value — the analogous discovery was made in
Story 1.4 with `NOTIFY` (`pg_notify()` was used for the same reason).

## Options Considered

| Option | Verdict |
|---|---|
| **Repository Pattern (this ADR)** | ✅ Adopted — explicit boundary, mockable, swappable backend, mature pattern. |
| **Active Record** (each ORM model has CRUD methods) | ❌ Couples persistence to model classes; harder to mock; tempts feature code to call `.save()` directly. |
| **Plain DAO functions** (no class hierarchy) | ⚠️ Workable but loses the single-base-class hook for `with_tenant`. Every function would need to repeat the GUC binding boilerplate, inviting drift. |
| **Middleware-driven `app.tenant_id`** (FastAPI middleware that binds the GUC on every request) | ❌ Tied to HTTP boundary; CLI / worker / Alembic invocations would silently lose tenant context. The repository is the right seam. |
| **`SET LOCAL` literal SQL** | ❌ Rejected — Postgres rejects bind placeholders for `SET LOCAL <name> = <value>`. Inlining the UUID string is safe (typing enforces UUID-only) but `set_config()` is more idiomatic and explicit. |

## Consequences

**Positive**

- Features never see `AsyncSession`. Mocking the repository is trivial and
  unit tests don't need testcontainers.
- The Sprint-4 transition to multi-tenant Growth is a one-line change at
  every call site (`tenant_id=request.user.tenant_id` instead of `None`).
  No SQL changes, no schema changes.
- Defense in depth: even if a feature tries to bypass `with_tenant()`,
  Postgres `FORCE ROW LEVEL SECURITY` ensures cross-tenant data is
  invisible to the `agentive_app` runtime role.
- The `OutboxRepo` published in Story 1.5 gives Story 9.1 (audit consumer)
  and Story 8.5 (Event Hooks) a clean read surface over `outbox_events`
  without touching the event_bus internals.

**Negative**

- ~30 lines per concrete repo — boilerplate cost for solo dev. Mitigation:
  the `BaseRepo.with_tenant()` already handles the heavy lifting; concrete
  classes are 2-3 representative methods each (CRUD only at Sprint 0).
- `with_tenant(None)` does NOT execute `set_config`, so
  `current_setting('app.tenant_id', true)` returns NULL (the `true`
  argument means "missing OK, return NULL"). The RLS policy clause
  `tenant_id IS NULL OR tenant_id = current_setting(...)::uuid` then
  evaluates to NULL/UNKNOWN for tenant-bound rows (the equality compares
  against `NULL::uuid`), which the WHERE filter treats as false — so
  tenant-bound rows are silently filtered. Global rows
  (`tenant_id IS NULL`) match the first clause and remain visible.
  No cast error is raised at runtime because `current_setting` never
  returns an empty string. (If a caller manually set the GUC to `''`,
  Postgres WOULD raise `invalid input syntax for type uuid` — that path
  is not reachable from `with_tenant(None)`.) See
  `docs/runbooks/repositories-usage.md` for forensics.
- Cross-tenant test setup needs a 4th Postgres role
  (`agentive_test_seed WITH BYPASSRLS`), provisioned only in
  `tests/integration/repositories/conftest.py`. Documented as a test-only
  escape hatch.

## Specific upsert semantics — `ChunkEmbeddingRepo.upsert`

`ChunkEmbeddingRepo.upsert(chunk_id, model, embedding, tenant_id)` uses
`INSERT ... ON CONFLICT (chunk_id, model) DO UPDATE SET embedding = ...`
and **deliberately does not include `tenant_id` in the SET clause**. The
existing row's `tenant_id` is preserved on update. Rationale:

- An `agentive_app` session bound to `app.tenant_id = bound_tenant`
  invokes `with_tenant(bound_tenant)` before the upsert. The RLS
  `tenant_isolation` policy USING clause filters out any existing row
  whose `tenant_id` differs from `bound_tenant`, so `ON CONFLICT` only
  fires for rows already owned by `bound_tenant`. The "preserved"
  `tenant_id` is therefore mathematically equal to `bound_tenant`.
- A test using the BYPASSRLS seed role bypasses both policies. If a test
  ever upserts the same `(chunk_id, model)` across tenants, the second
  call will silently keep the first tenant's `tenant_id`. This is by
  design — production upserts cannot reach that state.

Including `tenant_id` in the UPDATE SET is intentionally avoided because:
1. It would be redundant in the production path (RLS already constrains
   the value to `bound_tenant`).
2. It would let a BYPASSRLS test silently overwrite a tenant assignment,
   which is a weaker invariant than the current behavior.

## Performance

- Each `with_tenant(tenant_id)` adds **one** `set_config()` round-trip
  (~0.1 ms on local testcontainer). Below the noise floor for any
  user-facing endpoint (NFR1: < 500 ms p95).
- `set_config()` is transaction-local, so it never leaks across the
  connection pool. The runtime cost is paid per logical transaction, not
  per query.

## Revisitability

- If the Sprint-4 multi-tenant traffic exceeds expectations and the
  RLS policy becomes a hot path, switch to **per-tenant connection pools**
  (one pool per tenant; pool-level GUC binding instead of per-transaction).
  The repository surface stays unchanged.
- If the abstraction is ever leaking (a method requires direct
  `AsyncSession` access for a niche use case), prefer **adding a
  repository method** over exposing the session. If three or more
  features need the same pattern, promote it to `BaseRepo`.

## References

- Architecture: `_bmad-output/planning-artifacts/architecture.md`
  - Data Architecture (line 343) — SQLAlchemy 2.0 async + Repository Pattern.
  - FMEA Mitigation #1 (lines 497-506) — RLS + `SET LOCAL app.tenant_id` rationale (RPN ≥ 75).
  - Security Hardening #2 (lines 625-636) — `import-linter` Contract 3.
  - Anti-pattern Bannis (line 1256) — `AsyncSession` outside repos.
- Initial migration: `backend/alembic/versions/20260419_000000_initial.py:441-468` — RLS policy declarations.
- Story: `_bmad-output/implementation-artifacts/1-5-core-repositories-migrations.md`.
- Companion runbook: `docs/runbooks/repositories-usage.md`.
