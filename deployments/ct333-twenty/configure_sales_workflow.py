#!/usr/bin/env python3
"""Configure CT333 Company sales fields, lifecycle, and saved views."""

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

EXISTING_VIEW_NAMES = ("All Companies", "Dashboard Priority Call Queue")
RECONTACT_VIEW_NAME = "Recontact Due"
MANAGED_VIEW_NAMES = (*EXISTING_VIEW_NAMES, RECONTACT_VIEW_NAME)

SALES_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "type": "SELECT",
        "name": "callStatus",
        "label": "Call Status",
        "description": "Latest sales call outcome",
        "defaultValue": "'NOT_CALLED'",
        "isNullable": True,
        "options": [
            {
                "color": "gray",
                "label": "Not Called",
                "value": "NOT_CALLED",
                "position": 0,
            },
            {
                "color": "orange",
                "label": "No Answer",
                "value": "NO_ANSWER",
                "position": 1,
            },
            {
                "color": "yellow",
                "label": "Voicemail",
                "value": "VOICEMAIL",
                "position": 2,
            },
            {
                "color": "blue",
                "label": "Connected",
                "value": "CONNECTED",
                "position": 3,
            },
            {
                "color": "purple",
                "label": "Callback",
                "value": "CALLBACK",
                "position": 4,
            },
            {
                "color": "green",
                "label": "Qualified",
                "value": "QUALIFIED",
                "position": 5,
            },
            {
                "color": "gray",
                "label": "Not Interested",
                "value": "NOT_INTERESTED",
                "position": 6,
            },
            {
                "color": "red",
                "label": "Bad Number",
                "value": "BAD_NUMBER",
                "position": 7,
            },
        ],
    },
    {
        "type": "NUMBER",
        "name": "callAttempts",
        "label": "Call Attempts",
        "description": "Number of outbound call attempts",
        "isNullable": True,
    },
    {
        "type": "DATE_TIME",
        "name": "lastCalledAt",
        "label": "Last Called At",
        "description": "Time of the latest outbound call",
        "isNullable": True,
    },
    {
        "type": "DATE_TIME",
        "name": "nextFollowUpAt",
        "label": "Next Follow-Up",
        "description": "Scheduled time for the next sales follow-up",
        "isNullable": True,
    },
    {
        "type": "RICH_TEXT",
        "name": "callNotes",
        "label": "Call Notes",
        "description": "Sales notes from outbound calls",
        "isNullable": True,
    },
    {
        "type": "BOOLEAN",
        "name": "salesActioned",
        "label": "Actioned",
        "description": "Sales has taken the next action for this company",
        "defaultValue": False,
        "isNullable": False,
    },
    {
        "type": "BOOLEAN",
        "name": "byronReviewed",
        "label": "Byron Reviewed",
        "description": "Byron has reviewed this company",
        "defaultValue": False,
        "isNullable": False,
    },
    {
        "type": "SELECT",
        "name": "salesLifecycleStatus",
        "label": "Lifecycle Status",
        "description": "Sales-owned lifecycle state; records are retained in every state",
        "defaultValue": "'NEW'",
        "isNullable": False,
        "options": [
            {
                "color": "gray",
                "label": "New",
                "value": "NEW",
                "position": 0,
            },
            {
                "color": "blue",
                "label": "Working",
                "value": "WORKING",
                "position": 1,
            },
            {
                "color": "yellow",
                "label": "Nurture",
                "value": "NURTURE",
                "position": 2,
            },
            {
                "color": "purple",
                "label": "Qualified",
                "value": "QUALIFIED",
                "position": 3,
            },
            {
                "color": "green",
                "label": "Won",
                "value": "WON",
                "position": 4,
            },
            {
                "color": "orange",
                "label": "Disqualified",
                "value": "DISQUALIFIED",
                "position": 5,
            },
            {
                "color": "red",
                "label": "Do Not Contact",
                "value": "DO_NOT_CONTACT",
                "position": 6,
            },
            {
                "color": "gray",
                "label": "Closed Business",
                "value": "CLOSED_BUSINESS",
                "position": 7,
            },
        ],
    },
    {
        "type": "SELECT",
        "name": "salesDisposition",
        "label": "Disposition",
        "description": "Sales-owned reason for the current lifecycle state",
        "isNullable": True,
        "options": [
            {
                "color": "yellow",
                "label": "Not Now",
                "value": "NOT_NOW",
                "position": 0,
            },
            {
                "color": "orange",
                "label": "Unreachable",
                "value": "UNREACHABLE",
                "position": 1,
            },
            {
                "color": "gray",
                "label": "Poor Fit",
                "value": "POOR_FIT",
                "position": 2,
            },
            {
                "color": "red",
                "label": "Bad Number",
                "value": "BAD_NUMBER",
                "position": 3,
            },
            {
                "color": "gray",
                "label": "Closed Business",
                "value": "CLOSED_BUSINESS",
                "position": 4,
            },
            {
                "color": "red",
                "label": "Do Not Contact",
                "value": "DO_NOT_CONTACT",
                "position": 5,
            },
            {
                "color": "gray",
                "label": "Other",
                "value": "OTHER",
                "position": 6,
            },
        ],
    },
    {
        "type": "DATE_TIME",
        "name": "recontactAt",
        "label": "Recontact At",
        "description": "Date when a nurtured lead should return to the sales queue",
        "isNullable": True,
    },
)

