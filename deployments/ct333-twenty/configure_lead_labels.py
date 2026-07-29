#!/usr/bin/env python3
"""Relabel the CT333 Company object as Lead in the Twenty UI.

This configurator changes two display labels on one object. It never renames
the object internally, never reads or writes a CRM record, never touches a
field or a view, and never issues POST or DELETE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# The internal names are the CT332 API contract: the daily CRM sync posts to
# `/rest/companies` and reads Company field names. Twenty derives REST paths and
# GraphQL fields from these names, never from the labels, so relabelling leaves
# the contract intact — but only for as long as the names below stay put.
COMPANY_NAME_SINGULAR = "company"
COMPANY_NAME_PLURAL = "companies"

LEAD_LABEL_SINGULAR = "Lead"
LEAD_LABEL_PLURAL = "Leads"

# The whole mutation surface of this configurator: two display labels on one
# object, sent as the exact body of a single PATCH.
MANAGED_LABELS: dict[str, str] = {
    "labelSingular": LEAD_LABEL_SINGULAR,
    "labelPlural": LEAD_LABEL_PLURAL,
}

# QUERY_MAX_RECORDS clamps the server-side page size; pageInfo is followed so
# that a workspace with many objects still resolves Company.
OBJECT_PAGE_LIMIT = 200
MAX_OBJECT_PAGES = 25

# POST and DELETE are deliberately absent: this configurator creates nothing and
# removes nothing, so those methods are rejected before a request is built.
ALLOWED_METHODS = ("GET", "PATCH")

# Only GET is replayed. A metadata write that may already have landed cannot be
# replayed safely, so the PATCH is attempted exactly once.
RETRYABLE_METHODS = ("GET",)
MAX_ATTEMPTS = 3


class ConfigurationError(RuntimeError):
    """Raised when the live metadata cannot be changed safely."""


@dataclass(frozen=True, slots=True)
class Change:
    action: str
    resource: str
    name: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "resource": self.resource,
            "name": self.name,
            **self.details,
        }


class TwentyMetadataClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: int = 20):
        if not base_url:
            raise ConfigurationError("TWENTY_API_URL is required")
        if not api_key:
            raise ConfigurationError("TWENTY_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if method not in ALLOWED_METHODS:
            raise ConfigurationError(
                f"{method} is not permitted by this configurator"
            )
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
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    if response.status not in expected:
                        raise ConfigurationError(
                            f"Twenty returned unexpected HTTP {response.status}"
                        )
                    return json.load(response)
            except HTTPError as exc:
                detail = exc.read(4096).decode(errors="replace")
                if (exc.code == 429 or exc.code >= 500) and attempt < attempts:
                    time.sleep(attempt)
                    continue
                raise ConfigurationError(
                    f"Twenty metadata request failed with HTTP {exc.code}: {detail}"
                    + _write_outcome_guidance(method, path)
                ) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < attempts:
                    time.sleep(attempt)
                    continue
                raise ConfigurationError(
                    f"Twenty metadata connection failed: {exc}"
                    + _write_outcome_guidance(method, path)
                ) from exc
        raise AssertionError("unreachable")


def _write_outcome_guidance(method: str, path: str) -> str:
    if method == "GET":
        return ""
    return (
        f". The outcome of {method} {path} may be unknown; "
        "run a dry run before retrying"
    )


def configure_lead_labels(
    client: TwentyMetadataClient, *, apply: bool
) -> list[Change]:
    company = _company_metadata(client)
    company_id = _require_id(company, f'object "{COMPANY_NAME_SINGULAR}"')
    current_labels = _effective_labels(company)
    if current_labels == MANAGED_LABELS:
        return []

    change = Change(
        action="update",
        resource="object",
        name=COMPANY_NAME_SINGULAR,
        details={
            "labelSingular": {
                "from": current_labels["labelSingular"],
                "to": LEAD_LABEL_SINGULAR,
            },
            "labelPlural": {
                "from": current_labels["labelPlural"],
                "to": LEAD_LABEL_PLURAL,
            },
            # Restated in every change log entry because they are the invariant
            # the whole change rests on: the labels move, the names do not.
            "nameSingular": COMPANY_NAME_SINGULAR,
            "namePlural": COMPANY_NAME_PLURAL,
        },
    )
    if not apply:
        return [change]

    client.request(
        "PATCH", _company_path(company_id), dict(MANAGED_LABELS)
    )
    _verify_labels_settled(client)
    return [change]


def _company_path(company_id: str) -> str:
    return f"/rest/metadata/objects/{company_id}"


def _company_metadata(client: TwentyMetadataClient) -> dict[str, Any]:
    # Matched on either internal name so that a half-renamed object is caught as
    # drift rather than read as a missing Company and silently skipped.
    candidates = [
        item
        for item in _fetch_objects(client)
        if item.get("nameSingular") == COMPANY_NAME_SINGULAR
        or item.get("namePlural") == COMPANY_NAME_PLURAL
    ]
    if len(candidates) != 1:
        raise ConfigurationError(
            f"Twenty returned {len(candidates)} objects matching "
            f'"{COMPANY_NAME_SINGULAR}"/"{COMPANY_NAME_PLURAL}", expected '
            "exactly one; refusing to guess which object to relabel"
        )
    company = candidates[0]
    _validate_internal_names(company)
    return company


def _validate_internal_names(company: dict[str, Any]) -> None:
    for key, expected in (
        ("nameSingular", COMPANY_NAME_SINGULAR),
        ("namePlural", COMPANY_NAME_PLURAL),
    ):
        if company.get(key) != expected:
            raise ConfigurationError(
                f"Twenty Company object has {key} {company.get(key)!r}, "
                f"expected {expected!r}; refusing to relabel an object whose "
                "internal names have already moved"
            )
    # This flag means the owner asked Twenty to keep the names derived from the
    # labels, and `Lead`/`Leads` do not derive `company`/`companies`. v2.20
    # validates that pairing rather than rewriting the names, so the write would
    # be rejected on a custom object and would only slip through on a standard
    # one because the label is diverted into `overrides`. Neither outcome is a
    # relabel this configurator should perform unattended: clearing the flag is
    # a deliberate decision about the CT332 API contract, not a side effect.
    if company.get("isLabelSyncedWithName") is True:
        raise ConfigurationError(
            f'Twenty object "{COMPANY_NAME_SINGULAR}" has '
            "isLabelSyncedWithName set; its names are meant to track its "
            f"labels, and {LEAD_LABEL_SINGULAR!r}/{LEAD_LABEL_PLURAL!r} do "
            f"not derive {COMPANY_NAME_SINGULAR!r}/{COMPANY_NAME_PLURAL!r}"
        )


def _effective_labels(company: dict[str, Any]) -> dict[str, str | None]:
    # A label change on a standard object is stored in the `overrides` blob and
    # the base column keeps the shipped label; a custom object is written in
    # place. resolveEffectiveEntity spreads overrides over the entity, so
    # reading override-then-base is what the UI actually shows, and is what
    # makes a second run report no change under either storage.
    overrides = company.get("overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    labels: dict[str, str | None] = {}
    for key in MANAGED_LABELS:
        override = overrides.get(key)
        if isinstance(override, str) and override:
            labels[key] = override
            continue
        base = company.get(key)
        labels[key] = base if isinstance(base, str) else None
    return labels


def _verify_labels_settled(client: TwentyMetadataClient) -> None:
    # The PATCH is the only write, so there is nothing to roll back; this read
    # exists so an accepted-but-ineffective update is reported as an error
    # rather than as a successful run that never settles. It re-resolves through
    # _company_metadata, so it also re-asserts after the write that the internal
    # names, and therefore the CT332 API contract, did not move.
    settled = _effective_labels(_company_metadata(client))
    if settled != MANAGED_LABELS:
        raise ConfigurationError(
            f'Twenty object "{COMPANY_NAME_SINGULAR}" still reports labels '
            f"{settled['labelSingular']!r}/{settled['labelPlural']!r} after "
            f"the update, expected {LEAD_LABEL_SINGULAR!r}/"
            f"{LEAD_LABEL_PLURAL!r}"
        )


def _fetch_objects(client: TwentyMetadataClient) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_OBJECT_PAGES):
        query: dict[str, Any] = {"limit": OBJECT_PAGE_LIMIT}
        if cursor is not None:
            query["starting_after"] = cursor
        payload = client.request(
            "GET", "/rest/metadata/objects?" + urlencode(query)
        )
        objects.extend(_items(payload, "objects"))
        page_info = payload.get("pageInfo") if isinstance(payload, dict) else None
        if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
            return objects
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or next_cursor == cursor:
            raise ConfigurationError(
                "Twenty object metadata pagination did not advance"
            )
        cursor = next_cursor
    raise ConfigurationError(
        f"Twenty object metadata exceeded {MAX_OBJECT_PAGES} pages"
    )


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    # IS_REST_METADATA_API_NEW_FORMAT_DIRECT decides whether the collection
    # arrives as `data` or as `data.objects`; both shapes are accepted.
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ConfigurationError("Twenty returned invalid metadata JSON")
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, list):
            return value
    raise ConfigurationError("Twenty metadata response did not contain a list")


def _require_id(payload: dict[str, Any], description: str) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"Twenty returned {description} without an id")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Relabel the CT333 Company object as Lead. Only the display "
            "labels change; the internal names, fields, views, and records "
            "are left untouched. The default mode is read-only."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reported label change. Omit for a read-only dry run.",
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
        client = TwentyMetadataClient(
            args.base_url,
            os.environ.get("TWENTY_API_KEY", ""),
            args.timeout_seconds,
        )
        changes = configure_lead_labels(client, apply=args.apply)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result = {
        "mode": "apply" if args.apply else "dry-run",
        "object": COMPANY_NAME_SINGULAR,
        "internalNames": {
            "nameSingular": COMPANY_NAME_SINGULAR,
            "namePlural": COMPANY_NAME_PLURAL,
        },
        "labels": dict(MANAGED_LABELS),
        "changeCount": len(changes),
        "changes": [change.as_dict() for change in changes],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
