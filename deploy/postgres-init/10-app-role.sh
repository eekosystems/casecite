#!/bin/sh
# Creates the non-superuser role the application connects as.
#
# PostgreSQL exempts superusers from row-level security, and the image's
# POSTGRES_USER is a superuser. docker-compose.prod.yml mounts this script into
# /docker-entrypoint-initdb.d/, so it runs ONCE, when the data volume is first
# initialised. It creates APP_DB_USER (default casecite_app) with
# APP_DB_PASSWORD, owning the database and the public schema, so that Alembic
# (which runs as the app role) creates and owns every table and the FORCED
# policies apply to the app. The superuser stays for backups and administration.
#
# Existing installs (initdb already ran) are not touched by this script: see
# docs/deployment.md, "Row-level security needs a non-superuser app role".
set -eu

APP_DB_USER="${APP_DB_USER:-}"
if [ -z "$APP_DB_USER" ]; then
  echo "APP_DB_USER not set: the app will connect as the superuser ${POSTGRES_USER}; row-level security will NOT be enforced (docs/deployment.md)." >&2
  exit 0
fi
APP_DB_PASSWORD="${APP_DB_PASSWORD:?APP_DB_PASSWORD is required when APP_DB_USER is set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=db="$POSTGRES_DB" --set=app_user="$APP_DB_USER" --set=app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec
GRANT CONNECT ON DATABASE :"db" TO :"app_user";
ALTER DATABASE :"db" OWNER TO :"app_user";
ALTER SCHEMA public OWNER TO :"app_user";
SQL

echo "Application role ${APP_DB_USER} created and owns database ${POSTGRES_DB}."
