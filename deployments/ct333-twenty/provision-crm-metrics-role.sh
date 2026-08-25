#!/usr/bin/env bash
set -euo pipefail

: "${CRM_METRICS_DATABASE_PASSWORD:?missing CRM_METRICS_DATABASE_PASSWORD}"

readonly compose_dir=/opt/twenty/deployments/ct333-twenty

cd "$compose_dir"
docker compose exec -T \
  -e CRM_METRICS_DATABASE_PASSWORD \
  db sh -eu <<'SH'
psql -X -v ON_ERROR_STOP=1 -U postgres -d default <<'SQL'
\getenv metrics_password CRM_METRICS_DATABASE_PASSWORD

DO $role$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'twenty_crm_metrics') THEN
    CREATE ROLE twenty_crm_metrics LOGIN;
  END IF;
END
$role$;

SELECT format('ALTER ROLE twenty_crm_metrics PASSWORD %L', :'metrics_password') \gexec
ALTER ROLE twenty_crm_metrics SET default_transaction_read_only = on;
ALTER ROLE twenty_crm_metrics SET statement_timeout = '5s';

SELECT n.nspname AS workspace_schema
FROM pg_namespace n
JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = 'company'
WHERE n.nspname LIKE 'workspace_%'
ORDER BY n.nspname
\gset

GRANT CONNECT ON DATABASE "default" TO twenty_crm_metrics;
GRANT USAGE ON SCHEMA :"workspace_schema" TO twenty_crm_metrics;
GRANT SELECT ON TABLE :"workspace_schema".company TO twenty_crm_metrics;
SQL
SH
