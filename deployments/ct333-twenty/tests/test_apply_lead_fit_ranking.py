from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from typing import Any


MODULE_PATH = pathlib.Path(__file__).parents[1] / "apply_lead_fit_ranking.py"
SPEC = importlib.util.spec_from_file_location("apply_lead_fit_ranking", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
ranking = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ranking
SPEC.loader.exec_module(ranking)


FIRST_SOURCE_KEY = "discovered:10000000-0000-0000-0000-000000000001"
SECOND_SOURCE_KEY = "official:10000000-0000-0000-0000-000000000002"
FIRST_RECORD_ID = "20000000-0000-0000-0000-000000000001"
SECOND_RECORD_ID = "20000000-0000-0000-0000-000000000002"
MANIFEST_SCORED_AT = "2026-07-29T20:32:27.299371Z"
SCORED_AT = "2026-07-29T20:32:27.299Z"
MODEL_VERSION = "client-fit-v1"


def _csv_bytes(*, duplicate_source: bool = False) -> bytes:
    second_source = FIRST_SOURCE_KEY if duplicate_source else SECOND_SOURCE_KEY
    return (
        "source_lead_key,twenty_record_id,rank,priority,"
        "diversified_review_rank,fit_score,score_confidence,why_it_fits\n"
        f"{FIRST_SOURCE_KEY},{FIRST_RECORD_ID},1,A,1.0,89.7,HIGH,"
        '"Strong after-hours service pattern"\n'
        f"{second_source},{SECOND_RECORD_ID},2,B,,78.5,MEDIUM,"
        '"Similar retained-client pattern"\n'
    ).encode()


def _manifest(content: bytes) -> dict[str, Any]:
    return {
        "version": 1,
        "modelVersion": MODEL_VERSION,
        "scoredAt": MANIFEST_SCORED_AT,
        "sourceSha256": hashlib.sha256(content).hexdigest(),
        "rowCount": 2,
        "reviewCount": 1,
        "priorityCounts": {"A": 1, "B": 1},
    }


def _rows() -> list[Any]:
    return [
        ranking.RankingRow(
            source_lead_key=FIRST_SOURCE_KEY,
            twenty_record_id=FIRST_RECORD_ID,
            rank=1,
            priority="A",
            review_rank=1,
            fit_score=89.7,
            confidence="HIGH",
            reason="Strong after-hours service pattern",
        ),
        ranking.RankingRow(
            source_lead_key=SECOND_SOURCE_KEY,
            twenty_record_id=SECOND_RECORD_ID,
            rank=2,
            priority="B",
            review_rank=None,
            fit_score=78.5,
            confidence="MEDIUM",
            reason="Similar retained-client pattern",
        ),
    ]


def _live_companies() -> list[dict[str, Any]]:
    return [
        {"id": FIRST_RECORD_ID, "sourceLeadKey": FIRST_SOURCE_KEY},
        {"id": SECOND_RECORD_ID, "sourceLeadKey": SECOND_SOURCE_KEY},
    ]


class FakeClient:
    def __init__(self, companies: list[dict[str, Any]] | None = None) -> None:
        self.live_companies = companies or _live_companies()
        self.fields_validated = False
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.fail_after: int | None = None

    def validate_fit_fields(self) -> None:
        self.fields_validated = True

    def companies(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self.live_companies)

    def update_company(self, record_id: str, payload: dict[str, Any]) -> None:
        if self.fail_after is not None and len(self.writes) >= self.fail_after:
            raise ranking.RankingError("simulated write failure")
        if set(payload) != ranking.FIT_PAYLOAD_FIELDS:
            raise AssertionError("payload escaped fit-field allowlist")
        self.writes.append((record_id, copy.deepcopy(payload)))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RankingInputTests(unittest.TestCase):
    def _files(
        self, content: bytes, manifest: dict[str, Any]
    ) -> tuple[tempfile.TemporaryDirectory[str], pathlib.Path, pathlib.Path]:
        temp = tempfile.TemporaryDirectory()
        directory = pathlib.Path(temp.name)
        csv_path = directory / "ranking.csv"
        manifest_path = directory / "manifest.json"
        csv_path.write_bytes(content)
        manifest_path.write_text(json.dumps(manifest))
        return temp, csv_path, manifest_path

    def test_reviewed_csv_parses_with_integer_float_review_ranks(self) -> None:
        content = _csv_bytes()
        temp, csv_path, manifest_path = self._files(content, _manifest(content))
        self.addCleanup(temp.cleanup)

        manifest = ranking._read_manifest(manifest_path)
        rows = ranking._read_ranking(csv_path, manifest)

        self.assertEqual([row.rank for row in rows], [1, 2])
        self.assertEqual([row.review_rank for row in rows], [1, None])
        self.assertEqual(manifest["scoredAt"], SCORED_AT)

    def test_scored_at_is_normalized_to_twenty_millisecond_precision(self) -> None:
        self.assertEqual(ranking._iso_datetime(MANIFEST_SCORED_AT), SCORED_AT)
        self.assertEqual(ranking._iso_datetime(SCORED_AT), SCORED_AT)

    def test_committed_snapshot_matches_its_reviewed_manifest(self) -> None:
        manifest = ranking._read_manifest(ranking.DEFAULT_MANIFEST_PATH)
        rows = ranking._read_ranking(ranking.DEFAULT_RANKING_PATH, manifest)

        self.assertEqual(len(rows), 1643)
        self.assertEqual(sum(row.review_rank is not None for row in rows), 50)
        self.assertEqual(
            {row.priority for row in rows},
            {"A", "B", "C", "NURTURE"},
        )

    def test_hash_mismatch_fails_before_the_csv_is_accepted(self) -> None:
        content = _csv_bytes()
        manifest = _manifest(content)
        manifest["sourceSha256"] = "0" * 64
        temp, csv_path, manifest_path = self._files(content, manifest)
        self.addCleanup(temp.cleanup)

        with self.assertRaisesRegex(ranking.RankingError, "does not match"):
            ranking._read_ranking(csv_path, ranking._read_manifest(manifest_path))

    def test_duplicate_source_keys_are_rejected(self) -> None:
        content = _csv_bytes(duplicate_source=True)
        temp, csv_path, manifest_path = self._files(content, _manifest(content))
        self.addCleanup(temp.cleanup)

        with self.assertRaisesRegex(ranking.RankingError, "duplicate source"):
            ranking._read_ranking(csv_path, ranking._read_manifest(manifest_path))

    def test_priority_counts_must_match_the_reviewed_manifest(self) -> None:
        content = _csv_bytes()
        manifest = _manifest(content)
        manifest["priorityCounts"] = {"A": 2}
        temp, csv_path, manifest_path = self._files(content, manifest)
        self.addCleanup(temp.cleanup)

        with self.assertRaisesRegex(ranking.RankingError, "priority counts"):
            ranking._read_ranking(csv_path, ranking._read_manifest(manifest_path))

    def test_unreviewed_columns_are_rejected(self) -> None:
        content = _csv_bytes().replace(
            b"why_it_fits\n",
            b"why_it_fits,leadEmail\n",
        ).replace(
            b'"Strong after-hours service pattern"\n',
            b'"Strong after-hours service pattern",person@example.test\n',
        ).replace(
            b'"Similar retained-client pattern"\n',
            b'"Similar retained-client pattern",other@example.test\n',
        )
        temp, csv_path, manifest_path = self._files(content, _manifest(content))
        self.addCleanup(temp.cleanup)

        with self.assertRaisesRegex(ranking.RankingError, "unreviewed columns"):
            ranking._read_ranking(csv_path, ranking._read_manifest(manifest_path))


class RankingApplyTests(unittest.TestCase):
    manifest = {
        "modelVersion": MODEL_VERSION,
        "scoredAt": SCORED_AT,
        "sourceSha256": "reviewed-hash",
    }

    def test_dry_run_validates_every_record_without_writes(self) -> None:
        client = FakeClient()

        result = ranking.apply_ranking(
            client,
            rows=_rows(),
            manifest=self.manifest,
            apply=False,
        )

        self.assertTrue(client.fields_validated)
        self.assertEqual(client.writes, [])
        self.assertEqual(result["changeCount"], 2)
        self.assertEqual(result["reviewQueueRows"], 1)
        self.assertEqual(result["liveCompanies"], 2)

    def test_apply_only_writes_fit_fields_and_marks_the_review_slate(self) -> None:
        client = FakeClient()

        result = ranking.apply_ranking(
            client,
            rows=_rows(),
            manifest=self.manifest,
            apply=True,
        )

        self.assertEqual(result["appliedCount"], 2)
        self.assertEqual([record_id for record_id, _ in client.writes], [
            FIRST_RECORD_ID,
            SECOND_RECORD_ID,
        ])
        first_payload = client.writes[0][1]
        second_payload = client.writes[1][1]
        self.assertEqual(set(first_payload), ranking.FIT_PAYLOAD_FIELDS)
        self.assertTrue(first_payload["leadReviewQueue"])
        self.assertEqual(first_payload["leadReviewRank"], 1)
        self.assertFalse(second_payload["leadReviewQueue"])
        self.assertIsNone(second_payload["leadReviewRank"])
        self.assertNotIn("salesActioned", first_payload)
        self.assertNotIn("salesLifecycleStatus", first_payload)
        self.assertNotIn("callStatus", first_payload)

    def test_population_drift_fails_before_any_write(self) -> None:
        client = FakeClient(companies=[_live_companies()[0]])

        with self.assertRaisesRegex(ranking.RankingError, "exactly match"):
            ranking.apply_ranking(
                client,
                rows=_rows(),
                manifest=self.manifest,
                apply=True,
            )

        self.assertEqual(client.writes, [])

    def test_record_id_mismatch_fails_before_any_write(self) -> None:
        companies = _live_companies()
        companies[0]["id"] = "20000000-0000-0000-0000-000000000099"
        client = FakeClient(companies=companies)

        with self.assertRaisesRegex(ranking.RankingError, "id mismatch"):
            ranking.apply_ranking(
                client,
                rows=_rows(),
                manifest=self.manifest,
                apply=True,
            )

        self.assertEqual(client.writes, [])

    def test_an_already_applied_snapshot_is_idempotent(self) -> None:
        rows = _rows()
        companies = _live_companies()
        for company, row in zip(companies, rows, strict=True):
            company.update(
                row.payload(model_version=MODEL_VERSION, scored_at=SCORED_AT)
            )
            company["leadFitReasonMarkdown"] = company.pop("leadFitReason")["markdown"]
        client = FakeClient(companies=companies)

        result = ranking.apply_ranking(
            client,
            rows=rows,
            manifest=self.manifest,
            apply=True,
        )

        self.assertEqual(result["changeCount"], 0)
        self.assertEqual(result["unchangedCount"], 2)
        self.assertEqual(client.writes, [])

    def test_a_failed_patch_stops_and_reports_progress(self) -> None:
        client = FakeClient()
        client.fail_after = 1

        with self.assertRaisesRegex(ranking.RankingError, "after 1 successful writes"):
            ranking.apply_ranking(
                client,
                rows=_rows(),
                manifest=self.manifest,
                apply=True,
            )

        self.assertEqual(len(client.writes), 1)

    def test_non_fit_payload_is_rejected_by_the_real_client(self) -> None:
        client = ranking.TwentyClient("http://twenty.test", "test-key")

        with self.assertRaisesRegex(ranking.RankingError, "allowlist"):
            client.update_company(FIRST_RECORD_ID, {"salesActioned": True})

    def test_real_client_waits_before_exceeding_its_request_budget(self) -> None:
        clock = FakeClock()
        client = ranking.TwentyClient(
            "http://twenty.test",
            "test-key",
            requests_per_window=2,
            window_seconds=10,
            clock=clock.monotonic,
            sleeper=clock.sleep,
        )

        client._wait_for_rate_slot()
        client._wait_for_rate_slot()
        client._wait_for_rate_slot()

        self.assertEqual(clock.sleeps, [10])
        self.assertEqual(list(client.request_times), [10])


if __name__ == "__main__":
    unittest.main()
