#!/usr/bin/env python3
"""Expose aggregate, PII-free sales activity from Twenty CRM."""

from __future__ import annotations

import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg import sql


LISTEN_HOST = os.getenv("CRM_METRICS_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("CRM_METRICS_LISTEN_PORT", "9105"))
TIME_ZONE = "America/Chicago"
SAFE_STATUS = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


WINDOW_QUERY = """
WITH clock AS (
  SELECT (CURRENT_TIMESTAMP AT TIME ZONE %(time_zone)s)::date AS today
), windows(window_name, start_date, end_date) AS (
  SELECT 'today', today, today FROM clock
  UNION ALL SELECT 'last_7_days', today - 6, today FROM clock
  UNION ALL SELECT 'last_30_days', today - 29, today FROM clock
)
SELECT windows.window_name,
       count(company.id)::bigint
FROM windows
LEFT JOIN {company} AS company
  ON company."deletedAt" IS NULL
 AND company."lastCalledAt" IS NOT NULL
 AND (company."lastCalledAt" AT TIME ZONE %(time_zone)s)::date
     BETWEEN windows.start_date AND windows.end_date
GROUP BY windows.window_name
ORDER BY windows.window_name
"""

STATUS_QUERY = """
WITH clock AS (
  SELECT (CURRENT_TIMESTAMP AT TIME ZONE %(time_zone)s)::date AS today
), windows(window_name, start_date, end_date) AS (
  SELECT 'today', today, today FROM clock
  UNION ALL SELECT 'last_7_days', today - 6, today FROM clock
  UNION ALL SELECT 'last_30_days', today - 29, today FROM clock
)
SELECT windows.window_name,
       coalesce(company."callStatus"::text, 'UNKNOWN') AS call_status,
       count(company.id)::bigint
FROM windows
JOIN {company} AS company
  ON company."deletedAt" IS NULL
 AND company."lastCalledAt" IS NOT NULL
 AND (company."lastCalledAt" AT TIME ZONE %(time_zone)s)::date
     BETWEEN windows.start_date AND windows.end_date
GROUP BY windows.window_name, call_status
ORDER BY windows.window_name, call_status
"""

DAILY_QUERY = """
WITH clock AS (
  SELECT (CURRENT_TIMESTAMP AT TIME ZONE %(time_zone)s)::date AS today
), days AS (
  SELECT generate_series(today - 30, today, interval '1 day')::date AS day
  FROM clock
)
SELECT days.day::text,
       count(company.id)::bigint
FROM days
LEFT JOIN {company} AS company
  ON company."deletedAt" IS NULL
 AND company."lastCalledAt" IS NOT NULL
 AND (company."lastCalledAt" AT TIME ZONE %(time_zone)s)::date = days.day
GROUP BY days.day
ORDER BY days.day
"""

FRESHNESS_QUERY = """
SELECT count(*)::bigint,
       coalesce(extract(epoch FROM max("lastCalledAt")), 0)::double precision
FROM {company}
WHERE "deletedAt" IS NULL AND "lastCalledAt" IS NOT NULL
"""


def discover_workspace_schema(cursor: psycopg.Cursor) -> str:
    cursor.execute(
        """
        SELECT table_schema
        FROM information_schema.columns
        WHERE table_name = 'company'
          AND column_name IN ('id', 'deletedAt', 'lastCalledAt', 'callStatus')
          AND table_schema LIKE 'workspace_%'
        GROUP BY table_schema
        HAVING count(DISTINCT column_name) = 4
        ORDER BY table_schema
        """
    )
    schemas = [row[0] for row in cursor.fetchall()]
    if len(schemas) != 1:
        raise RuntimeError(f"expected one readable Twenty workspace schema, found {len(schemas)}")
    return schemas[0]


def connect_database() -> psycopg.Connection:
    """Connect from libpq environment variables injected into the container."""
    return psycopg.connect("")


def collect_metrics() -> str:
    with connect_database() as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            with connection.cursor() as cursor:
                schema = discover_workspace_schema(cursor)
                company = sql.Identifier(schema, "company")
                cursor.execute(sql.SQL(WINDOW_QUERY).format(company=company), {"time_zone": TIME_ZONE})
                windows = cursor.fetchall()
                cursor.execute(sql.SQL(STATUS_QUERY).format(company=company), {"time_zone": TIME_ZONE})
                statuses = cursor.fetchall()
                cursor.execute(sql.SQL(DAILY_QUERY).format(company=company), {"time_zone": TIME_ZONE})
                daily = cursor.fetchall()
                cursor.execute(sql.SQL(FRESHNESS_QUERY).format(company=company))
                history_total, latest_timestamp = cursor.fetchone()

    return render_metrics(
        windows,
        statuses,
        daily,
        history_total,
        latest_timestamp,
        snapshot_timestamp=time.time(),
    )


def render_metrics(
    windows: list[tuple[str, int]],
    statuses: list[tuple[str, str, int]],
    daily: list[tuple[str, int]],
    history_total: int,
    latest_timestamp: float,
    *,
    snapshot_timestamp: float,
) -> str:
    lines = [
        "# HELP twenty_crm_companies_called_window Active CRM companies whose latest Last Called At is in the Central calendar window.",
        "# TYPE twenty_crm_companies_called_window gauge",
    ]
    for window, value in windows:
        lines.append(f'twenty_crm_companies_called_window{{window="{window}"}} {int(value)}')

    lines.extend(
        [
            "# HELP twenty_crm_companies_called_status_window Active CRM companies grouped by their current call status and latest Last Called At window.",
            "# TYPE twenty_crm_companies_called_status_window gauge",
        ]
    )
    bounded_statuses: dict[tuple[str, str], int] = {}
    for window, status, value in statuses:
        bounded_status = status if SAFE_STATUS.fullmatch(status) else "OTHER"
        key = (window, bounded_status)
        bounded_statuses[key] = bounded_statuses.get(key, 0) + int(value)
    for (window, bounded_status), value in sorted(bounded_statuses.items()):
        lines.append(
            f'twenty_crm_companies_called_status_window{{window="{window}",status="{bounded_status}"}} {value}'
        )

    lines.extend(
        [
            "# HELP twenty_crm_companies_called_daily Active CRM companies whose latest Last Called At falls on the Central calendar date.",
            "# TYPE twenty_crm_companies_called_daily gauge",
        ]
    )
    for date, value in daily:
        lines.append(f'twenty_crm_companies_called_daily{{date="{date}"}} {int(value)}')

    lines.extend(
        [
            "# HELP twenty_crm_companies_with_call_history_total Active CRM companies with any Last Called At value.",
            "# TYPE twenty_crm_companies_with_call_history_total gauge",
            f"twenty_crm_companies_with_call_history_total {int(history_total)}",
            "# HELP twenty_crm_source_latest_last_called_at_timestamp_seconds Latest active CRM Last Called At timestamp.",
            "# TYPE twenty_crm_source_latest_last_called_at_timestamp_seconds gauge",
            f"twenty_crm_source_latest_last_called_at_timestamp_seconds {float(latest_timestamp):.3f}",
            "# HELP twenty_crm_metrics_snapshot_timestamp_seconds Time when this aggregate snapshot was generated.",
            "# TYPE twenty_crm_metrics_snapshot_timestamp_seconds gauge",
            f"twenty_crm_metrics_snapshot_timestamp_seconds {snapshot_timestamp:.3f}",
        ]
    )
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            try:
                with connect_database() as connection:
                    with connection.transaction():
                        connection.execute("SET TRANSACTION READ ONLY")
                        with connection.cursor() as cursor:
                            discover_workspace_schema(cursor)
            except Exception:
                self.send_error(503, "CRM metrics source unavailable")
                return
            self._respond(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if self.path == "/metrics":
            try:
                payload = collect_metrics().encode("utf-8")
            except Exception:
                self.send_error(503, "CRM metrics snapshot unavailable")
                return
            self._respond(200, "text/plain; version=0.0.4; charset=utf-8", payload)
            return
        self.send_error(404)

    def _respond(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