SALES_COLUMNS: tuple[dict[str, Any], ...] = (
    {"name": "salesActioned", "size": 110},
    {"name": "byronReviewed", "size": 150},
    {"name": "callNotes", "size": 320},
    {"name": "callAttempts", "size": 120},
    {"name": "nextFollowUpAt", "size": 190},
    {"name": "salesLifecycleStatus", "size": 170},
    {"name": "salesDisposition", "size": 170},
    {"name": "recontactAt", "size": 190},
)

RECONTACT_COLUMNS: tuple[dict[str, Any], ...] = (
    {"name": "name", "size": 220},
    {"name": "leadPhone", "size": 180},
    {"name": "leadEmail", "size": 240},
    {"name": "leadQualityTier", "size": 140},
    {"name": "salesLifecycleStatus", "size": 170},
    {"name": "salesDisposition", "size": 170},
    {"name": "recontactAt", "size": 190},
    {"name": "callStatus", "size": 150},
    {"name": "callNotes", "size": 320},
)

RECONTACT_FILTERS: tuple[dict[str, Any], ...] = (
    {
        "field": "salesLifecycleStatus",
        "operand": "IS",
        "value": ["NURTURE"],
    },
    {
        "field": "recontactAt",
        "operand": "IS_IN_PAST",
        "value": {},
    },
)

# DELETE is deliberately absent: this configurator only ever adds metadata.
ALLOWED_METHODS = ("GET", "POST", "PATCH")

# Only GET is replayed. A metadata write is not idempotent, so retrying a POST
# or PATCH that may already have been committed risks a duplicate field, view,
# view column, or view filter.
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
            raise ConfigurationError(f"{method} is not permitted by this configurator")
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


