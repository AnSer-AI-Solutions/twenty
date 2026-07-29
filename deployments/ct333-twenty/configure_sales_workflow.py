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

# The standard Company table. Twenty stores its name as the template
# `All {objectLabelPlural}` and renders it against the effective object label on
# every read, so it reads "All Companies" today and "All Leads" once the Lead
# labels are applied, and something else again under another workspace locale.
# `key` is the stable identifier and is what this configurator matches on.
INDEX_VIEW_KEY = "INDEX"
INDEX_VIEW_DESCRIPTION = f"Company {INDEX_VIEW_KEY} view"

# Operator-created views. Their names are stored verbatim, so they do not move
# with the object label and are safe to resolve by name.
QUEUE_VIEW_NAME = "Dashboard Priority Call Queue"
RECONTACT_VIEW_NAME = "Recontact Due"
NAMED_VIEW_NAMES = (QUEUE_VIEW_NAME, RECONTACT_VIEW_NAME)
MANAGED_VIEWS = (INDEX_VIEW_DESCRIPTION, *NAMED_VIEW_NAMES)

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

# Every Company field this configurator creates itself.
OWNED_FIELD_NAMES: frozenset[str] = frozenset(
    definition["name"] for definition in SALES_FIELDS
)


def _dependency_field_names() -> tuple[str, ...]:
    names: list[str] = []
    for column in (*SALES_COLUMNS, *RECONTACT_COLUMNS):
        if column["name"] not in OWNED_FIELD_NAMES and column["name"] not in names:
            names.append(column["name"])
    for definition in RECONTACT_FILTERS:
        field = definition["field"]
        if field not in OWNED_FIELD_NAMES and field not in names:
            names.append(field)
    return tuple(names)


# Managed columns and filters also reference the standard `name` and the
# enrichment-owned lead fields. Creating either here would take over a
# definition this configurator does not own, so they are required to exist.
DEPENDENCY_FIELD_NAMES: tuple[str, ...] = _dependency_field_names()

