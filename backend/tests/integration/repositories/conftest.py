"""Integration fixtures for ``shared.repositories`` tests.

These fixtures bring up a real Postgres + pgvector container (via the
session-scoped ``postgres_container`` fixture in ``backend/tests/conftest.py``),
provision the same 3 application roles + 1 dedicated test seed role that
production uses (``infra/postgres/init.sql``), then run ``alembic upgrade
head`` to materialize the full schema (tables, RLS policies, grants,
partitions, seed user John).

Why a 4th role ``agentive_test_seed WITH BYPASSRLS``:
    The migration applies ``ALTER TABLE ... FORCE ROW LEVEL SECURITY`` so
    even ``agentive_owner`` is subject to RLS. Cross-tenant test setup
    (insert rows for tenant A and tenant B in the same fixture) cannot
    happen without a role that bypasses RLS. ``BYPASSRLS`` is a Postgres
    role attribute that is **never** granted in production — it's a
    test-only escape hatch.

The fixtures expose three session factories:

* ``app_session_factory``: connects as ``agentive_app`` (the production
  runtime role). All RLS-driven assertions run against this factory.
* ``seed_session_factory``: connects as ``agentive_test_seed``. Used to
  bootstrap data the test needs to see across tenant boundaries.
* ``owner_session_factory``: connects as ``agentive_owner``. Mostly for
  schema introspection and TRUNCATE between tests.

A ``clean_repository_tables`` autouse fixture truncates the tenant-scoped
tables between tests (excluding ``users`` so the seed John row survives,
and excluding ``audit_events`` whose monthly partitions make TRUNCATE
expensive).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Test-only role passwords. Hardcoded by design — these roles only exist
# inside an ephemeral testcontainer that lives for the duration of the
# pytest session.
_OWNER_PASSWORD = "test_owner"
_APP_PASSWORD = "test_app"
_AUDIT_ADMIN_PASSWORD = "test_audit_admin"
_SEED_PASSWORD = "test_seed"

# Tenant-scoped tables to TRUNCATE between tests. ``users`` is excluded so
# the migration's seed (1 row owner John) survives. ``audit_events`` is
# excluded — its 12 monthly partitions make TRUNCATE expensive and audit
# tests are isolated and clean their own data.
_TRUNCATE_TABLES = (
    "memory_chunks",
    "chunk_embeddings",
    "namespaces",
    "workflows",
    "workflow_runs",
    "agent_templates",
    "agent_instances",
    "prompts",
    "outbox_events",
    "feature_flags",
    "sessions",
)


def _admin_dsn(postgres_container: object) -> str:
    """Return the psycopg sync DSN for the testcontainer superuser."""
    sa_url = postgres_container.get_connection_url()  # type: ignore[attr-defined]
    # testcontainers returns ``postgresql+psycopg://...`` — strip the dialect.
    return sa_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _swap_user(dsn: str, username: str, password: str) -> str:
    """Return ``dsn`` with the auth segment replaced by ``username:password``."""
    # postgresql://USER:PASS@HOST:PORT/DB → split on '@' once, replace lhs.
    scheme, rest = dsn.split("://", 1)
    _, host_db = rest.split("@", 1)
    return f"{scheme}://{username}:{password}@{host_db}"


@pytest.fixture(scope="session")
def roles_provisioned(postgres_container: object) -> Iterator[None]:
    """Mirror ``infra/postgres/init.sql`` inside the testcontainer.

    testcontainers initializes the DB with a single superuser; the production
    init.sql that creates the 3 application roles is **not** applied. We
    replay the relevant pieces here:

      * Create ``agentive_owner``, ``agentive_app``, ``agentive_audit_admin``.
      * Create a 4th ``agentive_test_seed WITH BYPASSRLS`` for cross-tenant
        test setup (never present in production — see module docstring).
      * Reassign DB + schema ownership to ``agentive_owner`` so the
        subsequent ``alembic upgrade head`` runs as owner.
      * Create the ``vector`` extension as superuser (CREATE EXTENSION
        requires superuser privilege).
    """
    sync_dsn = _admin_dsn(postgres_container)

    with psycopg.connect(sync_dsn, autocommit=True) as conn, conn.cursor() as cur:
        # Roles — idempotent: skip if they already exist (e.g. fixture reused).
        for role, password, extra in (
            ("agentive_owner", _OWNER_PASSWORD, ""),
            ("agentive_app", _APP_PASSWORD, ""),
            ("agentive_audit_admin", _AUDIT_ADMIN_PASSWORD, ""),
            ("agentive_test_seed", _SEED_PASSWORD, "BYPASSRLS"),
        ):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                # Postgres CREATE ROLE rejects bind placeholders for PASSWORD
                # (same gotcha as NOTIFY in Story 1.4 Debug Log #1). Use
                # psycopg.sql.Literal for safe quoting of the test-only
                # password constants.
                cur.execute(
                    sql.SQL(
                        "CREATE ROLE {role} WITH LOGIN PASSWORD {password} "
                        "NOSUPERUSER NOCREATEDB NOCREATEROLE {extra}"
                    ).format(
                        role=sql.Identifier(role),
                        password=sql.Literal(password),
                        extra=sql.SQL(extra),
                    )
                )

        # Identify the database name from the DSN — testcontainers picks it.
        db_name = sync_dsn.rsplit("/", 1)[-1].split("?", 1)[0]

        # Reassign DB + schema ownership so the migration runs as owner.
        cur.execute(f'ALTER DATABASE "{db_name}" OWNER TO agentive_owner')

    # Open a fresh connection so ALTER SCHEMA + CREATE EXTENSION run after
    # the database ownership switch above. Same DSN — the fresh session
    # picks up the new ``agentive_owner`` ownership for default privileges.
    with psycopg.connect(sync_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("ALTER SCHEMA public OWNER TO agentive_owner")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO agentive_app')
        cur.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO agentive_audit_admin')
        cur.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO agentive_test_seed')
        cur.execute("GRANT USAGE ON SCHEMA public TO agentive_app")
        cur.execute("GRANT USAGE ON SCHEMA public TO agentive_audit_admin")
        cur.execute("GRANT USAGE ON SCHEMA public TO agentive_test_seed")
        # Default privileges so newly-created tables are visible to seed/app.
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agentive_app"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agentive_audit_admin"
        )
        # The seed role gets full SELECT/INSERT/UPDATE/DELETE on every
        # tenant table so it can plant data across tenants. BYPASSRLS lets
        # it ignore the policy on read AND write.
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentive_test_seed"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agentive_test_seed"
        )

    yield


def _parse_host_port_db(dsn: str) -> tuple[str, str, str]:
    """Extract (host, port, db) from a sync ``postgresql://...`` DSN."""
    # postgresql://user:pass@host:port/db
    _scheme, rest = dsn.split("://", 1)
    _, host_db = rest.split("@", 1)
    host_port, db = host_db.rsplit("/", 1)
    db = db.split("?", 1)[0]
    host, port = host_port.split(":", 1)
    return host, port, db


