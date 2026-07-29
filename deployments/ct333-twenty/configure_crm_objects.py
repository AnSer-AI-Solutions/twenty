#!/usr/bin/env python3
"""Configure the CT333 Customer and Caller custom objects and table columns.

This configurator creates metadata only. It never reads or writes CRM records,
never writes to the Company object that holds the lead pipeline, and never
issues DELETE.
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

CUSTOMER_OBJECT: dict[str, Any] = {
    "nameSingular": "customer",
    "namePlural": "customers",
    "labelSingular": "Customer",
    "labelPlural": "Customers",
    "description": "Active AnSer customer account",
    "icon": "IconBuildingStore",
}

CALLER_OBJECT: dict[str, Any] = {
    "nameSingular": "caller",
    "namePlural": "callers",
    "labelSingular": "Caller",
    "labelPlural": "Callers",
    "description": "Individual who calls in on behalf of a customer",
    "icon": "IconPhoneCall",
}

MANAGED_OBJECTS: tuple[dict[str, Any], ...] = (CUSTOMER_OBJECT, CALLER_OBJECT)
MANAGED_OBJECT_NAMES: tuple[str, ...] = tuple(
    definition["nameSingular"] for definition in MANAGED_OBJECTS
)

CUSTOMER_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "type": "SELECT",
        "name": "customerStatus",
        "label": "Customer Status",
        "description": "Account state for this customer",
        # Twenty stores SELECT defaults SQL-quoted, matching the convention
        # configure_sales_workflow.py uses for salesLifecycleStatus.
        "defaultValue": "'ACTIVE'",
        "isNullable": False,
        "options": [
            {
                "color": "green",
                "label": "Active",
                "value": "ACTIVE",
                "position": 0,
            },
            {
                "color": "yellow",
                "label": "Paused",
                "value": "PAUSED",
                "position": 1,
            },
            {
                "color": "gray",
                "label": "Churned",
                "value": "CHURNED",
                "position": 2,
            },
        ],
    },
    {
        "type": "PHONES",
        "name": "accountPhone",
        "label": "Account Phone",
        "description": "Phone numbers for the customer account",
        "isNullable": True,
    },
    {
        "type": "EMAILS",
        "name": "accountEmail",
        "label": "Account Email",
        "description": "Email addresses for the customer account",
        "isNullable": True,
    },
)

CALLER_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "type": "PHONES",
        "name": "callerPhone",
        "label": "Caller Phone",
        "description": "Phone numbers this caller calls from",
        "isNullable": True,
    },
)

MANAGED_FIELDS: dict[str, tuple[dict[str, Any], ...]] = {
    CUSTOMER_OBJECT["nameSingular"]: CUSTOMER_FIELDS,
    CALLER_OBJECT["nameSingular"]: CALLER_FIELDS,
}

# Twenty only supports MANY_TO_ONE / ONE_TO_MANY and generates the inverse
# field itself, so the field is posted on Caller, the many side. MANY_TO_ONE
# sets onDelete SET_NULL, hence isNullable.
CALLER_CUSTOMER_RELATION: dict[str, Any] = {
    "objectName": CALLER_OBJECT["nameSingular"],
    "targetObjectName": CUSTOMER_OBJECT["nameSingular"],
    "inverseFieldName": "callers",
    "field": {
        "type": "RELATION",
        "name": "customer",
        "label": "Customer",
        "icon": "IconBuildingStore",
        "isNullable": True,
    },
    "relationCreationPayload": {
        "type": "MANY_TO_ONE",
        "targetFieldLabel": "Callers",
        "targetFieldIcon": "IconPhoneCall",
    },
    "inverseRelationType": "ONE_TO_MANY",
}

# ViewKey has exactly one member in v2.20 and the object-creation migration
# stamps it on the table view it generates. The generated view is stored under
# the literal name `All {objectLabelPlural}`, which ViewController rewrites per
# locale on every read, so the name that comes back is a rendered string and
# never a stable key.
INDEX_VIEW_KEY = "INDEX"

INDEX_VIEW_COLUMNS: dict[str, tuple[dict[str, Any], ...]] = {
    CUSTOMER_OBJECT["nameSingular"]: (
        {"name": "name", "size": 220},
        {"name": "customerStatus", "size": 150},
        {"name": "accountPhone", "size": 180},
        {"name": "accountEmail", "size": 240},
        {"name": CALLER_CUSTOMER_RELATION["inverseFieldName"], "size": 160},
    ),
    CALLER_OBJECT["nameSingular"]: (
        {"name": "name", "size": 220},
        {"name": "callerPhone", "size": 180},
        {"name": CALLER_CUSTOMER_RELATION["field"]["name"], "size": 200},
    ),
}

# QUERY_MAX_RECORDS clamps the server-side page size; pageInfo is followed so
# that a workspace with many objects still resolves Customer and Caller.
OBJECT_PAGE_LIMIT = 200
MAX_OBJECT_PAGES = 25

# DELETE is deliberately absent: this configurator only ever adds metadata.
ALLOWED_METHODS = ("GET", "POST", "PATCH")

# Only GET is replayed. A metadata write is not idempotent, so retrying a POST
# that may already have been applied risks a duplicate object or field.
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


def configure_crm_objects(
    client: TwentyMetadataClient, *, apply: bool
) -> list[Change]:
    changes: list[Change] = []
    objects_by_name = _managed_objects(client)
    _validate_metadata(objects_by_name)

    created_objects = False
    for definition in MANAGED_OBJECTS:
        if definition["nameSingular"] in objects_by_name:
            continue
        changes.append(
            Change(
                action="create",
                resource="object",
                name=definition["nameSingular"],
                details={
                    "labelSingular": definition["labelSingular"],
                    "labelPlural": definition["labelPlural"],
                },
            )
        )
        if apply:
            client.request(
                "POST",
                "/rest/metadata/objects",
                dict(definition),
                expected=(201,),
            )
            created_objects = True

    # Creating an object runs a real workspace schema migration, and the create
    # response shape depends on IS_REST_METADATA_API_NEW_FORMAT_DIRECT, so ids
    # are re-read from the collection instead of parsed out of the POST body.
    if created_objects:
        objects_by_name = _managed_objects(client)
        _validate_metadata(objects_by_name)

    created_fields = False
    for definition in MANAGED_OBJECTS:
        object_name = definition["nameSingular"]
        existing_fields = _fields_by_name(objects_by_name.get(object_name))
        for field_definition in MANAGED_FIELDS[object_name]:
            if field_definition["name"] in existing_fields:
                continue
            changes.append(
                Change(
                    action="create",
                    resource="field",
                    name=field_definition["name"],
                    details={
                        "object": object_name,
                        "label": field_definition["label"],
                        "type": field_definition["type"],
                    },
                )
            )
            if apply:
                client.request(
                    "POST",
                    "/rest/metadata/fields",
                    {
                        **field_definition,
                        "objectMetadataId": _managed_object_id(
                            objects_by_name, object_name
                        ),
                    },
                    expected=(201,),
                )
                created_fields = True

    if created_fields:
        objects_by_name = _managed_objects(client)
        _validate_metadata(objects_by_name)

    relation_changes = _configure_caller_customer_relation(
        client, objects_by_name, apply=apply
    )
    changes.extend(relation_changes)
    if apply and relation_changes:
        objects_by_name = _managed_objects(client)
        _validate_metadata(objects_by_name)

    changes.extend(
        _configure_index_view_columns(client, objects_by_name, apply=apply)
    )
    return changes


def _configure_caller_customer_relation(
    client: TwentyMetadataClient,
    objects_by_name: dict[str, dict[str, Any]],
    *,
    apply: bool,
) -> list[Change]:
    source_name = CALLER_CUSTOMER_RELATION["objectName"]
    target_name = CALLER_CUSTOMER_RELATION["targetObjectName"]
    inverse_name = CALLER_CUSTOMER_RELATION["inverseFieldName"]
    definition = CALLER_CUSTOMER_RELATION["field"]
    payload = CALLER_CUSTOMER_RELATION["relationCreationPayload"]

    source_fields = _fields_by_name(objects_by_name.get(source_name))
    # Relations are create-once: re-posting an existing one duplicates the pair
    # or fails with a name conflict. Compatibility was checked by
    # _validate_existing_relation before any write happened.
    if definition["name"] in source_fields:
        return []

    change = Change(
        action="create",
        resource="relation",
        name=f"{source_name}.{definition['name']}",
        details={
            "type": payload["type"],
            "target": target_name,
            "inverse": f"{target_name}.{inverse_name}",
        },
    )
    if not apply:
        return [change]

    client.request(
        "POST",
        "/rest/metadata/fields",
        {
            **definition,
            "objectMetadataId": _managed_object_id(objects_by_name, source_name),
            "relationCreationPayload": {
                **payload,
                "targetObjectMetadataId": _managed_object_id(
                    objects_by_name, target_name
                ),
            },
        },
        expected=(201,),
    )
    return [change]


def _configure_index_view_columns(
    client: TwentyMetadataClient,
    objects_by_name: dict[str, dict[str, Any]],
    *,
    apply: bool,
) -> list[Change]:
    changes: list[Change] = []
    for object_name, columns in INDEX_VIEW_COLUMNS.items():
        if object_name not in objects_by_name:
            changes.extend(_planned_index_view_changes(object_name, columns))
            continue
        changes.extend(
            _reconcile_index_view_columns(
                client, objects_by_name, object_name, columns, apply=apply
            )
        )
    return changes


def _reconcile_index_view_columns(
    client: TwentyMetadataClient,
    objects_by_name: dict[str, dict[str, Any]],
    object_name: str,
    columns: tuple[dict[str, Any], ...],
    *,
    apply: bool,
) -> list[Change]:
    object_id = _managed_object_id(objects_by_name, object_name)
    view = _resolve_index_view(client, object_name, object_id)
    view_id = _require_id(view, f'INDEX view of "{object_name}"')
    view_fields, view_fields_by_field_id = _index_view_fields(
        client, object_name, view_id
    )

    fields_by_name = _fields_by_name(objects_by_name[object_name])
    resolved = [
        (column, fields_by_name.get(column["name"])) for column in columns
    ]
    missing = [column["name"] for column, field in resolved if field is None]
    if missing and apply:
        # Every managed column is created earlier in this run, so a gap here
        # means the metadata read back does not match what was written. Raise
        # before touching this view rather than reconciling it half way.
        raise ConfigurationError(
            f"Twenty object {object_name} is missing field(s) "
            f"{', '.join(missing)} after creation; refusing to reconcile its "
            "INDEX view columns"
        )

    target_ids = {
        _require_id(field, f"field {object_name}.{column['name']}")
        for column, field in resolved
        if field is not None
    }
    # Managed columns are appended after every column this configurator does not
    # own, so an operator's own columns keep their positions. Same rule as
    # configure_sales_workflow.py.
    non_target_positions = [
        _position_of(item)
        for item in view_fields
        if item.get("fieldMetadataId") not in target_ids
        and _position_of(item) is not None
    ]
    first_position = max(non_target_positions, default=-1) + 1

    changes: list[Change] = []
    for offset, (column, field) in enumerate(resolved):
        target = {
            "isVisible": True,
            "position": first_position + offset,
            "size": column["size"],
        }
        if field is None:
            changes.append(
                _view_field_change("update", object_name, column, target)
            )
            continue

        existing = view_fields_by_field_id.get(field["id"])
        if existing is None:
            changes.append(
                _view_field_change("create", object_name, column, target)
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
            if existing.get(key) != value
        }
        if not update:
            continue
        changes.append(_view_field_change("update", object_name, column, update))
        if apply:
            # Creating a field makes Twenty register a hidden mapping on every
            # active INDEX view of that object, and the migration builder
            # rejects a second mapping for the same field and view. The
            # auto-created row is therefore patched, never re-posted.
            client.request(
                "PATCH",
                "/rest/metadata/viewFields/"
                + _require_id(
                    existing, f"INDEX view column {object_name}.{column['name']}"
                ),
                update,
            )
    return changes


def _resolve_index_view(
    client: TwentyMetadataClient, object_name: str, object_id: str
) -> dict[str, Any]:
    views = _items(
        client.request(
            "GET",
            "/rest/metadata/views?" + urlencode({"objectMetadataId": object_id}),
        ),
        "views",
    )
    index_views = [
        view
        for view in views
        if isinstance(view, dict) and view.get("key") == INDEX_VIEW_KEY
    ]
    # A new field is only registered on INDEX views that are active and not
    # soft-deleted, so those are the views this configurator may reconcile. The
    # REST list drops soft-deleted views but still returns deactivated ones.
    live_index_views = [
        view
        for view in index_views
        if view.get("isActive") is not False and view.get("deletedAt") is None
    ]
    if len(live_index_views) != 1:
        raise ConfigurationError(
            f'Twenty object "{object_name}" has {len(live_index_views)} live '
            f"INDEX views out of {len(index_views)}, expected exactly one; "
            "refusing to guess which view to reconcile"
        )
    view = live_index_views[0]
    # The objectMetadataId filter is applied server side. Re-checking it here
    # means the Company view can never be reconciled by mistake even if that
    # filter were ignored, rather than relying on it.
    if view.get("objectMetadataId") != object_id:
        raise ConfigurationError(
            f'Twenty returned an INDEX view for "{object_name}" that belongs to '
            f"object {view.get('objectMetadataId')!r}, expected {object_id!r}"
        )
    return view


def _position_of(view_field: dict[str, Any]) -> float | None:
    position = view_field.get("position")
    if isinstance(position, bool) or not isinstance(position, (int, float)):
        return None
    return position


def _index_view_fields(
    client: TwentyMetadataClient, object_name: str, view_id: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    view_fields = [
        item
        for item in _items(
            client.request(
                "GET",
                "/rest/metadata/viewFields?" + urlencode({"viewId": view_id}),
            ),
            "viewFields",
        )
        # Same reasoning as the view lookup: only mappings that name this view
        # are considered, so no other view's columns can ever be patched.
        if isinstance(item, dict) and item.get("viewId") == view_id
    ]
    view_fields_by_field_id: dict[str, dict[str, Any]] = {}
    for view_field in view_fields:
        field_id = view_field.get("fieldMetadataId")
        if not isinstance(field_id, str) or not field_id:
            continue
        if field_id in view_fields_by_field_id:
            raise ConfigurationError(
                f'Twenty INDEX view of "{object_name}" has more than one '
                f"mapping for field {field_id}; refusing to guess which one "
                "to reconcile"
            )
        view_fields_by_field_id[field_id] = view_field
    return view_fields, view_fields_by_field_id


def _planned_index_view_changes(
    object_name: str, columns: tuple[dict[str, Any], ...]
) -> list[Change]:
    # The object does not exist yet, so its INDEX view cannot be read. Creating
    # the object and its fields makes Twenty register a mapping for every
    # managed column, which is why the planned action is an update; the position
    # is left out because it is derived from view state that does not exist yet.
    return [
        _view_field_change(
            "update",
            object_name,
            column,
            {"isVisible": True, "size": column["size"]},
        )
        for column in columns
    ]


def _view_field_change(
    action: str,
    object_name: str,
    column: dict[str, Any],
    details: dict[str, Any],
) -> Change:
    return Change(
        action=action,
        resource="viewField",
        name=column["name"],
        details={"object": object_name, "viewKey": INDEX_VIEW_KEY, **details},
    )


def _managed_objects(
    client: TwentyMetadataClient,
) -> dict[str, dict[str, Any]]:
    # Only Customer and Caller are kept. Company never enters the mapping the
    # write paths resolve ids from, so no write can address it.
    objects_by_name: dict[str, dict[str, Any]] = {}
    for item in _fetch_objects(client):
        name = item.get("nameSingular")
        if name not in MANAGED_OBJECT_NAMES:
            continue
        if name in objects_by_name:
            raise ConfigurationError(
                f'Twenty returned more than one "{name}" object'
            )
        objects_by_name[name] = item
    return objects_by_name


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


def _fields_by_name(
    object_metadata: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(object_metadata, dict):
        return {}
    fields = object_metadata.get("fields") or []
    return {
        field["name"]: field
        for field in fields
        if isinstance(field, dict) and "name" in field
    }


def _managed_object_id(
    objects_by_name: dict[str, dict[str, Any]], object_name: str
) -> str:
    if object_name not in MANAGED_OBJECT_NAMES:
        raise ConfigurationError(
            f'Refusing to write metadata for unmanaged object "{object_name}"'
        )
    existing = objects_by_name.get(object_name)
    if existing is None:
        raise ConfigurationError(
            f'Twenty object "{object_name}" was not returned after creation'
        )
    return _require_id(existing, f'object "{object_name}"')


def _require_id(payload: dict[str, Any], description: str) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"Twenty returned {description} without an id")
    return value


def _validate_metadata(objects_by_name: dict[str, dict[str, Any]]) -> None:
    for definition in MANAGED_OBJECTS:
        object_name = definition["nameSingular"]
        existing = objects_by_name.get(object_name)
        if existing is None:
            continue
        _validate_existing_object(existing, definition)
        fields_by_name = _fields_by_name(existing)
        for field_definition in MANAGED_FIELDS[object_name]:
            field = fields_by_name.get(field_definition["name"])
            if field is not None:
                _validate_existing_field(field, field_definition, object_name)
    _validate_existing_relation(objects_by_name)


def _validate_existing_object(
    existing: dict[str, Any], definition: dict[str, Any]
) -> None:
    for key in ("namePlural", "labelSingular", "labelPlural"):
        if existing.get(key) != definition.get(key):
            raise ConfigurationError(
                f"Twenty object {definition['nameSingular']} has {key} "
                f"{existing.get(key)!r}, expected {definition.get(key)!r}"
            )


def _validate_existing_field(
    existing: dict[str, Any], definition: dict[str, Any], object_name: str
) -> None:
    for key in ("type", "label", "isNullable", "defaultValue"):
        if key not in definition:
            continue
        if existing.get(key) != definition.get(key):
            raise ConfigurationError(
                f"Twenty field {object_name}.{definition['name']} has {key} "
                f"{existing.get(key)!r}, expected {definition.get(key)!r}"
            )
    if definition["type"] != "SELECT":
        return
    actual_values = _select_option_values(existing.get("options") or [])
    expected_values = _select_option_values(definition["options"])
    if actual_values != expected_values:
        raise ConfigurationError(
            f"Twenty field {object_name}.{definition['name']} has unexpected "
            "select options"
        )


def _select_option_values(
    options: list[dict[str, Any]],
) -> list[tuple[Any, Any, Any, Any]]:
    # Options are compared in position order so that a workspace returning them
    # in a different array order is not mistaken for drift. A missing or
    # non-numeric position falls back to 0, which keeps the sort stable rather
    # than raising on a comparison.
    ordered = sorted(options, key=_option_position)
    return [
        (
            option.get("value"),
            option.get("label"),
            option.get("color"),
            option.get("position"),
        )
        for option in ordered
    ]


def _option_position(option: dict[str, Any]) -> float:
    position = option.get("position")
    return position if isinstance(position, (int, float)) else 0


def _validate_existing_relation(
    objects_by_name: dict[str, dict[str, Any]],
) -> None:
    source_name = CALLER_CUSTOMER_RELATION["objectName"]
    target_name = CALLER_CUSTOMER_RELATION["targetObjectName"]
    inverse_name = CALLER_CUSTOMER_RELATION["inverseFieldName"]
    definition = CALLER_CUSTOMER_RELATION["field"]

    source_field = _fields_by_name(objects_by_name.get(source_name)).get(
        definition["name"]
    )
    inverse_field = _fields_by_name(objects_by_name.get(target_name)).get(
        inverse_name
    )

    if source_field is not None:
        _validate_existing_field(source_field, definition, source_name)
        _validate_relation_type(
            source_field,
            f"{source_name}.{definition['name']}",
            CALLER_CUSTOMER_RELATION["relationCreationPayload"]["type"],
        )
        if inverse_field is None:
            raise ConfigurationError(
                f"Twenty field {source_name}.{definition['name']} exists "
                f"without its {target_name}.{inverse_name} inverse"
            )

    if inverse_field is not None:
        if inverse_field.get("type") != "RELATION":
            raise ConfigurationError(
                f"Twenty field {target_name}.{inverse_name} has type "
                f"{inverse_field.get('type')!r}, expected 'RELATION'"
            )
        _validate_relation_type(
            inverse_field,
            f"{target_name}.{inverse_name}",
            CALLER_CUSTOMER_RELATION["inverseRelationType"],
        )
        if source_field is None:
            raise ConfigurationError(
                f"Twenty field {target_name}.{inverse_name} exists without "
                f"{source_name}.{definition['name']}; creating the relation "
                "would collide"
            )


def _validate_relation_type(
    existing: dict[str, Any], qualified_name: str, expected: str
) -> None:
    settings = existing.get("settings")
    actual = settings.get("relationType") if isinstance(settings, dict) else None
    if actual != expected:
        raise ConfigurationError(
            f"Twenty field {qualified_name} has relation type {actual!r}, "
            f"expected {expected!r}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Configure the CT333 Customer and Caller custom objects, their "
            "fields, the Caller to Customer relation, and the columns of each "
            "object's INDEX table view. The default mode is read-only."
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
        changes = configure_crm_objects(client, apply=args.apply)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result = {
        "mode": "apply" if args.apply else "dry-run",
        "objects": list(MANAGED_OBJECT_NAMES),
        "indexViewColumns": {
            object_name: [column["name"] for column in columns]
            for object_name, columns in INDEX_VIEW_COLUMNS.items()
        },
        "changeCount": len(changes),
        "changes": [change.as_dict() for change in changes],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
