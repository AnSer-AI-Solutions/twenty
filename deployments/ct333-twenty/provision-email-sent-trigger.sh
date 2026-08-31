#!/usr/bin/env bash
set -euo pipefail

readonly compose_dir=/opt/twenty/deployments/ct333-twenty

cd "$compose_dir"
docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U postgres -d default <<'SQL'
BEGIN;

SELECT n.nspname AS workspace_schema
FROM pg_namespace n
JOIN pg_class c
  ON c.relnamespace = n.oid
 AND c.relname = 'company'
 AND c.relkind IN ('r', 'p')
JOIN pg_attribute email_sent
  ON email_sent.attrelid = c.oid
 AND email_sent.attname = 'emailSent'
 AND NOT email_sent.attisdropped
JOIN pg_attribute email_sent_date
  ON email_sent_date.attrelid = c.oid
 AND email_sent_date.attname = 'emailSentDate'
 AND NOT email_sent_date.attisdropped
WHERE n.nspname LIKE 'workspace_%'
ORDER BY n.nspname
\gset

SELECT format($ddl$
CREATE OR REPLACE FUNCTION %I.stamp_company_email_sent_date()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
  IF NEW."emailSent" IS TRUE
     AND NEW."emailSentDate" IS NULL
     AND (TG_OP = 'INSERT' OR OLD."emailSent" IS DISTINCT FROM TRUE) THEN
    NEW."emailSentDate" := (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago')::date;
  END IF;

  RETURN NEW;
END;
$function$
$ddl$, :'workspace_schema') \gexec

SELECT format(
  'DROP TRIGGER IF EXISTS stamp_company_email_sent_date ON %I.company',
  :'workspace_schema'
) \gexec

SELECT format($ddl$
CREATE TRIGGER stamp_company_email_sent_date
BEFORE INSERT OR UPDATE OF "emailSent"
ON %I.company
FOR EACH ROW
EXECUTE FUNCTION %I.stamp_company_email_sent_date()
$ddl$, :'workspace_schema', :'workspace_schema') \gexec

-- Reconcile any row left between the old poller's last pass and this trigger.
SELECT format($sql$
UPDATE %I.company
SET "emailSentDate" = (CURRENT_TIMESTAMP AT TIME ZONE 'America/Chicago')::date
WHERE "emailSent" IS TRUE
  AND "emailSentDate" IS NULL
$sql$, :'workspace_schema') \gexec

COMMIT;
SQL