@pytest.fixture(scope="session")
def migrated_db(postgres_container: object, roles_provisioned: None) -> Iterator[str]:
    """Run ``alembic upgrade head`` against the testcontainer.

    Yields the **owner** DSN (the migration must run as ``agentive_owner``,
    not the testcontainer superuser, otherwise table grants for
    ``agentive_app`` / ``agentive_audit_admin`` won't apply correctly).

    The migration's ``alembic/env.py`` reads
    ``settings.database_url_owner`` directly — it doesn't honour
    ``cfg.set_main_option("sqlalchemy.url", ...)`` because the env file
    overrides the URL itself (see ``backend/alembic/env.py:21-25``). So we
    set the underlying ``POSTGRES_*`` env vars to point at the
    testcontainer **before** calling ``command.upgrade``.
    """
    sync_dsn = _admin_dsn(postgres_container)
    owner_dsn = _swap_user(sync_dsn, "agentive_owner", _OWNER_PASSWORD)
    host, port, db = _parse_host_port_db(sync_dsn)

    # Reset the public schema before migration so we tolerate prior test
    # packages (e.g. tests/integration/event_bus) that may have created
    # ``outbox_events`` via inline DDL inside the same session-scoped
    # testcontainer. The migration would otherwise fail with
    # ``relation "outbox_events" already exists``. Re-create the
    # extension + ownership grants that ``init.sql`` would have applied.
    with psycopg.connect(sync_dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute("ALTER SCHEMA public OWNER TO agentive_owner")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute("GRANT USAGE ON SCHEMA public TO agentive_app")
        cur.execute("GRANT USAGE ON SCHEMA public TO agentive_audit_admin")
        cur.execute("GRANT USAGE ON SCHEMA public TO agentive_test_seed")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agentive_app"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agentive_audit_admin"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentive_test_seed"
        )
        cur.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO agentive_test_seed"
        )

    # Snapshot every env var we're about to override so we can restore them.
    overrides = {
        "POSTGRES_HOST": host,
        "POSTGRES_PORT": port,
        "POSTGRES_DB": db,
        "POSTGRES_OWNER_PASSWORD": _OWNER_PASSWORD,
        "POSTGRES_APP_PASSWORD": _APP_PASSWORD,
    }
    previous = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        os.environ[k] = v

    # Mutate the live settings object — env.py imported ``settings`` by name,
    # so reassigning ``_config.settings`` would not update its binding.
    # Directly setting the fields on the existing instance does (Pydantic
    # BaseSettings is mutable; ``database_url_owner`` is a computed_field
    # that reads these fields lazily on every access).
    from pydantic import SecretStr

    from agentive_backend.shared import config as _config

    _previous_settings_fields = {
        "postgres_host": _config.settings.postgres_host,
        "postgres_port": _config.settings.postgres_port,
        "postgres_db": _config.settings.postgres_db,
        "postgres_owner_password": _config.settings.postgres_owner_password,
        "postgres_app_password": _config.settings.postgres_app_password,
    }
    _config.settings.postgres_host = host
    _config.settings.postgres_port = int(port)
    _config.settings.postgres_db = db
    _config.settings.postgres_owner_password = SecretStr(_OWNER_PASSWORD)
    _config.settings.postgres_app_password = SecretStr(_APP_PASSWORD)

    here = Path(__file__).resolve()
    backend_root = next(parent for parent in here.parents if (parent / "alembic.ini").exists())

    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", owner_dsn)

    sa_owner_dsn = owner_dsn.replace("postgresql://", "postgresql+psycopg://", 1)

    try:
        command.upgrade(cfg, "head")
        yield sa_owner_dsn
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for field, value in _previous_settings_fields.items():
            setattr(_config.settings, field, value)