def configure_sales_workflow(
    client: TwentyMetadataClient, *, apply: bool
) -> list[Change]:
    changes: list[Change] = []
    company = _company_metadata(client)
    fields_by_name = {field["name"]: field for field in company.get("fields", [])}

    for definition in SALES_FIELDS:
        existing = fields_by_name.get(definition["name"])
        if existing is not None:
            _validate_existing_field(existing, definition)
            continue

        changes.append(
            Change(
                action="create",
                resource="field",
                name=definition["name"],
                details={"label": definition["label"], "type": definition["type"]},
            )
        )
        if apply:
            client.request(
                "POST",
                "/rest/metadata/fields",
                {**definition, "objectMetadataId": company["id"]},
                expected=(201,),
            )

    if apply and any(change.resource == "field" for change in changes):
        company = _company_metadata(client)
        fields_by_name = {field["name"]: field for field in company.get("fields", [])}

    views = _items(
        client.request(
            "GET",
            "/rest/metadata/views?" + urlencode({"objectMetadataId": company["id"]}),
        )
    )
    views_by_name = {view.get("name"): view for view in views}
    for view_name in EXISTING_VIEW_NAMES:
        view = views_by_name.get(view_name)
        if view is None:
            raise ConfigurationError(f'Twenty view "{view_name}" was not found')
        changes.extend(
            _configure_view_columns(
                client,
                view=view,
                fields_by_name=fields_by_name,
                columns=SALES_COLUMNS,
                apply=apply,
            )
        )

    recontact_view = views_by_name.get(RECONTACT_VIEW_NAME)
    if recontact_view is None:
        changes.append(
            Change(
                action="create",
                resource="view",
                name=RECONTACT_VIEW_NAME,
                details={"type": "TABLE", "visibility": "WORKSPACE"},
            )
        )
        if apply:
            recontact_view = client.request(
                "POST",
                "/rest/metadata/views",
                {
                    "name": RECONTACT_VIEW_NAME,
                    "objectMetadataId": company["id"],
                    "icon": "IconCalendar",
                    "type": "TABLE",
                    "position": len(views),
                    "isCompact": False,
                    "openRecordIn": "SIDE_PANEL",
                    "visibility": "WORKSPACE",
                },
                expected=(201,),
            )

    if recontact_view is not None:
        changes.extend(
            _configure_view_columns(
                client,
                view=recontact_view,
                fields_by_name=fields_by_name,
                columns=RECONTACT_COLUMNS,
                apply=apply,
            )
        )
        changes.extend(
            _configure_view_filters(
                client,
                view=recontact_view,
                fields_by_name=fields_by_name,
                apply=apply,
            )
        )
    else:
        for column in RECONTACT_COLUMNS:
            changes.append(
                Change(
                    action="create",
                    resource="viewField",
                    name=column["name"],
                    details={
                        "view": RECONTACT_VIEW_NAME,
                        "isVisible": True,
                        "size": column["size"],
                    },
                )
            )
        for definition in RECONTACT_FILTERS:
            changes.append(
                Change(
                    action="create",
                    resource="viewFilter",
                    name=definition["field"],
                    details={
                        "view": RECONTACT_VIEW_NAME,
                        "operand": definition["operand"],
                        "value": definition["value"],
                    },
                )
            )

    return changes


def _configure_view_columns(
    client: TwentyMetadataClient,
    *,
    view: dict[str, Any],
    fields_by_name: dict[str, dict[str, Any]],
    columns: tuple[dict[str, Any], ...],
    apply: bool,
) -> list[Change]:
    changes: list[Change] = []
    view_fields = _items(
        client.request(
            "GET",
            "/rest/metadata/viewFields?" + urlencode({"viewId": view["id"]}),
        )
    )
    target_ids = {
        fields_by_name[column["name"]]["id"]
        for column in columns
        if column["name"] in fields_by_name
    }
    non_target_positions = [
        item.get("position", 0)
        for item in view_fields
        if item.get("fieldMetadataId") not in target_ids
    ]
    first_position = max(non_target_positions, default=-1) + 1
    view_fields_by_field_id = {item["fieldMetadataId"]: item for item in view_fields}

    for offset, column in enumerate(columns):
        field = fields_by_name.get(column["name"])
        target = {
            "isVisible": True,
            "position": first_position + offset,
            "size": column["size"],
        }

        if field is None:
            if apply:
                raise ConfigurationError(
                    f"Twenty field {column['name']} was not returned after creation"
                )
            changes.append(
                Change(
                    action="create",
                    resource="viewField",
                    name=column["name"],
                    details={"view": view["name"], **target},
                )
            )
            continue

        existing_view_field = view_fields_by_field_id.get(field["id"])
        if existing_view_field is None:
            changes.append(
                Change(
                    action="create",
                    resource="viewField",
                    name=column["name"],
                    details={"view": view["name"], **target},
                )
            )
            if apply:
                client.request(
                    "POST",
                    "/rest/metadata/viewFields",
                    {
                        "viewId": view["id"],
                        "fieldMetadataId": field["id"],
                        **target,
                    },
                    expected=(201,),
                )
            continue

        update = {
            key: value
            for key, value in target.items()
            if existing_view_field.get(key) != value
        }
        if not update:
            continue
        changes.append(
            Change(
                action="update",
                resource="viewField",
                name=column["name"],
                details={"view": view["name"], **update},
            )
        )
        if apply:
            client.request(
                "PATCH",
                f"/rest/metadata/viewFields/{existing_view_field['id']}",
                update,
            )

    return changes


