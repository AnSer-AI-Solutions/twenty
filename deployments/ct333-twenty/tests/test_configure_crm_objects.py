from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import sys
import unittest
from typing import Any
from unittest import mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

MODULE_PATH = pathlib.Path(__file__).parents[1] / "configure_crm_objects.py"
SPEC = importlib.util.spec_from_file_location("configure_crm_objects", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
crm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = crm
SPEC.loader.exec_module(crm)

COMPANY_ID = "10000000-0000-0000-0000-000000000001"
COMPANY_VIEW_ID = "10000000-0000-0000-0000-0000000000ff"
OBJECT_IDS = {
    "customer": "30000000-0000-0000-0000-000000000001",
    "caller": "30000000-0000-0000-0000-000000000002",
}
# Returned by every create so a configurator that reads ids out of a POST
# response instead of re-reading the collection fails loudly.
DECOY_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"

RESPONSE_FORMATS = ("new", "legacy")

# DEFAULT_VIEW_FIELD_SIZE in v2.20. Every view field Twenty creates by itself
# gets this width, which is why the configurator has to patch sizes at all.
DEFAULT_VIEW_FIELD_SIZE = 180

# The system fields Twenty adds to every custom object, in the order the
# metadata API returns them. computeFlatViewFieldsToCreate excludes deletedAt,
# TS_VECTOR, POSITION, RELATION, and a non-label-identifier id from the default
# view fields, and sorts the label identifier first; the rest are created
# visible, at DEFAULT_VIEW_FIELD_SIZE, at positions 0..n.
SYSTEM_FIELDS: tuple[dict[str, Any], ...] = (
    {"name": "id", "label": "Id", "type": "UUID"},
    {"name": "name", "label": "Name", "type": "TEXT"},
    {"name": "createdAt", "label": "Creation date", "type": "DATE_TIME"},
    {"name": "createdBy", "label": "Created by", "type": "ACTOR"},
    {"name": "updatedAt", "label": "Last update", "type": "DATE_TIME"},
    {"name": "updatedBy", "label": "Last updated by", "type": "ACTOR"},
    {"name": "deletedAt", "label": "Deleted at", "type": "DATE_TIME"},
    {"name": "position", "label": "Position", "type": "POSITION"},
    {"name": "searchVector", "label": "Search vector", "type": "TS_VECTOR"},
)
LABEL_IDENTIFIER_FIELD_NAME = "name"
DEFAULT_VIEW_FIELD_EXCLUDED_TYPES = (
    "TS_VECTOR",
    "POSITION",
    "RELATION",
    "MORPH_RELATION",
)
# The unmanaged columns Twenty leaves visible on a freshly created object.
UNMANAGED_DEFAULT_COLUMNS = ("createdAt", "createdBy", "updatedAt", "updatedBy")


class FakeClient:
    def __init__(
        self,
        *,
        response_format: str = "new",
        include_objects: bool = False,
        include_fields: bool = False,
        include_relation: bool = False,
        include_view_columns: bool = False,
        filler_object_count: int = 0,
        page_size: int = 200,
    ) -> None:
        self.response_format = response_format
        self.page_size = page_size
        self.company: dict[str, Any] = {
            "id": COMPANY_ID,
            "nameSingular": "company",
            "namePlural": "companies",
            "labelSingular": "Company",
            "labelPlural": "Companies",
            "fields": [
                {
                    "id": "company-field-name",
                    "name": "name",
                    "label": "Name",
                    "type": "TEXT",
                },
                {
                    "id": "company-field-lead-phone",
                    "name": "leadPhone",
                    "label": "Lead Phone",
                    "type": "TEXT",
                },
                {
                    "id": "company-field-lifecycle",
                    "name": "salesLifecycleStatus",
                    "label": "Lifecycle Status",
                    "type": "SELECT",
                },
            ],
        }
        self.objects: list[dict[str, Any]] = [self.company]
        self.views: list[dict[str, Any]] = []
        self.view_fields: dict[str, list[dict[str, Any]]] = {}
        self._add_company_index_view()
        for index in range(filler_object_count):
            self.objects.append(
                {
                    "id": f"20000000-0000-0000-0000-{index:012d}",
                    "nameSingular": f"filler{index}",
                    "namePlural": f"filler{index}s",
                    "labelSingular": f"Filler {index}",
                    "labelPlural": f"Filler {index}s",
                    "fields": [],
                }
            )
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        if include_objects:
            self._add_objects()
        if include_fields:
            self._add_fields()
        if include_relation:
            self._add_relation()
        if include_view_columns:
            self._add_view_columns()

    # --- request routing -------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path.startswith("/rest/metadata/objects"):
            return self._objects_response(path)
        if method == "GET" and path.startswith("/rest/metadata/views?"):
            query = parse_qs(urlsplit(path).query)
            object_id = query["objectMetadataId"][0]
            return copy.deepcopy(
                [
                    view
                    for view in self.views
                    if view["objectMetadataId"] == object_id
                ]
            )
        if method == "GET" and path.startswith("/rest/metadata/viewFields?"):
            query = parse_qs(urlsplit(path).query)
            return copy.deepcopy(self.view_fields[query["viewId"][0]])
        if method == "POST" and path == "/rest/metadata/objects":
            assert payload is not None
            self._create_object(payload)
            return self._wrap_create("createOneObject", payload)
        if method == "POST" and path == "/rest/metadata/fields":
            assert payload is not None
            self._create_field(payload)
            return self._wrap_create("createOneField", payload)
        if method == "POST" and path == "/rest/metadata/viewFields":
            assert payload is not None
            items = self.view_fields[payload["viewId"]]
            # v2.20 rejects a second mapping for the same field and view, so a
            # configurator that posts over an auto-created row fails the run.
            if any(
                item["fieldMetadataId"] == payload["fieldMetadataId"]
                for item in items
            ):
                raise AssertionError(
                    "view field already exists for "
                    f"{payload['viewId']}/{payload['fieldMetadataId']}"
                )
            view_field = copy.deepcopy(payload)
            view_field["id"] = (
                f"view-field-{payload['viewId']}-{payload['fieldMetadataId']}"
            )
            items.append(view_field)
            return copy.deepcopy(view_field)
        if method == "PATCH" and path.startswith("/rest/metadata/viewFields/"):
            assert payload is not None
            view_field = self._view_field_by_id(path.rsplit("/", 1)[-1])
            view_field.update(copy.deepcopy(payload))
            return copy.deepcopy(view_field)
        raise AssertionError(f"unexpected request: {method} {path}")

    def _objects_response(self, path: str) -> dict[str, Any]:
        query = parse_qs(urlsplit(path).query)
        limit = min(int(query.get("limit", ["60"])[0]), 200)
        cursor = query.get("starting_after", [None])[0]
        # The server paginates by id cursor, descending.
        ordered = sorted(self.objects, key=lambda item: item["id"], reverse=True)
        if cursor is not None:
            ordered = [item for item in ordered if item["id"] < cursor]
        page = ordered[: min(limit, self.page_size)]
        page_info = {
            "hasNextPage": len(ordered) > len(page),
            "startCursor": page[0]["id"] if page else None,
            "endCursor": page[-1]["id"] if page else None,
        }
        data = copy.deepcopy(page)
        if self.response_format == "legacy":
            return {
                "data": {"objects": data},
                "pageInfo": page_info,
                "totalCount": len(self.objects),
            }
        return {
            "data": data,
            "pageInfo": page_info,
            "totalCount": len(self.objects),
        }

    def _wrap_create(self, legacy_key: str, payload: dict[str, Any]) -> Any:
        created = {**copy.deepcopy(payload), "id": DECOY_ID}
        if self.response_format == "legacy":
            return {"data": {legacy_key: created}}
        return created

    # --- mutation --------------------------------------------------------

    def _create_object(self, payload: dict[str, Any]) -> None:
        name = payload["nameSingular"]
        if any(item["nameSingular"] == name for item in self.objects):
            raise AssertionError(f"object {name} was created twice")
        object_id = OBJECT_IDS[name]
        object_metadata = {
            "id": object_id,
            "nameSingular": name,
            "namePlural": payload["namePlural"],
            "labelSingular": payload["labelSingular"],
            "labelPlural": payload["labelPlural"],
            "fields": [
                {**field, "id": f"{name}-field-{field['name']}"}
                for field in SYSTEM_FIELDS
            ],
        }
        self.objects.append(object_metadata)
        self._add_index_view(object_metadata)

    def _create_field(self, payload: dict[str, Any]) -> None:
        parent = self._object_by_id(payload["objectMetadataId"])
        field = {
            key: copy.deepcopy(value)
            for key, value in payload.items()
            if key not in ("objectMetadataId", "relationCreationPayload")
        }
        field["id"] = f"{parent['nameSingular']}-field-{payload['name']}"
        relation = payload.get("relationCreationPayload")
        if relation is not None:
            field["settings"] = {
                "relationType": relation["type"],
                "joinColumnName": f"{payload['name']}Id",
            }
            self._create_inverse_field(relation, parent)
        parent["fields"].append(field)
        self._auto_add_view_field(parent, field)

    def _create_inverse_field(
        self, relation: dict[str, Any], source: dict[str, Any]
    ) -> None:
        target = self._object_by_id(relation["targetObjectMetadataId"])
        inverse_name = crm.CALLER_CUSTOMER_RELATION["inverseFieldName"]
        inverse_field = {
            "id": f"{target['nameSingular']}-field-{inverse_name}",
            "name": inverse_name,
            "label": relation["targetFieldLabel"],
            "icon": relation["targetFieldIcon"],
            "type": "RELATION",
            "isNullable": True,
            "settings": {
                "relationType": crm.CALLER_CUSTOMER_RELATION[
                    "inverseRelationType"
                ],
                "joinColumnName": f"{source['nameSingular']}Id",
            },
        }
        target["fields"].append(inverse_field)
        self._auto_add_view_field(target, inverse_field)

    def _add_company_index_view(self) -> None:
        # Company owns an INDEX view too, so any configurator that resolved
        # views by name or swept every object would show up here.
        self.views.append(
            {
                "id": COMPANY_VIEW_ID,
                "name": "All {objectLabelPlural}",
                "key": "INDEX",
                "isActive": True,
                "deletedAt": None,
                "objectMetadataId": COMPANY_ID,
            }
        )
        self.view_fields[COMPANY_VIEW_ID] = [
            {
                "id": f"view-field-{COMPANY_VIEW_ID}-{field['id']}",
                "viewId": COMPANY_VIEW_ID,
                "fieldMetadataId": field["id"],
                "isVisible": True,
                "position": position,
                "size": DEFAULT_VIEW_FIELD_SIZE,
            }
            for position, field in enumerate(self.company["fields"])
        ]

    def _add_index_view(self, object_metadata: dict[str, Any]) -> None:
        object_name = object_metadata["nameSingular"]
        view_id = f"{object_name}-index-view"
        self.views.append(
            {
                "id": view_id,
                # Stored as the literal template; ViewController rewrites it per
                # locale on read, so the name is never a stable key.
                "name": "All {objectLabelPlural}",
                "key": "INDEX",
                "isActive": True,
                "deletedAt": None,
                "objectMetadataId": object_metadata["id"],
            }
        )
        default_fields = [
            field
            for field in object_metadata["fields"]
            if field["name"] != "deletedAt"
            and field["type"] not in DEFAULT_VIEW_FIELD_EXCLUDED_TYPES
            and field["name"] != "id"
        ]
        default_fields.sort(
            key=lambda field: field["name"] != LABEL_IDENTIFIER_FIELD_NAME
        )
        self.view_fields[view_id] = [
            {
                "id": f"view-field-{view_id}-{field['id']}",
                "viewId": view_id,
                "fieldMetadataId": field["id"],
                "isVisible": True,
                "position": position,
                "size": DEFAULT_VIEW_FIELD_SIZE,
            }
            for position, field in enumerate(default_fields)
        ]

    def _auto_add_view_field(
        self, object_metadata: dict[str, Any], field: dict[str, Any]
    ) -> None:
        # getFieldViewTargets emits one hidden view field per active, undeleted
        # INDEX view of the object, at max(position) + 1, regardless of type.
        for view in self.views:
            if (
                view["objectMetadataId"] != object_metadata["id"]
                or view["key"] != "INDEX"
                or view.get("isActive") is False
                or view.get("deletedAt") is not None
            ):
                continue
            items = self.view_fields[view["id"]]
            items.append(
                {
                    "id": f"view-field-{view['id']}-{field['id']}",
                    "viewId": view["id"],
                    "fieldMetadataId": field["id"],
                    "isVisible": False,
                    "position": max(
                        (item["position"] for item in items), default=-1
                    )
                    + 1,
                    "size": DEFAULT_VIEW_FIELD_SIZE,
                }
            )

    def _view_field_by_id(self, view_field_id: str) -> dict[str, Any]:
        for items in self.view_fields.values():
            for item in items:
                if item["id"] == view_field_id:
                    return item
        raise AssertionError(f"unknown view field {view_field_id}")

    def _object_by_id(self, object_id: str) -> dict[str, Any]:
        for item in self.objects:
            if item["id"] == object_id:
                return item
        raise AssertionError(f"unknown objectMetadataId {object_id}")

    # --- fixtures --------------------------------------------------------

    def _add_objects(self) -> None:
        for definition in crm.MANAGED_OBJECTS:
            self._create_object(definition)

    def _add_fields(self) -> None:
        for definition in crm.MANAGED_OBJECTS:
            name = definition["nameSingular"]
            parent = self.object_by_name(name)
            for field_definition in crm.MANAGED_FIELDS[name]:
                self._create_field(
                    {**field_definition, "objectMetadataId": parent["id"]}
                )

    def _add_relation(self) -> None:
        spec = crm.CALLER_CUSTOMER_RELATION
        self._create_field(
            {
                **spec["field"],
                "objectMetadataId": self.object_by_name(spec["objectName"])["id"],
                "relationCreationPayload": {
                    **spec["relationCreationPayload"],
                    "targetObjectMetadataId": self.object_by_name(
                        spec["targetObjectName"]
                    )["id"],
                },
            }
        )

    def add_view(
        self,
        object_name: str,
        view_id: str,
        *,
        name: str,
        key: str | None = None,
        is_active: bool = True,
        deleted_at: str | None = None,
    ) -> dict[str, Any]:
        view = {
            "id": view_id,
            "name": name,
            "key": key,
            "isActive": is_active,
            "deletedAt": deleted_at,
            "objectMetadataId": self.object_by_name(object_name)["id"],
        }
        self.views.append(view)
        self.view_fields[view_id] = []
        return view

    def _add_view_columns(self) -> None:
        # The reconciled end state, written out from the documented rule rather
        # than by calling the configurator, so a change to either side shows up
        # as a failing idempotency test.
        for object_name, columns in crm.INDEX_VIEW_COLUMNS.items():
            items = self.view_fields[self.index_view_id(object_name)]
            target_ids = {
                self.field_by_name(object_name, column["name"])["id"]
                for column in columns
            }
            first_position = (
                max(
                    (
                        item["position"]
                        for item in items
                        if item["fieldMetadataId"] not in target_ids
                    ),
                    default=-1,
                )
                + 1
            )
            for offset, column in enumerate(columns):
                field_id = self.field_by_name(object_name, column["name"])["id"]
                item = next(
                    item
                    for item in items
                    if item["fieldMetadataId"] == field_id
                )
                item["isVisible"] = True
                item["position"] = first_position + offset
                item["size"] = column["size"]

    # --- assertions helpers ----------------------------------------------

    def object_by_name(self, name: str) -> dict[str, Any]:
        for item in self.objects:
            if item["nameSingular"] == name:
                return item
        raise AssertionError(f"unknown object {name}")

    def field_by_name(self, object_name: str, field_name: str) -> dict[str, Any]:
        for field in self.object_by_name(object_name)["fields"]:
            if field["name"] == field_name:
                return field
        raise AssertionError(f"unknown field {object_name}.{field_name}")

    def index_view_id(self, object_name: str) -> str:
        object_id = self.object_by_name(object_name)["id"]
        for view in self.views:
            if (
                view["objectMetadataId"] == object_id
                and view["key"] == "INDEX"
                and view.get("isActive") is not False
            ):
                return view["id"]
        raise AssertionError(f"no live INDEX view for {object_name}")

    def index_view_columns(
        self, object_name: str
    ) -> list[tuple[str, bool, int, int]]:
        # (field name, isVisible, position, size), rendered in position order.
        field_names = {
            field["id"]: field["name"]
            for field in self.object_by_name(object_name)["fields"]
        }
        return [
            (
                field_names[item["fieldMetadataId"]],
                item["isVisible"],
                item["position"],
                item["size"],
            )
            for item in sorted(
                self.view_fields[self.index_view_id(object_name)],
                key=lambda item: item["position"],
            )
        ]

    def visible_index_view_columns(self, object_name: str) -> list[str]:
        return [
            name
            for name, is_visible, _, _ in self.index_view_columns(object_name)
            if is_visible
        ]

    @property
    def writes(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [call for call in self.calls if call[0] != "GET"]

    @property
    def view_field_writes(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [
            call
            for call in self.writes
            if call[1].startswith("/rest/metadata/viewFields")
        ]


class FailingWriteClient(FakeClient):
    def __init__(self, *, fail_on_write_number: int) -> None:
        super().__init__()
        self.fail_on_write_number = fail_on_write_number
        self.write_attempts = 0

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if method != "GET":
            self.write_attempts += 1
            if self.write_attempts == self.fail_on_write_number:
                self.calls.append((method, path, copy.deepcopy(payload)))
                raise crm.ConfigurationError("simulated metadata write failure")
        return super().request(method, path, payload, expected=expected)


class MisroutedViewClient(FakeClient):
    """Answers every view lookup with the Company INDEX view.

    Models a server that ignores the `objectMetadataId` filter, which is the
    only way a Company view could ever reach the reconciliation path.
    """

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if method == "GET" and path.startswith("/rest/metadata/views?"):
            self.calls.append((method, path, copy.deepcopy(payload)))
            return copy.deepcopy(
                [view for view in self.views if view["id"] == COMPANY_VIEW_ID]
            )
        return super().request(method, path, payload, expected=expected)


class MisroutedViewFieldClient(FakeClient):
    """Returns every view field whatever `viewId` was asked for."""

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if method == "GET" and path.startswith("/rest/metadata/viewFields?"):
            self.calls.append((method, path, copy.deepcopy(payload)))
            return copy.deepcopy(
                [item for items in self.view_fields.values() for item in items]
            )
        return super().request(method, path, payload, expected=expected)


def _change_tuples(changes: list[Any]) -> list[tuple[str, str, str]]:
    # Qualified by owning object, because the two INDEX views both carry a
    # column called `name`.
    return [
        (
            change.action,
            change.resource,
            f"{change.details['object']}.{change.name}"
            if "object" in change.details
            else change.name,
        )
        for change in changes
    ]


EXPECTED_FULL_CHANGES = [
    ("create", "object", "customer"),
    ("create", "object", "caller"),
    ("create", "field", "customer.customerStatus"),
    ("create", "field", "customer.accountPhone"),
    ("create", "field", "customer.accountEmail"),
    ("create", "field", "caller.callerPhone"),
    ("create", "relation", "caller.customer"),
    ("update", "viewField", "customer.name"),
    ("update", "viewField", "customer.customerStatus"),
    ("update", "viewField", "customer.accountPhone"),
    ("update", "viewField", "customer.accountEmail"),
    ("update", "viewField", "customer.callers"),
    ("update", "viewField", "caller.name"),
    ("update", "viewField", "caller.callerPhone"),
    ("update", "viewField", "caller.customer"),
]

# What the two tables must look like once reconciled: the managed columns in
# order, at the specified widths, appended after the columns Twenty created with
# the object and this configurator does not own.
EXPECTED_INDEX_VIEW_COLUMNS = {
    "customer": [
        ("createdAt", True, 1, DEFAULT_VIEW_FIELD_SIZE),
        ("createdBy", True, 2, DEFAULT_VIEW_FIELD_SIZE),
        ("updatedAt", True, 3, DEFAULT_VIEW_FIELD_SIZE),
        ("updatedBy", True, 4, DEFAULT_VIEW_FIELD_SIZE),
        ("name", True, 5, 220),
        ("customerStatus", True, 6, 150),
        ("accountPhone", True, 7, 180),
        ("accountEmail", True, 8, 240),
        ("callers", True, 9, 160),
    ],
    "caller": [
        ("createdAt", True, 1, DEFAULT_VIEW_FIELD_SIZE),
        ("createdBy", True, 2, DEFAULT_VIEW_FIELD_SIZE),
        ("updatedAt", True, 3, DEFAULT_VIEW_FIELD_SIZE),
        ("updatedBy", True, 4, DEFAULT_VIEW_FIELD_SIZE),
        ("name", True, 5, 220),
        ("callerPhone", True, 6, 180),
        ("customer", True, 7, 200),
    ],
}


class DryRunTests(unittest.TestCase):
    def test_dry_run_reports_creates_without_writes(self) -> None:
        client = FakeClient()

        changes = crm.configure_crm_objects(client, apply=False)

        self.assertEqual(_change_tuples(changes), EXPECTED_FULL_CHANGES)
        self.assertEqual(client.writes, [])
        self.assertEqual(
            [item["nameSingular"] for item in client.objects], ["company"]
        )

    def test_dry_run_on_partially_configured_workspace_reports_the_remainder(
        self,
    ) -> None:
        client = FakeClient(include_objects=True)

        changes = crm.configure_crm_objects(client, apply=False)

        self.assertEqual(
            _change_tuples(changes),
            [entry for entry in EXPECTED_FULL_CHANGES if entry[1] != "object"],
        )
        self.assertEqual(client.writes, [])


class ApplyOrderingTests(unittest.TestCase):
    def test_apply_creates_objects_before_fields_and_relation(self) -> None:
        client = FakeClient()

        changes = crm.configure_crm_objects(client, apply=True)

        self.assertEqual(_change_tuples(changes), EXPECTED_FULL_CHANGES)
        write_paths = [path for _, path, _ in client.writes]
        self.assertEqual(
            write_paths[:7],
            ["/rest/metadata/objects"] * 2 + ["/rest/metadata/fields"] * 5,
        )
        # Every remaining write reconciles a column, after all metadata exists.
        self.assertEqual(
            [path.rsplit("/", 1)[0] for path in write_paths[7:]],
            ["/rest/metadata/viewFields"] * 8,
        )
        self.assertEqual(
            [
                payload["nameSingular"]
                for _, path, payload in client.writes
                if path == "/rest/metadata/objects"
            ],
            ["customer", "caller"],
        )

    def test_relation_is_posted_last_with_re_read_object_ids(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        relation_writes = [
            payload
            for _, _, payload in client.writes
            if payload is not None and "relationCreationPayload" in payload
        ]
        self.assertEqual(len(relation_writes), 1)
        payload = relation_writes[0]
        field_writes = [
            call for call in client.writes if call[1] == "/rest/metadata/fields"
        ]
        self.assertEqual(field_writes[-1][2], payload)
        self.assertEqual(payload["objectMetadataId"], OBJECT_IDS["caller"])
        self.assertEqual(
            payload["relationCreationPayload"]["targetObjectMetadataId"],
            OBJECT_IDS["customer"],
        )
        self.assertEqual(payload["relationCreationPayload"]["type"], "MANY_TO_ONE")
        self.assertEqual(payload["isNullable"], True)
        self.assertEqual(
            client.field_by_name("customer", "callers")["type"], "RELATION"
        )

    def test_ids_are_never_taken_from_create_responses(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        self.assertNotIn(DECOY_ID, repr(client.writes))

    def test_object_pages_are_followed_until_the_cursor_is_exhausted(
        self,
    ) -> None:
        client = FakeClient(filler_object_count=5, page_size=2)

        changes = crm.configure_crm_objects(client, apply=True)

        self.assertEqual(_change_tuples(changes), EXPECTED_FULL_CHANGES)
        self.assertTrue(
            any("starting_after" in path for _, path, _ in client.calls)
        )


class IdempotencyTests(unittest.TestCase):
    def test_repeated_apply_is_idempotent(self) -> None:
        client = FakeClient(
            include_objects=True,
            include_fields=True,
            include_relation=True,
            include_view_columns=True,
        )

        changes = crm.configure_crm_objects(client, apply=True)

        self.assertEqual(changes, [])
        self.assertEqual(client.writes, [])

    def test_apply_twice_against_the_same_workspace_settles(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)
        first_write_count = len(client.writes)
        changes = crm.configure_crm_objects(client, apply=True)

        self.assertEqual(changes, [])
        self.assertEqual(len(client.writes), first_write_count)

    def test_relation_is_not_recreated_when_present(self) -> None:
        client = FakeClient(include_objects=True, include_relation=True)

        changes = crm.configure_crm_objects(client, apply=True)

        self.assertEqual(
            _change_tuples(changes),
            [
                entry
                for entry in EXPECTED_FULL_CHANGES
                if entry[1] in ("field", "viewField")
            ],
        )
        self.assertFalse(
            any(
                payload is not None and "relationCreationPayload" in payload
                for _, _, payload in client.writes
            )
        )
        self.assertEqual(
            sum(
                field["name"] == "customer"
                for field in client.object_by_name("caller")["fields"]
            ),
            1,
        )

    def test_partial_apply_is_recoverable_by_rerunning(self) -> None:
        client = FailingWriteClient(fail_on_write_number=2)

        with self.assertRaisesRegex(
            crm.ConfigurationError, "simulated metadata write failure"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(
            [item["nameSingular"] for item in client.objects],
            ["company", "customer"],
        )

        remaining = crm.configure_crm_objects(client, apply=True)

        self.assertEqual(
            _change_tuples(remaining),
            [
                entry
                for entry in EXPECTED_FULL_CHANGES
                if entry != ("create", "object", "customer")
            ],
        )
        self.assertEqual(crm.configure_crm_objects(client, apply=True), [])


class DriftTests(unittest.TestCase):
    def test_object_label_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True)
        client.object_by_name("customer")["labelPlural"] = "Accounts"

        with self.assertRaisesRegex(crm.ConfigurationError, "has labelPlural"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_object_name_plural_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True)
        client.object_by_name("caller")["namePlural"] = "callerRecords"

        with self.assertRaisesRegex(crm.ConfigurationError, "has namePlural"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_field_type_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_fields=True)
        client.field_by_name("customer", "accountPhone")["type"] = "TEXT"

        with self.assertRaisesRegex(crm.ConfigurationError, "has type"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_select_option_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_fields=True)
        status = client.field_by_name("customer", "customerStatus")
        status["options"][2]["value"] = "CANCELLED"

        with self.assertRaisesRegex(
            crm.ConfigurationError, "unexpected select options"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_field_nullability_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_fields=True)
        client.field_by_name("customer", "customerStatus")["isNullable"] = True

        with self.assertRaisesRegex(crm.ConfigurationError, "has isNullable"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_field_default_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_fields=True)
        client.field_by_name("customer", "customerStatus")["defaultValue"] = (
            "'CHURNED'"
        )

        with self.assertRaisesRegex(crm.ConfigurationError, "has defaultValue"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_select_option_label_and_color_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_fields=True)
        status = client.field_by_name("customer", "customerStatus")
        status["options"][0]["label"] = "Live"
        status["options"][0]["color"] = "red"

        with self.assertRaisesRegex(
            crm.ConfigurationError, "unexpected select options"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_select_options_returned_out_of_order_are_accepted(self) -> None:
        client = FakeClient(
            include_objects=True,
            include_fields=True,
            include_relation=True,
            include_view_columns=True,
        )
        status = client.field_by_name("customer", "customerStatus")
        status["options"] = list(reversed(status["options"]))

        self.assertEqual(crm.configure_crm_objects(client, apply=True), [])

    def test_relation_type_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_relation=True)
        client.field_by_name("caller", "customer")["settings"]["relationType"] = (
            "ONE_TO_MANY"
        )

        with self.assertRaisesRegex(crm.ConfigurationError, "has relation type"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_missing_relation_settings_fail_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_relation=True)
        client.field_by_name("caller", "customer").pop("settings")

        with self.assertRaisesRegex(crm.ConfigurationError, "relation type None"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_non_relation_field_on_the_relation_name_fails_before_writes(
        self,
    ) -> None:
        client = FakeClient(include_objects=True)
        client.object_by_name("caller")["fields"].append(
            {
                "id": "caller-field-customer",
                "name": "customer",
                "label": "Customer",
                "type": "TEXT",
            }
        )

        with self.assertRaisesRegex(crm.ConfigurationError, "has type"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_orphan_inverse_field_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_relation=True)
        caller = client.object_by_name("caller")
        caller["fields"] = [
            field for field in caller["fields"] if field["name"] != "customer"
        ]

        with self.assertRaisesRegex(crm.ConfigurationError, "would collide"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_missing_inverse_field_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True, include_relation=True)
        customer = client.object_by_name("customer")
        customer["fields"] = [
            field for field in customer["fields"] if field["name"] != "callers"
        ]

        with self.assertRaisesRegex(
            crm.ConfigurationError, "without its customer.callers inverse"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_duplicate_managed_object_fails_before_writes(self) -> None:
        client = FakeClient(include_objects=True)
        duplicate = copy.deepcopy(client.object_by_name("customer"))
        duplicate["id"] = "30000000-0000-0000-0000-000000000009"
        client.objects.append(duplicate)

        with self.assertRaisesRegex(
            crm.ConfigurationError, "more than one .customer. object"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])


def _view_field_id(
    client: FakeClient, object_name: str, field_name: str
) -> str:
    return (
        f"view-field-{client.index_view_id(object_name)}"
        f"-{client.field_by_name(object_name, field_name)['id']}"
    )


def _patch_body(
    client: FakeClient, object_name: str, field_name: str
) -> dict[str, Any]:
    suffix = "/" + _view_field_id(client, object_name, field_name)
    bodies = [
        payload
        for method, path, payload in client.writes
        if method == "PATCH" and path.endswith(suffix)
    ]
    assert len(bodies) == 1, f"{object_name}.{field_name} patched {len(bodies)}x"
    assert bodies[0] is not None
    return bodies[0]


class IndexViewColumnTests(unittest.TestCase):
    def test_declared_columns_match_the_specification(self) -> None:
        self.assertEqual(
            {
                object_name: [
                    (column["name"], column["size"]) for column in columns
                ]
                for object_name, columns in crm.INDEX_VIEW_COLUMNS.items()
            },
            {
                "customer": [
                    ("name", 220),
                    ("customerStatus", 150),
                    ("accountPhone", 180),
                    ("accountEmail", 240),
                    ("callers", 160),
                ],
                "caller": [
                    ("name", 220),
                    ("callerPhone", 180),
                    ("customer", 200),
                ],
            },
        )

    def test_apply_reconciles_both_index_views(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        for object_name, expected in EXPECTED_INDEX_VIEW_COLUMNS.items():
            with self.subTest(object_name=object_name):
                self.assertEqual(
                    client.index_view_columns(object_name), expected
                )

    def test_managed_columns_are_visible_in_the_declared_order(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        for object_name, columns in crm.INDEX_VIEW_COLUMNS.items():
            with self.subTest(object_name=object_name):
                visible = client.visible_index_view_columns(object_name)
                managed = [column["name"] for column in columns]
                self.assertEqual(visible[-len(managed) :], managed)

    def test_auto_created_hidden_mappings_are_patched_not_posted(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        methods = [method for method, _, _ in client.view_field_writes]
        self.assertEqual(methods, ["PATCH"] * 8)
        # customerStatus arrives as a hidden 180-wide column at position 5.
        self.assertEqual(
            _patch_body(client, "customer", "customerStatus"),
            {"isVisible": True, "position": 6, "size": 150},
        )
        # name is already visible, so only the width and position move.
        self.assertEqual(
            _patch_body(client, "customer", "name"),
            {"position": 5, "size": 220},
        )
        self.assertEqual(
            _patch_body(client, "caller", "customer"),
            {"isVisible": True, "position": 7, "size": 200},
        )

    def test_every_field_keeps_exactly_one_mapping(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        for object_name in crm.INDEX_VIEW_COLUMNS:
            with self.subTest(object_name=object_name):
                field_ids = [
                    item["fieldMetadataId"]
                    for item in client.view_fields[
                        client.index_view_id(object_name)
                    ]
                ]
                self.assertEqual(len(field_ids), len(set(field_ids)))

    def test_a_genuinely_absent_mapping_is_posted_once(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        view_id = client.index_view_id("customer")
        removed = _view_field_id(client, "customer", "accountEmail")
        client.view_fields[view_id] = [
            item for item in client.view_fields[view_id] if item["id"] != removed
        ]

        changes = crm.configure_crm_objects(client, apply=True)

        posts = [
            (path, payload)
            for method, path, payload in client.view_field_writes
            if method == "POST"
        ]
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            posts[0],
            (
                "/rest/metadata/viewFields",
                {
                    "viewId": view_id,
                    "fieldMetadataId": client.field_by_name(
                        "customer", "accountEmail"
                    )["id"],
                    "isVisible": True,
                    "position": 8,
                    "size": 240,
                },
            ),
        )
        self.assertIn(
            ("create", "viewField", "customer.accountEmail"),
            _change_tuples(changes),
        )
        self.assertEqual(crm.configure_crm_objects(client, apply=True), [])

    def test_unmanaged_columns_are_preserved(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        for object_name in crm.INDEX_VIEW_COLUMNS:
            with self.subTest(object_name=object_name):
                unmanaged = [
                    entry
                    for entry in client.index_view_columns(object_name)
                    if entry[0] in UNMANAGED_DEFAULT_COLUMNS
                ]
                self.assertEqual(
                    unmanaged,
                    [
                        (name, True, position, DEFAULT_VIEW_FIELD_SIZE)
                        for position, name in enumerate(
                            UNMANAGED_DEFAULT_COLUMNS, start=1
                        )
                    ],
                )

    def test_an_operator_added_column_is_left_alone(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        view_id = client.index_view_id("caller")
        operator_column = {
            "id": "caller-operator-column",
            "viewId": view_id,
            "fieldMetadataId": client.field_by_name("caller", "createdBy")["id"],
            "isVisible": True,
            "position": 42,
            "size": 320,
        }
        client.view_fields[view_id] = [
            item
            for item in client.view_fields[view_id]
            if item["fieldMetadataId"] != operator_column["fieldMetadataId"]
        ] + [operator_column]

        crm.configure_crm_objects(client, apply=True)

        self.assertIn(
            ("createdBy", True, 42, 320), client.index_view_columns("caller")
        )
        # Managed columns are appended after it rather than renumbering it.
        self.assertEqual(
            [
                entry
                for entry in client.index_view_columns("caller")
                if entry[0] in ("name", "callerPhone", "customer")
            ],
            [
                ("name", True, 43, 220),
                ("callerPhone", True, 44, 180),
                ("customer", True, 45, 200),
            ],
        )

    def test_dry_run_reports_view_columns_without_writes(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        before = copy.deepcopy(client.view_fields)

        changes = crm.configure_crm_objects(client, apply=False)

        self.assertEqual(
            _change_tuples(changes),
            [entry for entry in EXPECTED_FULL_CHANGES if entry[1] == "viewField"],
        )
        self.assertEqual(client.writes, [])
        self.assertEqual(client.view_fields, before)

    def test_dry_run_before_the_objects_exist_omits_unknowable_positions(
        self,
    ) -> None:
        client = FakeClient()

        changes = crm.configure_crm_objects(client, apply=False)

        view_field_changes = [
            change for change in changes if change.resource == "viewField"
        ]
        self.assertEqual(len(view_field_changes), 8)
        for change in view_field_changes:
            with self.subTest(name=change.name):
                self.assertNotIn("position", change.details)
                self.assertIs(change.details["isVisible"], True)
        self.assertEqual(client.writes, [])

    def test_partial_apply_of_view_columns_is_recoverable(self) -> None:
        # Write 8 is the first column reconciliation: 2 objects, 4 fields and
        # the relation come first.
        client = FailingWriteClient(fail_on_write_number=9)

        with self.assertRaisesRegex(
            crm.ConfigurationError, "simulated metadata write failure"
        ):
            crm.configure_crm_objects(client, apply=True)

        remaining = crm.configure_crm_objects(client, apply=True)

        self.assertEqual(
            _change_tuples(remaining),
            [
                entry
                for entry in EXPECTED_FULL_CHANGES
                if entry[1] == "viewField"
                and entry != ("update", "viewField", "customer.name")
            ],
        )
        self.assertEqual(crm.configure_crm_objects(client, apply=True), [])
        for object_name, expected in EXPECTED_INDEX_VIEW_COLUMNS.items():
            with self.subTest(object_name=object_name):
                self.assertEqual(
                    client.index_view_columns(object_name), expected
                )


class IndexViewResolutionTests(unittest.TestCase):
    def _reconciled_client(self, view_name: str) -> FakeClient:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        for view in client.views:
            if view["id"] == client.index_view_id("customer"):
                view["name"] = view_name
        return client

    def test_index_view_is_matched_by_key_not_name(self) -> None:
        # The stored template, what ViewController renders it to in English,
        # and what it renders to under a non-English locale.
        for view_name in (
            "All {objectLabelPlural}",
            "All Customers",
            "Tous les Customers",
            "すべての Customers",
            "",
        ):
            with self.subTest(view_name=view_name):
                client = self._reconciled_client(view_name)

                crm.configure_crm_objects(client, apply=True)

                self.assertEqual(
                    client.index_view_columns("customer"),
                    EXPECTED_INDEX_VIEW_COLUMNS["customer"],
                )

    def test_a_view_named_like_the_index_view_is_ignored(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        decoy = client.add_view(
            "customer", "customer-decoy-view", name="All Customers", key=None
        )

        crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.view_fields[decoy["id"]], [])
        self.assertEqual(
            client.index_view_columns("customer"),
            EXPECTED_INDEX_VIEW_COLUMNS["customer"],
        )

    def test_a_second_live_index_view_fails_closed(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        client.add_view(
            "caller", "caller-second-index-view", name="Callers", key="INDEX"
        )

        with self.assertRaisesRegex(
            crm.ConfigurationError, "has 2 live INDEX views out of 2"
        ):
            crm.configure_crm_objects(client, apply=True)

        # Customer is reconciled first, so its columns are already written; the
        # failure stops before anything is written for Caller, which keeps the
        # visible defaults and the hidden mappings Twenty auto-created.
        self.assertEqual(
            client.index_view_columns("caller"),
            [
                ("name", True, 0, DEFAULT_VIEW_FIELD_SIZE),
                ("createdAt", True, 1, DEFAULT_VIEW_FIELD_SIZE),
                ("createdBy", True, 2, DEFAULT_VIEW_FIELD_SIZE),
                ("updatedAt", True, 3, DEFAULT_VIEW_FIELD_SIZE),
                ("updatedBy", True, 4, DEFAULT_VIEW_FIELD_SIZE),
                ("callerPhone", False, 5, DEFAULT_VIEW_FIELD_SIZE),
                ("customer", False, 6, DEFAULT_VIEW_FIELD_SIZE),
            ],
        )

    def test_a_missing_index_view_fails_closed(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        view_id = client.index_view_id("customer")
        client.views = [view for view in client.views if view["id"] != view_id]

        with self.assertRaisesRegex(
            crm.ConfigurationError, "has 0 live INDEX views out of 0"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_a_deactivated_index_view_is_ignored(self) -> None:
        # Twenty registers new fields only on active INDEX views, so a
        # deactivated one is not the view to reconcile.
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        stale = client.add_view(
            "customer",
            "customer-stale-index-view",
            name="All {objectLabelPlural}",
            key="INDEX",
            is_active=False,
        )

        crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.view_fields[stale["id"]], [])
        self.assertEqual(
            client.index_view_columns("customer"),
            EXPECTED_INDEX_VIEW_COLUMNS["customer"],
        )

    def test_two_deactivated_index_views_still_fail_closed(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        original_view_id = client.index_view_id("customer")
        for suffix in ("a", "b"):
            client.add_view(
                "customer",
                f"customer-stale-index-view-{suffix}",
                name="All {objectLabelPlural}",
                key="INDEX",
                is_active=False,
            )
        for view in client.views:
            if view["id"] == original_view_id:
                view["isActive"] = False

        with self.assertRaisesRegex(
            crm.ConfigurationError, "has 0 live INDEX views out of 3"
        ):
            crm.configure_crm_objects(client, apply=True)

    def test_duplicate_view_field_mappings_fail_closed(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        view_id = client.index_view_id("customer")
        duplicate = copy.deepcopy(client.view_fields[view_id][0])
        duplicate["id"] = "customer-duplicate-view-field"
        duplicate["position"] = 99
        client.view_fields[view_id].append(duplicate)

        with self.assertRaisesRegex(
            crm.ConfigurationError, "more than one mapping for field"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_a_view_belonging_to_another_object_fails_closed(self) -> None:
        client = MisroutedViewClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        before = copy.deepcopy(client.view_fields[COMPANY_VIEW_ID])

        with self.assertRaisesRegex(
            crm.ConfigurationError, "belongs to object"
        ):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])
        self.assertEqual(client.view_fields[COMPANY_VIEW_ID], before)

    def test_mappings_from_another_view_are_ignored(self) -> None:
        client = MisroutedViewFieldClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        before = copy.deepcopy(client.view_fields[COMPANY_VIEW_ID])

        crm.configure_crm_objects(client, apply=True)

        company_view_field_ids = {item["id"] for item in before}
        for method, path, _ in client.writes:
            for view_field_id in company_view_field_ids:
                self.assertNotIn(view_field_id, path, msg=f"{method} {path}")
        self.assertEqual(client.view_fields[COMPANY_VIEW_ID], before)
        for object_name, expected in EXPECTED_INDEX_VIEW_COLUMNS.items():
            with self.subTest(object_name=object_name):
                self.assertEqual(
                    client.index_view_columns(object_name), expected
                )

    def test_a_view_without_an_id_fails_closed(self) -> None:
        client = FakeClient(
            include_objects=True, include_fields=True, include_relation=True
        )
        for view in client.views:
            if view["id"] == client.index_view_id("customer"):
                view.pop("id")
                break

        with self.assertRaisesRegex(crm.ConfigurationError, "without an id"):
            crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.writes, [])


class ResponseFormatTests(unittest.TestCase):
    def test_both_metadata_response_formats_are_tolerated(self) -> None:
        results = {}
        for response_format in RESPONSE_FORMATS:
            client = FakeClient(response_format=response_format)
            changes = crm.configure_crm_objects(client, apply=True)
            results[response_format] = (
                _change_tuples(changes),
                [(method, path, payload) for method, path, payload in client.writes],
            )

        self.assertEqual(results["new"], results["legacy"])
        self.assertEqual(results["new"][0], EXPECTED_FULL_CHANGES)

    def test_both_formats_report_the_same_dry_run(self) -> None:
        reports = [
            _change_tuples(
                crm.configure_crm_objects(
                    FakeClient(response_format=response_format), apply=False
                )
            )
            for response_format in RESPONSE_FORMATS
        ]

        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0], EXPECTED_FULL_CHANGES)

    def test_both_formats_are_idempotent(self) -> None:
        for response_format in RESPONSE_FORMATS:
            with self.subTest(response_format=response_format):
                client = FakeClient(
                    response_format=response_format,
                    include_objects=True,
                    include_fields=True,
                    include_relation=True,
                    include_view_columns=True,
                )

                self.assertEqual(
                    crm.configure_crm_objects(client, apply=True), []
                )
                self.assertEqual(client.writes, [])


class CompanyIsolationTests(unittest.TestCase):
    def _company_ids(self, client: FakeClient) -> set[str]:
        return (
            {COMPANY_ID, COMPANY_VIEW_ID}
            | {field["id"] for field in client.company["fields"]}
            | {item["id"] for item in client.view_fields[COMPANY_VIEW_ID]}
        )

    def test_company_object_and_fields_are_never_written(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        company_ids = self._company_ids(client)
        for method, path, payload in client.writes:
            serialized = f"{path} {json.dumps(payload, sort_keys=True)}"
            for company_id in company_ids:
                self.assertNotIn(company_id, serialized, msg=f"{method} {path}")
        self.assertEqual(
            [field["name"] for field in client.company["fields"]],
            ["name", "leadPhone", "salesLifecycleStatus"],
        )
        self.assertEqual(client.company["labelPlural"], "Companies")

    def test_company_lead_field_labels_are_never_changed(self) -> None:
        client = FakeClient()
        before = copy.deepcopy(client.company)

        crm.configure_crm_objects(client, apply=True)

        self.assertEqual(client.company, before)

    def test_the_company_index_view_is_never_read_or_written(self) -> None:
        client = FakeClient()
        before = copy.deepcopy(client.view_fields[COMPANY_VIEW_ID])

        crm.configure_crm_objects(client, apply=True)

        company_ids = self._company_ids(client)
        # Reads count here too: resolving views by object id must never reach
        # the Company view, whatever its name renders to.
        for method, path, payload in client.calls:
            serialized = f"{path} {json.dumps(payload, sort_keys=True)}"
            for company_id in company_ids:
                self.assertNotIn(company_id, serialized, msg=f"{method} {path}")
        self.assertEqual(client.view_fields[COMPANY_VIEW_ID], before)

    def test_only_managed_objects_can_be_resolved_for_writes(self) -> None:
        client = FakeClient(include_objects=True)
        objects_by_name = crm._managed_objects(client)

        self.assertEqual(
            sorted(objects_by_name), sorted(crm.MANAGED_OBJECT_NAMES)
        )
        with self.assertRaisesRegex(crm.ConfigurationError, "unmanaged object"):
            crm._managed_object_id({"company": client.company}, "company")

    def test_no_delete_is_ever_issued(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        self.assertFalse(any(method == "DELETE" for method, _, _ in client.calls))


class ClientPolicyTests(unittest.TestCase):
    def _client(self) -> Any:
        return crm.TwentyMetadataClient("https://twenty.example", "key", 1)

    def test_get_is_retried_on_transport_failure(self) -> None:
        client = self._client()
        with (
            mock.patch.object(
                crm, "urlopen", side_effect=URLError("boom")
            ) as opener,
            mock.patch.object(crm.time, "sleep"),
        ):
            with self.assertRaises(crm.ConfigurationError):
                client.request("GET", "/rest/metadata/objects")

        self.assertEqual(opener.call_count, crm.MAX_ATTEMPTS)

    def test_writes_are_never_retried(self) -> None:
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                client = self._client()
                with (
                    mock.patch.object(
                        crm, "urlopen", side_effect=URLError("boom")
                    ) as opener,
                    mock.patch.object(crm.time, "sleep") as sleeper,
                ):
                    with self.assertRaisesRegex(
                        crm.ConfigurationError,
                        "outcome of .* may be unknown; run a dry run",
                    ):
                        client.request(method, "/rest/metadata/fields", {})

                self.assertEqual(opener.call_count, 1)
                self.assertEqual(sleeper.call_count, 0)

    def test_delete_is_rejected_by_the_client(self) -> None:
        client = self._client()

        with self.assertRaisesRegex(crm.ConfigurationError, "not permitted"):
            client.request("DELETE", "/rest/metadata/objects/some-id")


if __name__ == "__main__":
    unittest.main()