# Clamp the server-side page size and follow pageInfo so that a workspace with
# many objects still resolves Company instead of reporting it absent because it
# landed past the first page.
OBJECT_PAGE_LIMIT = 200
MAX_OBJECT_PAGES = 25

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
    _preflight_dependencies(fields_by_name)

    # Resolved before the first write, and identically in both modes: a managed
    # view that is missing or ambiguous must stop the run before any field
    # exists, not after half the metadata has been created. Only the view list
    # is read here; each view's columns are read later, once field creation has
    # had a chance to register its own mappings on them.
    views = _items(
        client.request(
            "GET",
            "/rest/metadata/views?" + urlencode({"objectMetadataId": company["id"]}),
        )
    )
    index_view, queue_view, recontact_view = _resolve_managed_views(
        views, company["id"]
    )

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

    index_view_details = {
        "view": _index_view_label(index_view),
        "viewKey": INDEX_VIEW_KEY,
    }
    for view, view_details in (
        (index_view, index_view_details),
        (queue_view, {"view": QUEUE_VIEW_NAME}),
    ):
        changes.extend(
            _configure_view_columns(
                client,
                view=view,
                view_details=view_details,
                fields_by_name=fields_by_name,
                columns=SALES_COLUMNS,
                apply=apply,
            )
        )

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
                view_details={"view": RECONTACT_VIEW_NAME},
                fields_by_name=fields_by_name,
                columns=RECONTACT_COLUMNS,
                apply=apply,
            )
        )
        changes.extend(
            _configure_view_filters(
                client,
                view=recontact_view,
                view_details={"view": RECONTACT_VIEW_NAME},
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


def _resolve_managed_views(
    views: list[dict[str, Any]], company_id: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    index_view = _resolve_index_view(views, company_id)
    queue_view = _resolve_named_view(views, QUEUE_VIEW_NAME, required=True)
    recontact_view = _resolve_named_view(views, RECONTACT_VIEW_NAME, required=False)
    _reject_role_collisions(
        (
            (INDEX_VIEW_DESCRIPTION, index_view),
            (QUEUE_VIEW_NAME, queue_view),
            (RECONTACT_VIEW_NAME, recontact_view),
        )
    )
    return index_view, queue_view, recontact_view


def _resolve_index_view(
    views: list[dict[str, Any]], company_id: str
) -> dict[str, Any]:
    index_views = [
        view
        for view in views
        if isinstance(view, dict) and view.get("key") == INDEX_VIEW_KEY
    ]
    # Creating a field makes Twenty register a hidden mapping on every INDEX view
    # that is active and not soft-deleted, so those are the views this
    # configurator may reconcile. The REST list drops soft-deleted views but
    # still returns deactivated ones. Same rule as configure_crm_objects.py.
    live_index_views = [
        view
        for view in index_views
        if view.get("isActive") is not False and view.get("deletedAt") is None
    ]
    if len(live_index_views) != 1:
        raise ConfigurationError(
            f"Twenty Company has {len(live_index_views)} live {INDEX_VIEW_KEY} "
            f"views out of {len(index_views)}, expected exactly one; refusing to "
            "guess which view to reconcile"
        )
    view = live_index_views[0]
    # The objectMetadataId filter is applied server side. Re-checking it here
    # means no other object's index view can be reconciled by mistake even if
    # that filter were ignored, rather than relying on it.
    if view.get("objectMetadataId") != company_id:
        raise ConfigurationError(
            f"Twenty returned a Company {INDEX_VIEW_KEY} view that belongs to "
            f"object {view.get('objectMetadataId')!r}, expected {company_id!r}"
        )
    return view


def _resolve_named_view(
    views: list[dict[str, Any]], name: str, *, required: bool
) -> dict[str, Any] | None:
    matches = [
        view for view in views if isinstance(view, dict) and view.get("name") == name
    ]
    if len(matches) > 1:
        raise ConfigurationError(
            f'Twenty returned {len(matches)} views named "{name}"; refusing to '
            "guess which one to reconcile"
        )
    if matches:
        return matches[0]
    if required:
        raise ConfigurationError(f'Twenty view "{name}" was not found')
    # Absent is a normal state for Recontact Due; the caller creates it.
    return None


def _reject_role_collisions(
    resolved: tuple[tuple[str, dict[str, Any] | None], ...],
) -> None:
    # A rendered index-view name that happens to equal a managed name, or an
    # operator who renamed the index view onto one, would otherwise make one view
    # take two managed column sets and have the second overwrite the first.
    roles_by_view_id: dict[str, list[str]] = {}
    for role, view in resolved:
        if view is None:
            continue
        view_id = _require_id(view, f"the view for {role}")
        roles_by_view_id.setdefault(view_id, []).append(role)
    for view_id, roles in roles_by_view_id.items():
        if len(roles) > 1:
            raise ConfigurationError(
                f"Twenty view {view_id} resolves to more than one managed role "
                f"({', '.join(roles)}); refusing to reconcile one view twice"
            )


def _index_view_label(view: dict[str, Any]) -> str:
    # Reported for the operator's benefit only; the view was resolved by key, so
    # whatever this renders to has no bearing on which view is written.
    name = view.get("name")
    if isinstance(name, str) and name:
        return name
    return INDEX_VIEW_DESCRIPTION


def _require_id(payload: dict[str, Any], description: str) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"Twenty returned {description} without an id")
    return value


def _preflight_dependencies(fields_by_name: dict[str, dict[str, Any]]) -> None:
    # Read-only, and independent of the mode: a dependency this configurator
    # cannot create must fail identically in a dry run and an apply, before the
    # first write, rather than after half the metadata has been created.
    # Existence is all that is checked; these definitions belong to whoever owns
    # them, so their type, label, and options are none of this script's business.
    missing = [name for name in DEPENDENCY_FIELD_NAMES if name not in fields_by_name]
    if not missing:
        return
    raise ConfigurationError(
        "Twenty Company is missing fields required by managed columns or "
        f"filters: {', '.join(missing)}. This configurator never creates them; "
        "they are standard or enrichment-owned. Create them first, then rerun."
    )


def _configure_view_columns(
    client: TwentyMetadataClient,
    *,
    view: dict[str, Any],
    view_details: dict[str, Any],
    fields_by_name: dict[str, dict[str, Any]],
    columns: tuple[dict[str, Any], ...],
    apply: bool,
) -> list[Change]:
    changes: list[Change] = []
    view_id = _require_id(view, f"the view for {view_details['view']}")
    view_fields = _items(
        client.request(
            "GET",
            "/rest/metadata/viewFields?" + urlencode({"viewId": view_id}),
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
                    details={**view_details, **target},
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
                    details={**view_details, **target},
                )
            )
            if apply:
                client.request(
                    "POST",
                    "/rest/metadata/viewFields",
                    {
                        "viewId": view_id,
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
                details={**view_details, **update},
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
    view_details: dict[str, Any],
    fields_by_name: dict[str, dict[str, Any]],
    apply: bool,
) -> list[Change]:
    changes: list[Change] = []
    view_id = _require_id(view, f"the view for {view_details['view']}")
    view_filters = _items(
        client.request(
            "GET",
            "/rest/metadata/viewFilters?" + urlencode({"viewId": view_id}),
        )
    )
    filters_by_field_id: dict[str, dict[str, Any]] = {}
    for item in view_filters:
        field_id = item.get("fieldMetadataId")
        if field_id in filters_by_field_id:
            raise ConfigurationError(
                f'Twenty view "{view_details["view"]}" has duplicate filters for '
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
                        **view_details,
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
                    details={**view_details, **target},
                )
            )
            if apply:
                client.request(
                    "POST",
                    "/rest/metadata/viewFilters",
                    {
                        "viewId": view_id,
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
                details={**view_details, **update},
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
    company = _find_company(client)
    if company is None:
        raise ConfigurationError("Twenty Company object was not found")
    return company


def _find_company(client: TwentyMetadataClient) -> dict[str, Any] | None:
    # Company on the first page stays a single request, which is the normal
    # case. Later pages are fetched only while Company is still missing, so a
    # workspace with many objects cannot make this report Company absent, and a
    # server that paginates badly past a page that already answered the
    # question cannot fail a run that had its answer.
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(MAX_OBJECT_PAGES):
        query: dict[str, Any] = {"limit": OBJECT_PAGE_LIMIT}
        if cursor is not None:
            query["starting_after"] = cursor
        payload = client.request(
            "GET", "/rest/metadata/objects?" + urlencode(query)
        )
        company = next(
            (
                item
                for item in _items(payload)
                if isinstance(item, dict) and item.get("nameSingular") == "company"
            ),
            None,
        )
        if company is not None:
            return company

        page_info = payload.get("pageInfo") if isinstance(payload, dict) else None
        if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
            return None
        next_cursor = page_info.get("endCursor")
        # A page that claims a successor without naming a usable, unseen one
        # would otherwise loop or silently truncate the search.
        if not isinstance(next_cursor, str) or not next_cursor:
            raise ConfigurationError(
                "Twenty object metadata pagination did not advance"
            )
        if next_cursor in seen_cursors:
            raise ConfigurationError(
                "Twenty object metadata pagination repeated a page"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise ConfigurationError(
        f"Twenty object metadata exceeded {MAX_OBJECT_PAGES} pages"
    )


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
    # isNullable and defaultValue decide how the field behaves at runtime just
    # as much as its type does, so a live field that has drifted on either is
    # not the field this configurator would have created and must not be
    # reported as converged.
    for key in ("type", "label", "isNullable", "defaultValue"):
        # A key the definition omits is unconstrained, except for defaultValue:
        # the field is then created without one, so the live field is expected
        # to carry null. An absent key and an explicit null read the same way.
        if key not in definition and key != "defaultValue":
            continue
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
        "views": list(MANAGED_VIEWS),
        "changeCount": len(changes),
        "changes": [change.as_dict() for change in changes],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
