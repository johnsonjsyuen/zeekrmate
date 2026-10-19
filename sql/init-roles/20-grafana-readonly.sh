#!/usr/bin/env bash
# Create (or update) the SELECT-only database role that Grafana uses.
#
# Runs automatically from /docker-entrypoint-initdb.d on the FIRST start of an
# empty database volume, after 10-schema.sql. To apply it to a database that
# already exists (or after changing GRAFANA_DB_PASSWORD in .env), run:
#
#   docker compose up -d db
#   docker compose exec db bash /docker-entrypoint-initdb.d/20-grafana-readonly.sh
#
# Inputs (environment of the db container, set from .env by docker-compose.yml):
#   POSTGRES_USER, POSTGRES_DB   owner role and database (used by the logger)
#   GRAFANA_DB_USER              read-only role name (default grafana_ro)
#   GRAFANA_DB_PASSWORD          its password (required)
#
# Safe to run more than once.
set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is not set}"
: "${POSTGRES_DB:?POSTGRES_DB is not set}"
: "${GRAFANA_DB_PASSWORD:?GRAFANA_DB_PASSWORD is not set (see .env.example)}"
GRAFANA_DB_USER="${GRAFANA_DB_USER:-grafana_ro}"

if [ "$GRAFANA_DB_USER" = "$POSTGRES_USER" ]; then
  echo "GRAFANA_DB_USER must differ from POSTGRES_USER" >&2
  exit 1
fi

# Values are passed as psql variables and quoted by psql (:'x' / :"x"),
# so passwords with quotes or spaces are handled safely.
psql -v ON_ERROR_STOP=1 --no-psqlrc --no-password \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v ro_user="$GRAFANA_DB_USER" \
  -v ro_pw="$GRAFANA_DB_PASSWORD" \
  -v owner="$POSTGRES_USER" \
  -v db="$POSTGRES_DB" <<'SQL'
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ro_user') AS ro_missing \gset
\if :ro_missing
CREATE ROLE :"ro_user" LOGIN PASSWORD :'ro_pw';
\else
ALTER ROLE :"ro_user" LOGIN PASSWORD :'ro_pw';
\endif
ALTER ROLE :"ro_user" NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE :"ro_user" SET default_transaction_read_only = on;
ALTER ROLE :"ro_user" SET statement_timeout = '60s';

GRANT CONNECT ON DATABASE :"db" TO :"ro_user";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"ro_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"ro_user";
-- Tables the logger creates later (schema changes) become readable too.
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA public
  GRANT SELECT ON TABLES TO :"ro_user";

-- No temporary tables/functions for non-superusers (PUBLIC has TEMP by
-- default; the read-only role could use it after turning read-only off).
REVOKE TEMP ON DATABASE :"db" FROM PUBLIC;
-- No advisory locks for non-superusers: a Grafana user could otherwise hold
-- the logger's schema lock (db.SCHEMA_LOCK_ID) and block its start-up. The
-- logger connects as the superuser POSTGRES_USER and is not affected; a
-- future non-superuser logger role needs GRANT EXECUTE on these.
DO $$
DECLARE f regprocedure;
BEGIN
  FOR f IN SELECT p.oid::regprocedure FROM pg_proc p
           JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'pg_catalog' AND p.proname ~ '^pg_(try_)?advisory_(xact_)?lock(_shared)?$'
  LOOP
    EXECUTE format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', f);
  END LOOP;
END
$$;
-- The read-only role only needs this database.
SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'postgres') AS has_postgres_db \gset
\if :has_postgres_db
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
\endif
SQL

echo "Read-only role '$GRAFANA_DB_USER' is ready on database '$POSTGRES_DB'."