def _build_factory(dsn: str) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(dsn, pool_pre_ping=True)
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def app_session_factory(
    migrated_db: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory bound to ``agentive_app`` — RLS applies."""
    sync_dsn = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    app_sync_dsn = _swap_user(sync_dsn, "agentive_app", _APP_PASSWORD)
    sa_dsn = app_sync_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(sa_dsn, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def seed_session_factory(
    migrated_db: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory bound to ``agentive_test_seed`` — RLS bypassed."""
    sync_dsn = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    seed_sync_dsn = _swap_user(sync_dsn, "agentive_test_seed", _SEED_PASSWORD)
    sa_dsn = seed_sync_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(sa_dsn, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def owner_session_factory(
    migrated_db: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory bound to ``agentive_owner`` — for schema introspection."""
    engine = create_async_engine(migrated_db, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def audit_admin_session_factory(
    migrated_db: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory bound to ``agentive_audit_admin`` — RLS applies.

    Has ``SELECT, INSERT`` on ``audit_events`` (and partitions); no
    ``BYPASSRLS``. Used to validate the RLS ``tenant_isolation`` policy
    on ``audit_events`` end-to-end.
    """
    sync_dsn = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    admin_sync_dsn = _swap_user(sync_dsn, "agentive_audit_admin", _AUDIT_ADMIN_PASSWORD)
    sa_dsn = admin_sync_dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(sa_dsn, pool_pre_ping=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def clean_repository_tables(migrated_db: str) -> Iterator[None]:
    """Truncate tenant-scoped tables before every test in this package.

    ``users`` is preserved (seed John) and ``audit_events`` is left alone
    (partitions make TRUNCATE expensive — audit tests clean themselves).
    """
    sync_dsn = migrated_db.replace("postgresql+psycopg://", "postgresql://", 1)
    tables_csv = ", ".join(_TRUNCATE_TABLES)
    with (
        psycopg.connect(sync_dsn, autocommit=True) as conn,
        conn.cursor() as cur,
        suppress(psycopg.errors.UndefinedTable),
    ):
        cur.execute(f"TRUNCATE TABLE {tables_csv} RESTART IDENTITY CASCADE")
    yield
