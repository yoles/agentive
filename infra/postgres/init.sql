-- Agentive — PostgreSQL init.sql (exécuté au premier démarrage du container db)
-- Crée les 3 rôles applicatifs + extension vector + grants scopés.
--
-- Passwords injectés via psql `-v` depuis les variables d'environnement
-- exposées par docker-entrypoint.sh. PAS de backtick shell (leak risk +
-- break sur special chars). Pas de `log_statement` logué ici car l'image
-- Postgres applique `postgresql.conf` APRÈS ce script.
--
-- L'entrypoint standard `docker-entrypoint.sh` appelle psql avec :
--   POSTGRES_OWNER_PASSWORD, POSTGRES_APP_PASSWORD, POSTGRES_AUDIT_ADMIN_PASSWORD
-- disponibles dans l'env psql via ENV_POSTGRES_xxx → accessibles via
-- `current_setting('xxx')` n'est pas viable. On utilise psql `\getenv` (PG 17+)
-- pour importer les vars proprement.

\set ON_ERROR_STOP on

-- Import passwords from env vars (PG 17+ `\getenv` directive).
-- docker-entrypoint.sh propagates POSTGRES_*_PASSWORD into psql's environment.
-- Presence/non-sentinel validation is enforced upstream :
--   - docker-compose.yml utilise `:?required` pour refuser une var manquante,
--   - Pydantic Settings (shared/config.py) refuse les valeurs `change_me` en
--     production.
-- Si une valeur vide arrive ici malgré ces gates, `CREATE ROLE ... PASSWORD ''`
-- créera un rôle sans password — ce qui échouera à la première connection app
-- (pg_hba rejette les passwords vides par défaut).
\getenv owner_password POSTGRES_OWNER_PASSWORD
\getenv app_password POSTGRES_APP_PASSWORD
\getenv audit_admin_password POSTGRES_AUDIT_ADMIN_PASSWORD

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Rôles (principe du moindre privilège)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- Owner : propriétaire des objets, utilisé par Alembic pour les migrations.
-- PAS de CREATEDB : n'a besoin que de ALTER/CREATE/DROP sur la DB `agentive`.
CREATE ROLE agentive_owner WITH LOGIN PASSWORD :'owner_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- App runtime : utilisé par FastAPI. SELECT/INSERT/UPDATE/DELETE sur tables
-- métier uniquement (pas d'écriture sur audit_events — grants scopés ci-dessous).
CREATE ROLE agentive_app WITH LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- Audit admin : INSERT-only sur audit_events + SELECT sur toutes les partitions.
-- Grants explicites dans la migration Alembic (après création des tables).
CREATE ROLE agentive_audit_admin WITH LOGIN PASSWORD :'audit_admin_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Base de données + ownership + extension pgvector
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- La DB `agentive` est déjà créée par POSTGRES_DB=agentive.
-- ALTER OWNERSHIP AVANT de se connecter — sinon tout objet créé dans public
-- (notamment l'extension vector ci-dessous) appartient à postgres (superuser)
-- et ne peut pas être manipulé par agentive_owner (migrations Alembic).
ALTER DATABASE agentive OWNER TO agentive_owner;

GRANT CONNECT ON DATABASE agentive TO agentive_app;
GRANT CONNECT ON DATABASE agentive TO agentive_audit_admin;

\connect agentive

-- Transférer ownership du schema public AVANT création extension
-- → l'extension sera owned par agentive_owner, pas par postgres.
ALTER SCHEMA public OWNER TO agentive_owner;

-- Extension pgvector (CREATE EXTENSION nécessite superuser, c'est bien postgres
-- qui exécute ce script). L'extension elle-même reste attachée à la DB mais
-- ses objets (opérateurs, types) sont partagés.
CREATE EXTENSION IF NOT EXISTS vector;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Schema + grants de base
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GRANT USAGE ON SCHEMA public TO agentive_app;
GRANT USAGE ON SCHEMA public TO agentive_audit_admin;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Default privileges — ATTENTION : ne PAS donner DELETE/UPDATE globalement à
-- agentive_app car les tables audit futures (partitions auto) hériteraient.
-- On accorde SELECT/INSERT/UPDATE/DELETE table-par-table dans les migrations,
-- et on REVOKE explicitement sur audit_events.
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- Seul default privilege ici : SELECT sur séquences (pour nextval de PK).
ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO agentive_app;

ALTER DEFAULT PRIVILEGES FOR ROLE agentive_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO agentive_audit_admin;

-- Les grants CRUD par table sont appliqués dans la migration Alembic (qui sait
-- quelle table accepte quels verbs — CRUD sur métier, INSERT-only sur audit).
