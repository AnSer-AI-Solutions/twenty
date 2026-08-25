from __future__ import annotations

import importlib.util
import pathlib
import unittest


MODULE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "crm-metrics-exporter"
    / "crm_metrics_exporter.py"
)
SPEC = importlib.util.spec_from_file_location("crm_metrics_exporter", MODULE_PATH)
assert SPEC and SPEC.loader
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)


class FakeCursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self.rows = rows
        self.query = ""

    def execute(self, query: str) -> None:
        self.query = query

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


class WorkspaceDiscoveryTest(unittest.TestCase):
    def test_accepts_exactly_one_readable_company_schema(self) -> None:
        cursor = FakeCursor([("workspace_expected",)])

        self.assertEqual(EXPORTER.discover_workspace_schema(cursor), "workspace_expected")
        self.assertIn("lastCalledAt", cursor.query)
        self.assertIn("callStatus", cursor.query)

    def test_fails_closed_on_zero_or_multiple_workspaces(self) -> None:
        for rows in ([], [("workspace_a",), ("workspace_b",)]):
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                EXPORTER.discover_workspace_schema(FakeCursor(rows))


class PrometheusRenderingTest(unittest.TestCase):
    def test_renders_only_aggregate_bounded_dimensions(self) -> None:
        rendered = EXPORTER.render_metrics(
            [("today", 3), ("last_7_days", 8), ("last_30_days", 12)],
            [
                ("today", "CONNECTED", 2),
                ("today", "unsafe phone 3125550199", 1),
                ("today", "another unsafe value", 2),
            ],
            [("2026-08-24", 1), ("2026-08-25", 3)],
            12,
            1_777_000_000.25,
            snapshot_timestamp=1_777_000_010.5,
        )

        self.assertIn('window="today"} 3', rendered)
        self.assertIn('status="CONNECTED"} 2', rendered)
        self.assertIn('status="OTHER"} 3', rendered)
        self.assertEqual(rendered.count('status="OTHER"'), 1)
        self.assertNotIn("3125550199", rendered)
        self.assertIn('date="2026-08-25"} 3', rendered)
        self.assertIn("twenty_crm_companies_with_call_history_total 12", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_metric_help_states_latest_per_company_semantics(self) -> None:
        rendered = EXPORTER.render_metrics([], [], [], 0, 0, snapshot_timestamp=1)

        self.assertIn("latest Last Called At", rendered)


if __name__ == "__main__":
    unittest.main()