def _configure_view_filters(
    client: TwentyMetadataClient,
    *,
    view: dict[str, Any],
    fields_by_name: dict[str, dict[str, Any]],
    apply: bool,
) -> list[Change]:
    changes: list[Change] = []
    view_filters = _items(
        client.request(
            "GET",
            "/rest/metadata/viewFilters?" + urlencode({"viewId": view["id"]}),
        )
    )
    filters_by_field_id: dict[str, dict[str, Any]] = {}
    for item in view_filters:
        field_id = item.get("fieldMetadataId")
        if field_id in filters_by_field_id:
            raise ConfigurationError(
                f'Twenty view "{view["name"]}" has duplicate filters for '
                f"field {field_id}"
            )
        if field_id:
            filters_by_field_id[field_id] = item

    for definition in RECONTACT_FILTERS:
        field = fields_by_name.get(definition["field"])
        if field is None:
            if apply:
                raise ConfigurationError(
                    f"Twenty field {definition['field']} was not returned "
                    "after creation"
                )
            changes.append(
                Change(
                    action="create",
                    resource="viewFilter",
                    name=definition["field"],
                    details={
                        "view": view["name"],
                        "operand": definition["operand"],
                        "value": definition["value"],
                    },
                )
            )
            continue

        target = {
            "operand": definition["operand"],
            "value": definition["value"],
        }
        existing = filters_by_field_id.get(field["id"])
        if existing is None:
            changes.append(
                Change(
                    action="create",
                    resource="viewFilter",
                    name=definition["field"],
                    details={"view": view["name"], **target},
                )
            )
            if apply:
                client.request(
                    "POST",
                    "/rest/metadata/viewFilters",
                    {
                        "viewId": view["id"],
                        "fieldMetadataId": field["id"],
                        **target,
                    },
                    expected=(201,),
                )
            continue

        update = {
            key: value for key, value in target.items() if existing.get(key) != value
        }
        if not update:
            continue
        changes.append(
            Change(
                action="update",
                resource="viewFilter",
                name=definition["field"],
                details={"view": view["name"], **update},
            )
        )
        if apply:
            client.request(
                "PATCH",
                f"/rest/metadata/viewFilters/{existing['id']}",
                update,
            )

    return changes


def _company_metadata(client: TwentyMetadataClient) -> dict[str, Any]:
    objects = _items(client.request("GET", "/rest/metadata/objects?limit=100"))
    company = next(
        (item for item in objects if item.get("nameSingular") == "company"),
        None,
    )
    if company is None:
        raise ConfigurationError("Twenty Company object was not found")
    return company


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ConfigurationError("Twenty returned invalid metadata JSON")
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("objects", "views", "viewFields", "viewFilters"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    raise ConfigurationError("Twenty metadata response did not contain a list")


def _validate_existing_field(
    existing: dict[str, Any], definition: dict[str, Any]
) -> None:
    for key in ("type", "label"):
        if existing.get(key) != definition.get(key):
            raise ConfigurationError(
                f"Twenty field {definition['name']} has {key} "
                f"{existing.get(key)!r}, expected {definition.get(key)!r}"
            )
    if definition["type"] != "SELECT":
        return
    actual_values = [option.get("value") for option in existing.get("options") or []]
    expected_values = [option["value"] for option in definition["options"]]
    if actual_values != expected_values:
        raise ConfigurationError(
            f"Twenty field {definition['name']} has unexpected select options"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Configure CT333 Company sales fields, lifecycle, and saved views. "
            "The default mode is read-only."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the reported metadata changes. Omit for a read-only dry run.",
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
        changes = configure_sales_workflow(client, apply=args.apply)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result = {
        "mode": "apply" if args.apply else "dry-run",
        "views": list(MANAGED_VIEW_NAMES),
        "changeCount": len(changes),
        "changes": [change.as_dict() for change in changes],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
