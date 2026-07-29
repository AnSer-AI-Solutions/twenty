#!/usr/bin/env python3
"""Validate and apply a reviewed lead-fit ranking snapshot to CT333."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RANKING_PATH = SCRIPT_DIR / "rankings" / "cold-lead-ranking-v2.csv"
DEFAULT_MANIFEST_PATH = SCRIPT_DIR / "rankings" / "cold-lead-ranking-v2.manifest.json"
COMPANY_PAGE_LIMIT = 100
MAX_COMPANY_PAGES = 100
ALLOWED_METHODS = ("GET", "PATCH")
RETRYABLE_METHODS = ("GET",)
MAX_ATTEMPTS = 3
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0
PROGRESS_INTERVAL = 30

REQUIRED_COLUMNS = frozenset(
    {
        "source_lead_key",
        "twenty_record_id",
        "rank",
        "priority",
        "diversified_review_rank",
        "fit_score",
        "score_confidence",
        "why_it_fits",
    }
)
FIT_FIELD_TYPES = {
    "leadFitScore": "NUMBER",
    "leadFitRank": "NUMBER",
    "leadFitPriority": "SELECT",
    "leadFitConfidence": "SELECT",
    "leadFitReason": "RICH_TEXT",
    "leadFitModelVersion": "TEXT",
    "leadFitScoredAt": "DATE_TIME",
    "leadReviewQueue": "BOOLEAN",
    "leadReviewRank": "NUMBER",
}
FIT_PAYLOAD_FIELDS = frozenset(FIT_FIELD_TYPES)
PRIORITIES = frozenset({"A", "B", "C", "NURTURE"})
CONFIDENCE_LEVELS = frozenset({"HIGH", "MEDIUM", "LOW"})


class RankingError(RuntimeError):
    """Raised when a ranking cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class RankingRow:
    source_lead_key: str
    twenty_record_id: str
    rank: int
    priority: str
    review_rank: int | None
    fit_score: float
    confidence: str
    reason: str

    def payload(self, *, model_version: str, scored_at: str) -> dict[str, Any]:
        return {
            "leadFitScore": self.fit_score,
            "leadFitRank": self.rank,
            "leadFitPriority": self.priority,
            "leadFitConfidence": self.confidence,
            "leadFitReason": {"markdown": self.reason},
            "leadFitModelVersion": model_version,
            "leadFitScoredAt": scored_at,
            "leadReviewQueue": self.review_rank is not None,
            "leadReviewRank": self.review_rank,
        }


class TwentyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 20,
        *,
        requests_per_window: int = RATE_LIMIT_REQUESTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if not base_url:
            raise RankingError("TWENTY_API_URL is required")
        if not api_key:
            raise RankingError("TWENTY_API_KEY is required")
        if requests_per_window <= 0:
            raise RankingError("requests_per_window must be positive")
        if window_seconds <= 0:
            raise RankingError("window_seconds must be positive")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.requests_per_window = requests_per_window
        self.window_seconds = window_seconds
        self.clock = clock
        self.sleeper = sleeper
        self.next_request_at: float | None = None

    def _wait_for_rate_slot(self) -> None:
        interval = self.window_seconds / self.requests_per_window
        now = self.clock()
        if self.next_request_at is None:
            self.next_request_at = now
        while now < self.next_request_at:
            self.sleeper(self.next_request_at - now)
            now = self.clock()
        self.next_request_at = max(now, self.next_request_at) + interval

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        if method not in ALLOWED_METHODS:
            raise RankingError(f"{method} is not permitted by this importer")
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        attempts = MAX_ATTEMPTS if method in RETRYABLE_METHODS else 1
        for attempt in range(1, attempts + 1):
            self._wait_for_rate_slot()
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    if response.status not in expected:
                        raise RankingError(
                            f"Twenty returned unexpected HTTP {response.status}"
                        )
                    decoded = json.load(response)
                    if not isinstance(decoded, dict):
                        raise RankingError("Twenty returned a non-object JSON response")
                    return decoded
            except HTTPError as exc:
                detail = exc.read(4096).decode(errors="replace")
                if (exc.code == 429 or exc.code >= 500) and attempt < attempts:
                    time.sleep(attempt)
                    continue
                raise RankingError(
                    f"Twenty request failed with HTTP {exc.code}: {detail}"
                    + _write_outcome_guidance(method, path)
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < attempts:
                    time.sleep(attempt)
                    continue
                raise RankingError(
                    f"Twenty connection failed: {exc}"
                    + _write_outcome_guidance(method, path)
                ) from exc
        raise AssertionError("unreachable")

    def validate_fit_fields(self) -> None:
        response = self.request("GET", "/rest/metadata/objects?limit=200")
        company = next(
            (
                item
                for item in _items(response, "objects")
                if item.get("nameSingular") == "company"
            ),
            None,
        )
        if company is None:
            raise RankingError("Twenty Company object was not found")
        fields = {field.get("name"): field for field in company.get("fields", [])}
        missing = [name for name in FIT_FIELD_TYPES if name not in fields]
        if missing:
            raise RankingError(
                "Twenty is missing lead-fit fields; apply the reviewed metadata "
                f"configuration first: {', '.join(missing)}"
            )
        for name, expected_type in FIT_FIELD_TYPES.items():
            actual_type = fields[name].get("type")
            if actual_type != expected_type:
                raise RankingError(
                    f"Twenty field {name} has type {actual_type!r}, "
                    f"expected {expected_type!r}"
                )

    def companies(self) -> list[dict[str, Any]]:
        companies: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _ in range(MAX_COMPANY_PAGES):
            query: dict[str, str | int] = {"limit": COMPANY_PAGE_LIMIT}
            if cursor:
                query["starting_after"] = cursor
            response = self.request(
                "GET",
                "/rest/companies?" + urlencode(query),
            )
            data = response.get("data")
            if not isinstance(data, dict):
                raise RankingError("Twenty company response did not contain data")
            page = data.get("companies")
            if not isinstance(page, list) or not all(
                isinstance(item, dict) for item in page
            ):
                raise RankingError("Twenty company response did not contain companies")
            companies.extend(page)

            page_info = response.get("pageInfo")
            if page_info is None:
                page_info = data.get("pageInfo")
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                return companies
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise RankingError("Twenty company pagination did not advance")
            if next_cursor in seen_cursors:
                raise RankingError("Twenty company pagination repeated a cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise RankingError(
            f"Twenty company listing exceeded {MAX_COMPANY_PAGES} pages"
        )

    def update_company(self, record_id: str, payload: dict[str, Any]) -> None:
        if set(payload) != FIT_PAYLOAD_FIELDS:
            raise RankingError("refusing a payload outside the lead-fit field allowlist")
        self.request("PATCH", f"/rest/companies/{record_id}", payload)


def _write_outcome_guidance(method: str, path: str) -> str:
    if method == "GET":
        return ""
    return (
        f". The outcome of {method} {path} may be unknown; "
        "run a dry run before retrying"
    )


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        values = data.get(key, [])
        if isinstance(values, list):
            return [item for item in values if isinstance(item, dict)]
    return []


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RankingError(f"could not read ranking manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RankingError("ranking manifest must be a JSON object")
    required = {
        "version",
        "modelVersion",
        "scoredAt",
        "sourceSha256",
        "rowCount",
        "reviewCount",
        "priorityCounts",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise RankingError(f"ranking manifest is missing: {', '.join(missing)}")
    if manifest["version"] != 1:
        raise RankingError("unsupported ranking manifest version")
    if not isinstance(manifest["modelVersion"], str) or not manifest[
        "modelVersion"
    ].strip():
        raise RankingError("ranking manifest modelVersion must be non-empty")
    source_hash = manifest["sourceSha256"]
    if (
        not isinstance(source_hash, str)
        or len(source_hash) != 64
        or any(character not in "0123456789abcdef" for character in source_hash)
    ):
        raise RankingError("ranking manifest sourceSha256 must be lowercase SHA-256")
    if not isinstance(manifest["priorityCounts"], dict):
        raise RankingError("ranking manifest priorityCounts must be an object")
    manifest["scoredAt"] = _iso_datetime(str(manifest["scoredAt"]))
    return manifest


def _read_ranking(path: Path, manifest: dict[str, Any]) -> list[RankingRow]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RankingError(f"could not read ranking input {path}: {exc}") from exc
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != manifest["sourceSha256"]:
        raise RankingError(
            f"ranking SHA-256 {actual_hash} does not match reviewed manifest"
        )

    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames is None:
        raise RankingError("ranking CSV has no header")
    missing_columns = sorted(REQUIRED_COLUMNS - set(reader.fieldnames))
    if missing_columns:
        raise RankingError(
            f"ranking CSV is missing columns: {', '.join(missing_columns)}"
        )
    unexpected_columns = sorted(set(reader.fieldnames) - REQUIRED_COLUMNS)
    if unexpected_columns:
        raise RankingError(
            "ranking CSV contains unreviewed columns: "
            + ", ".join(unexpected_columns)
        )
    rows = [_parse_row(row, line_number=index) for index, row in enumerate(reader, 2)]
    _validate_rows(rows, manifest)
    return rows


def _parse_row(row: dict[str, str | None], *, line_number: int) -> RankingRow:
    source_key = (row.get("source_lead_key") or "").strip()
    source_parts = source_key.split(":", 1)
    if len(source_parts) != 2 or source_parts[0] not in {"official", "discovered"}:
        raise RankingError(f"line {line_number}: invalid source_lead_key")
    _uuid(source_parts[1], f"line {line_number} source_lead_key")
    record_id = _uuid(
        (row.get("twenty_record_id") or "").strip(),
        f"line {line_number} twenty_record_id",
    )
    rank = _integer(row.get("rank"), f"line {line_number} rank")
    priority = (row.get("priority") or "").strip().upper()
    if priority not in PRIORITIES:
        raise RankingError(f"line {line_number}: invalid priority {priority!r}")
    confidence = (row.get("score_confidence") or "").strip().upper()
    if confidence not in CONFIDENCE_LEVELS:
        raise RankingError(f"line {line_number}: invalid confidence {confidence!r}")
    score = _decimal(row.get("fit_score"), f"line {line_number} fit_score")
    if score < 0 or score > 100:
        raise RankingError(f"line {line_number}: fit_score must be within 0-100")
    review_value = (row.get("diversified_review_rank") or "").strip()
    review_rank = (
        _integer(review_value, f"line {line_number} diversified_review_rank")
        if review_value
        else None
    )
    reason = (row.get("why_it_fits") or "").strip()
    if not reason:
        raise RankingError(f"line {line_number}: why_it_fits is required")
    if len(reason) > 20_000:
        raise RankingError(f"line {line_number}: why_it_fits is too long")
    return RankingRow(
        source_lead_key=source_key,
        twenty_record_id=record_id,
        rank=rank,
        priority=priority,
        review_rank=review_rank,
        fit_score=float(score),
        confidence=confidence,
        reason=reason,
    )


def _validate_rows(rows: list[RankingRow], manifest: dict[str, Any]) -> None:
    expected_count = _positive_integer(manifest["rowCount"], "manifest rowCount")
    if len(rows) != expected_count:
        raise RankingError(
            f"ranking contains {len(rows)} rows, expected {expected_count}"
        )
    source_keys = [row.source_lead_key for row in rows]
    record_ids = [row.twenty_record_id for row in rows]
    if len(set(source_keys)) != len(rows):
        raise RankingError("ranking contains duplicate source_lead_key values")
    if len(set(record_ids)) != len(rows):
        raise RankingError("ranking contains duplicate twenty_record_id values")
    if [row.rank for row in rows] != list(range(1, len(rows) + 1)):
        raise RankingError("ranking ranks must be consecutive and ordered from 1")

    review_ranks = sorted(
        row.review_rank for row in rows if row.review_rank is not None
    )
    expected_review_count = _positive_integer(
        manifest["reviewCount"], "manifest reviewCount"
    )
    if review_ranks != list(range(1, expected_review_count + 1)):
        raise RankingError(
            "diversified review ranks must be consecutive and match reviewCount"
        )
    actual_priority_counts = dict(sorted(Counter(row.priority for row in rows).items()))
    expected_priority_counts = {
        str(key): int(value)
        for key, value in sorted(manifest["priorityCounts"].items())
    }
    if actual_priority_counts != expected_priority_counts:
        raise RankingError(
            f"priority counts {actual_priority_counts} do not match manifest "
            f"{expected_priority_counts}"
        )


def _uuid(value: str, description: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise RankingError(f"{description} is not a UUID") from exc


def _decimal(value: Any, description: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RankingError(f"{description} is not numeric") from exc
    if not parsed.is_finite():
        raise RankingError(f"{description} must be finite")
    return parsed


def _integer(value: Any, description: str) -> int:
    parsed = _decimal(value, description)
    integral = parsed.to_integral_value()
    if parsed != integral:
        raise RankingError(f"{description} must be an integer")
    return int(integral)


def _positive_integer(value: Any, description: str) -> int:
    parsed = _integer(value, description)
    if parsed <= 0:
        raise RankingError(f"{description} must be positive")
    return parsed


def _iso_datetime(value: str) -> str:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise RankingError("manifest scoredAt must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RankingError("manifest scoredAt must include a timezone")
    return (
        parsed.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _live_company_map(
    companies: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_source_key: dict[str, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for company in companies:
        source_key = company.get("sourceLeadKey")
        if not isinstance(source_key, str) or not source_key:
            raise RankingError("live Twenty company is missing sourceLeadKey")
        if source_key in by_source_key:
            raise RankingError(f"live Twenty has duplicate source key {source_key}")
        record_id = _uuid(str(company.get("id", "")), "live Twenty company id")
        if record_id in seen_ids:
            raise RankingError(f"live Twenty has duplicate company id {record_id}")
        seen_ids.add(record_id)
        normalized = dict(company)
        normalized["id"] = record_id
        by_source_key[source_key] = normalized
    return by_source_key


def _preflight_population(
    rows: list[RankingRow], companies: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    live_by_source_key = _live_company_map(companies)
    ranking_keys = {row.source_lead_key for row in rows}
    live_keys = set(live_by_source_key)
    if ranking_keys != live_keys:
        missing = len(live_keys - ranking_keys)
        stale = len(ranking_keys - live_keys)
        raise RankingError(
            "ranking must exactly match the live CRM population before any write "
            f"(missing live={missing}, stale input={stale})"
        )
    for row in rows:
        live_id = live_by_source_key[row.source_lead_key]["id"]
        if live_id != row.twenty_record_id:
            raise RankingError(
                f"Twenty id mismatch for {row.source_lead_key}; refusing to write"
            )
    return live_by_source_key


def _values_match(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    for field, target in desired.items():
        if field == "leadFitReason":
            value = current.get(field)
            current_markdown = (
                value.get("markdown")
                if isinstance(value, dict)
                else current.get("leadFitReasonMarkdown")
            )
            if current_markdown != target["markdown"]:
                return False
            continue
        if field == "leadFitScoredAt":
            value = current.get(field)
            if not isinstance(value, str):
                return False
            try:
                if _iso_datetime(value) != target:
                    return False
            except RankingError:
                return False
            continue
        value = current.get(field)
        if field in {"leadFitScore", "leadFitRank", "leadReviewRank"}:
            if value is None or target is None:
                if value is not target:
                    return False
            elif _decimal(value, field) != _decimal(target, field):
                return False
            continue
        if value != target:
            return False
    return True


def apply_ranking(
    client: TwentyClient,
    *,
    rows: list[RankingRow],
    manifest: dict[str, Any],
    apply: bool,
) -> dict[str, Any]:
    client.validate_fit_fields()
    companies = client.companies()
    live_by_source_key = _preflight_population(rows, companies)
    model_version = str(manifest["modelVersion"])
    scored_at = str(manifest["scoredAt"])
    pending: list[tuple[RankingRow, dict[str, Any]]] = []
    for row in rows:
        payload = row.payload(model_version=model_version, scored_at=scored_at)
        if not _values_match(live_by_source_key[row.source_lead_key], payload):
            pending.append((row, payload))

    applied = 0
    if apply:
        for row, payload in pending:
            try:
                client.update_company(row.twenty_record_id, payload)
            except RankingError as exc:
                raise RankingError(
                    f"ranking apply stopped after {applied} successful writes: {exc}"
                ) from exc
            applied += 1
            if len(pending) >= PROGRESS_INTERVAL and (
                applied % PROGRESS_INTERVAL == 0 or applied == len(pending)
            ):
                print(
                    f"progress applied={applied} remaining={len(pending) - applied}",
                    file=sys.stderr,
                    flush=True,
                )

    return {
        "mode": "apply" if apply else "dry-run",
        "modelVersion": model_version,
        "scoredAt": scored_at,
        "sourceSha256": manifest["sourceSha256"],
        "rankingRows": len(rows),
        "liveCompanies": len(companies),
        "reviewQueueRows": sum(row.review_rank is not None for row in rows),
        "priorityCounts": dict(sorted(Counter(row.priority for row in rows).items())),
        "changeCount": len(pending),
        "unchangedCount": len(rows) - len(pending),
        "appliedCount": applied,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and apply the reviewed CT333 lead-fit ranking. "
            "The default mode is read-only."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_RANKING_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changed lead-fit fields after all preflight checks pass.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TWENTY_API_URL", ""),
        help="Twenty base URL (default: TWENTY_API_URL).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=20,
        help="HTTP request timeout (default: 20).",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest = _read_manifest(args.manifest)
        rows = _read_ranking(args.input, manifest)
        client = TwentyClient(
            args.base_url,
            os.environ.get("TWENTY_API_KEY", ""),
            args.timeout_seconds,
        )
        result = apply_ranking(
            client,
            rows=rows,
            manifest=manifest,
            apply=args.apply,
        )
    except RankingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
