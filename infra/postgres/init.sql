-- Agentive — PostgreSQL init.sql (exécuté au premier démarrage du container db)
-- Crée les 3 rôles applicatifs + la base agentive + grants appropriés
--
-- Mots de passe consommés depuis les variables d'environnement du container :
--   POSTGRES_OWNER_PASSWORD, POSTGRES_APP_PASSWORD, POSTGRES_AUDIT_ADMIN_PASSWORD
--
-- Les migrations Alembic s'exécutent ensuite via `agentive_owner`
-- (cf alembic/env.py — DATABASE_URL_OWNER)

\set owner_password `echo "$POSTGRES_OWNER_PASSWORD"`
\set app_password `echo "$POSTGRES_APP_PASSWORD"`
\set audit_admin_password `echo "$POSTGRES_AUDIT_ADMIN_PASSWORD"`

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Rôles (principe du moindre privilège)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- Owner : propriétaire des objets, utilisé par Alembic pour les migrations
-- Peut CREATE/DROP/ALTER — usage strict migrations, pas d'application runtime
CREATE ROLE agentive_owner WITH LOGIN PASSWORD :'owner_password' NOSUPERUSER CREATEDB NOCREATEROLE;

-- App runtime : utilisé par FastAPI
-- SELECT/INSERT/UPDATE/DELETE sur tables métier, mais pas d'écriture sur audit_events
CREATE ROLE agentive_app WITH LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- Audit admin : écritures dédiées audit_events
-- INSERT-only sur audit_events parent, SELECT sur toutes les partitions
CREATE ROLE agentive_audit_admin WITH LOGIN PASSWORD :'audit_admin_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Base de données
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- La DB `agentive` est déjà créée par POSTGRES_DB=agentive env var du container
-- On ajuste simplement l'ownership
ALTER DATABASE agentive OWNER TO agentive_owner;

-- Grants de base : connexion + usage schema public
GRANT CONNECT ON DATABASE agentive TO agentive_app;
GRANT CONNECT ON DATABASE agentive TO agentive_audit_admin;

-- Se connecter à agentive pour les grants schema
\connect agentive

-- Extension pgvector (CREATE EXTENSION nécessite superuser — exécuté ici comme postgres)
CREATE EXTENSION IF NOT EXISTS vector;

-- Schema public : ownership + grants
ALTER SCHEMA public OWNER TO agentive_owner;
GRANT USAGE ON SCHEMA public TO agentive_app;
GRANT USAGE ON SCHEMA public TO agentive_audit_admin;

-- Default privileges : les prochaines tables créées par agentive_owner
-- seront accessibles à agentive_app (lecture/écriture) et audit_admin (lecture)
ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentive_app;

ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO agentive_app;

ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO agentive_audit_admin;

ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO agentive_audit_admin;

-- Note : les grants spécifiques sur audit_events (INSERT pour agentive_audit_admin,
-- REVOKE DELETE/UPDATE) sont appliqués dans la migration Alembic initiale,
-- après la création de la table et de ses partitions.

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Notes
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- - L'extension `vector` (pgvector) est créée dans la migration Alembic (pas ici)
-- - RLS est activée table par table dans la migration Alembic
-- - Les partitions `audit_events_YYYY_MM` sont créées automatiquement via trigger
--   ou via pg_partman (à décider en Sprint 1) — voir migration
