from __future__ import annotations

import copy
import importlib.util
import io
import json
import pathlib
import sys
import unittest
from typing import Any
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

MODULE_PATH = pathlib.Path(__file__).parents[1] / "configure_sales_workflow.py"
SPEC = importlib.util.spec_from_file_location("configure_sales_workflow", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
workflow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow
SPEC.loader.exec_module(workflow)


COMPANY_ID = "10000000-0000-0000-0000-000000000001"
INDEX_VIEW_ID = "20000000-0000-0000-0000-000000000001"
QUEUE_VIEW_ID = "20000000-0000-0000-0000-000000000002"
RECONTACT_VIEW_ID = "20000000-0000-0000-0000-000000000003"

# What Twenty renders for the Company index view today. It is a rendered string,
# never matched on, so every test that cares states the name it is standing in
# for explicitly.
DEFAULT_INDEX_VIEW_NAME = "All Companies"

# The index view and the Dashboard Priority Call Queue both carry SALES_COLUMNS.
SALES_VIEW_COUNT = 2


class FakeClient:
    def __init__(
        self,
        *,
        index_view_name: str = DEFAULT_INDEX_VIEW_NAME,
        include_sales_fields: bool = False,
        include_recontact_view: bool = False,
        include_managed_view_fields: bool = False,
        include_recontact_filters: bool = False,
        filler_object_count: int = 0,
        page_size: int | None = None,
        response_format: str = "direct",
    ) -> None:
        # Fillers sort ahead of Company, so any page_size below the total puts
        # Company on a later page.
        self.filler_objects = [
            {
                "id": f"00000000-0000-0000-0000-{index:012d}",
                "nameSingular": f"filler{index}",
                "fields": [],
            }
            for index in range(1, filler_object_count + 1)
        ]
        self.page_size = page_size
        self.response_format = response_format
        self.company = {
            "id": COMPANY_ID,
            "nameSingular": "company",
            "fields": [
                {
                    "id": "field-name",
                    "name": "name",
                    "label": "Name",
                    "type": "TEXT",
                },
                {
                    "id": "field-industry",
                    "name": "leadIndustry",
                    "label": "Lead Industry",
                    "type": "TEXT",
                },
                {
                    "id": "field-lead-phone",
                    "name": "leadPhone",
                    "label": "Lead Phone",
                    "type": "TEXT",
                },
                {
                    "id": "field-lead-email",
                    "name": "leadEmail",
                    "label": "Lead Email",
                    "type": "TEXT",
                },
                {
                    "id": "field-lead-quality-tier",
                    "name": "leadQualityTier",
                    "label": "Lead Quality Tier",
                    "type": "SELECT",
                },
            ],
        }
        self.views = [
            {
                "id": INDEX_VIEW_ID,
                "name": index_view_name,
                "key": workflow.INDEX_VIEW_KEY,
                "isActive": True,
                "objectMetadataId": COMPANY_ID,
            },
            {
                "id": QUEUE_VIEW_ID,
                "name": workflow.QUEUE_VIEW_NAME,
                "key": None,
                "isActive": True,
                "objectMetadataId": COMPANY_ID,
            },
        ]
        self.view_fields = {
            view["id"]: [
                {
                    "id": f"view-field-name-{view['id']}",
                    "fieldMetadataId": "field-name",
                    "isVisible": True,
                    "position": 0,
                    "size": 220,
                },
                {
                    "id": f"view-field-industry-{view['id']}",
                    "fieldMetadataId": "field-industry",
                    "isVisible": True,
                    "position": 6,
                    "size": 220,
                },
            ]
            for view in self.views
        }
        self.view_filters: dict[str, list[dict[str, Any]]] = {
            view["id"]: [] for view in self.views
        }
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        if include_sales_fields:
            self._add_sales_fields()
        if include_recontact_view:
            self._add_recontact_view()
        if include_managed_view_fields:
            self._add_managed_view_fields()
        if include_recontact_filters:
            self._add_recontact_filters()

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
            return copy.deepcopy(self.views)
        if method == "GET" and path.startswith("/rest/metadata/viewFields?"):
            view_id = path.split("viewId=", 1)[1]
            return copy.deepcopy(self.view_fields[view_id])
        if method == "GET" and path.startswith("/rest/metadata/viewFilters?"):
            view_id = path.split("viewId=", 1)[1]
            return copy.deepcopy(self.view_filters[view_id])
        if method == "POST" and path == "/rest/metadata/fields":
            assert payload is not None
            field = copy.deepcopy(payload)
            field["id"] = f"field-{field['name']}"
            self.company["fields"].append(field)
            return field
        if method == "POST" and path == "/rest/metadata/viewFields":
            assert payload is not None
            view_field = copy.deepcopy(payload)
            view_field["id"] = (
                f"view-field-{payload['viewId']}-{payload['fieldMetadataId']}"
            )
            self.view_fields[payload["viewId"]].append(view_field)
            return view_field
        if method == "POST" and path == "/rest/metadata/views":
            assert payload is not None
            view = copy.deepcopy(payload)
            view["id"] = RECONTACT_VIEW_ID
            self.views.append(view)
            self.view_fields[view["id"]] = []
            self.view_filters[view["id"]] = []
            return copy.deepcopy(view)
        if method == "POST" and path == "/rest/metadata/viewFilters":
            assert payload is not None
            view_filter = copy.deepcopy(payload)
            view_filter["id"] = (
                f"view-filter-{payload['viewId']}-{payload['fieldMetadataId']}"
            )
            self.view_filters[payload["viewId"]].append(view_filter)
            return copy.deepcopy(view_filter)
        if method == "PATCH" and path.startswith("/rest/metadata/viewFields/"):
            assert payload is not None
            view_field_id = path.rsplit("/", 1)[-1]
            target = next(
                item
                for items in self.view_fields.values()
                for item in items
                if item["id"] == view_field_id
            )
            target.update(payload)
            return copy.deepcopy(target)
        if method == "PATCH" and path.startswith("/rest/metadata/viewFilters/"):
            assert payload is not None
            view_filter_id = path.rsplit("/", 1)[-1]
            target = next(
                item
                for items in self.view_filters.values()
                for item in items
                if item["id"] == view_filter_id
            )
            target.update(payload)
            return copy.deepcopy(target)
        raise AssertionError(f"unexpected request: {method} {path}")

    def _objects_response(self, path: str) -> Any:
        ordered = [*self.filler_objects, copy.deepcopy(self.company)]
        if self.page_size is None:
            # The unpaginated shape the workspace returns today: one page and
            # no pageInfo at all.
            return {"data": ordered}

        query = parse_qs(urlsplit(path).query)
        limit = min(
            int(query.get("limit", [str(workflow.OBJECT_PAGE_LIMIT)])[0]),
            self.page_size,
        )
        cursor = query.get("starting_after", [None])[0]
        if cursor is not None:
            ids = [item["id"] for item in ordered]
            if cursor not in ids:
                raise AssertionError(f"unknown cursor: {cursor}")
            ordered = ordered[ids.index(cursor) + 1 :]
        page = ordered[:limit]
        page_info = {
            "hasNextPage": len(ordered) > len(page),
            "startCursor": page[0]["id"] if page else None,
            "endCursor": page[-1]["id"] if page else None,
        }
        if self.response_format == "legacy":
            return {"data": {"objects": page}, "pageInfo": page_info}
        return {"data": page, "pageInfo": page_info}

    def object_request_count(self) -> int:
        return len(
            [
                path
                for method, path, _ in self.calls
                if method == "GET" and path.startswith("/rest/metadata/objects")
            ]
        )

    def _add_sales_fields(self) -> None:
        existing_names = {field["name"] for field in self.company["fields"]}
        for definition in workflow.SALES_FIELDS:
            if definition["name"] in existing_names:
                continue
            field = copy.deepcopy(definition)
            field["id"] = f"field-{definition['name']}"
            self.company["fields"].append(field)

    def _add_recontact_view(self) -> None:
        if any(view["name"] == workflow.RECONTACT_VIEW_NAME for view in self.views):
            return
        view = {
            "id": RECONTACT_VIEW_ID,
            "name": workflow.RECONTACT_VIEW_NAME,
            "key": None,
            "isActive": True,
            "objectMetadataId": COMPANY_ID,
        }
        self.views.append(view)
        self.view_fields[view["id"]] = []
        self.view_filters[view["id"]] = []

    @property
    def index_view(self) -> dict[str, Any]:
        return next(
            view
            for view in self.views
            if view.get("key") == workflow.INDEX_VIEW_KEY
        )

    def view_named(self, name: str) -> dict[str, Any]:
        return next(view for view in self.views if view["name"] == name)

    def field_by_name(self, name: str) -> dict[str, Any]:
        return next(field for field in self.company["fields"] if field["name"] == name)

    def _add_managed_view_fields(self) -> None:
        fields = {field["name"]: field for field in self.company["fields"]}
        for view in self.views:
            columns = (
                workflow.RECONTACT_COLUMNS
                if view["name"] == workflow.RECONTACT_VIEW_NAME
                else workflow.SALES_COLUMNS
            )
            first_position = 0 if view["name"] == workflow.RECONTACT_VIEW_NAME else 7
            for position, column in enumerate(columns, start=first_position):
                self.view_fields[view["id"]].append(
                    {
                        "id": f"view-field-{view['id']}-{column['name']}",
                        "fieldMetadataId": fields[column["name"]]["id"],
                        "isVisible": True,
                        "position": position,
                        "size": column["size"],
                    }
                )

    def _add_recontact_filters(self) -> None:
        fields = {field["name"]: field for field in self.company["fields"]}
        view = next(
            item for item in self.views if item["name"] == workflow.RECONTACT_VIEW_NAME
        )
        for definition in workflow.RECONTACT_FILTERS:
            field = fields[definition["field"]]
            self.view_filters[view["id"]].append(
                {
                    "id": f"view-filter-{view['id']}-{field['id']}",
                    "viewId": view["id"],
                    "fieldMetadataId": field["id"],
                    "operand": definition["operand"],
                    "value": copy.deepcopy(definition["value"]),
                }
            )


class ConfigureSalesWorkflowTests(unittest.TestCase):
    def test_dry_run_reports_changes_without_writes(self) -> None:
        client = FakeClient()

        changes = workflow.configure_sales_workflow(client, apply=False)

        self.assertEqual(
            {change.name for change in changes if change.resource == "field"},
            {definition["name"] for definition in workflow.SALES_FIELDS},
        )
        self.assertEqual(
            [
                (change.details["view"], change.name)
                for change in changes
                if change.resource == "viewField"
            ],
            [
                (view_name, column["name"])
                for view_name in (DEFAULT_INDEX_VIEW_NAME, workflow.QUEUE_VIEW_NAME)
                for column in workflow.SALES_COLUMNS
            ]
            + [
                (workflow.RECONTACT_VIEW_NAME, column["name"])
                for column in workflow.RECONTACT_COLUMNS
            ],
        )
        self.assertEqual(
            [change.name for change in changes if change.resource == "view"],
            [workflow.RECONTACT_VIEW_NAME],
        )
        self.assertEqual(
            [change.name for change in changes if change.resource == "viewFilter"],
            [definition["field"] for definition in workflow.RECONTACT_FILTERS],
        )
        self.assertFalse(any(method != "GET" for method, _, _ in client.calls))

    def test_apply_creates_fields_and_appends_only_requested_columns(self) -> None:
        client = FakeClient()

        changes = workflow.configure_sales_workflow(client, apply=True)

        writes = [call for call in client.calls if call[0] != "GET"]
        self.assertEqual(
            len([call for call in writes if call[1] == "/rest/metadata/fields"]),
            len(workflow.SALES_FIELDS),
        )
        view_field_creates = [
            call for call in writes if call[1] == "/rest/metadata/viewFields"
        ]
        self.assertEqual(
            len(view_field_creates),
            SALES_VIEW_COUNT * len(workflow.SALES_COLUMNS)
            + len(workflow.RECONTACT_COLUMNS),
        )
        self.assertEqual(
            len([call for call in writes if call[1] == "/rest/metadata/viewFilters"]),
            len(workflow.RECONTACT_FILTERS),
        )
        existing_view_ids = {INDEX_VIEW_ID, QUEUE_VIEW_ID}
        for view_id, items in client.view_fields.items():
            if view_id not in existing_view_ids:
                continue
            self.assertEqual(items[0]["position"], 0)
            self.assertEqual(items[1]["position"], 6)
        self.assertTrue(changes)

    def test_repeated_apply_is_idempotent(self) -> None:
        client = FakeClient(
            include_sales_fields=True,
            include_recontact_view=True,
            include_managed_view_fields=True,
            include_recontact_filters=True,
        )

        changes = workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(changes, [])
        self.assertFalse(any(method != "GET" for method, _, _ in client.calls))

    def test_existing_target_mapping_is_updated_without_duplication(self) -> None:
        client = FakeClient(
            include_sales_fields=True,
            include_recontact_view=True,
        )
        actioned = next(
            field
            for field in client.company["fields"]
            if field["name"] == "salesActioned"
        )
        queue_view = next(
            view
            for view in client.views
            if view["name"] == "Dashboard Priority Call Queue"
        )
        client.view_fields[queue_view["id"]].append(
            {
                "id": "existing-actioned",
                "fieldMetadataId": actioned["id"],
                "isVisible": False,
                "position": 99,
                "size": 20,
            }
        )

        workflow.configure_sales_workflow(client, apply=True)

        patches = [
            call
            for call in client.calls
            if call[0] == "PATCH" and call[1].endswith("/existing-actioned")
        ]
        self.assertEqual(len(patches), 1)
        self.assertEqual(
            patches[0][2],
            {"isVisible": True, "position": 7, "size": 110},
        )
        self.assertEqual(
            sum(
                item["fieldMetadataId"] == actioned["id"]
                for item in client.view_fields[queue_view["id"]]
            ),
            1,
        )

    def test_hidden_standard_mappings_are_made_visible(self) -> None:
        client = FakeClient(
            include_sales_fields=True,
            include_recontact_view=True,
            include_managed_view_fields=True,
            include_recontact_filters=True,
        )
        standard_view = client.index_view
        target_names = {column["name"] for column in workflow.SALES_COLUMNS}
        fields_by_id = {
            field["id"]: field["name"] for field in client.company["fields"]
        }
        for item in client.view_fields[standard_view["id"]]:
            if fields_by_id.get(item["fieldMetadataId"]) in target_names:
                item["isVisible"] = False

        changes = workflow.configure_sales_workflow(client, apply=True)

        standard_changes = [
            change
            for change in changes
            if change.resource == "viewField"
            and change.details.get("viewKey") == workflow.INDEX_VIEW_KEY
        ]
        self.assertEqual(
            [change.name for change in standard_changes],
            [column["name"] for column in workflow.SALES_COLUMNS],
        )
        self.assertTrue(
            all(
                change.details
                == {
                    "view": DEFAULT_INDEX_VIEW_NAME,
                    "viewKey": workflow.INDEX_VIEW_KEY,
                    "isVisible": True,
                }
                for change in standard_changes
            )
        )

    def test_field_type_drift_fails_before_writes(self) -> None:
        client = FakeClient(include_sales_fields=True)
        actioned = next(
            field
            for field in client.company["fields"]
            if field["name"] == "salesActioned"
        )
        actioned["type"] = "TEXT"

        with self.assertRaisesRegex(workflow.ConfigurationError, "has type"):
            workflow.configure_sales_workflow(client, apply=True)

        self.assertFalse(any(method != "GET" for method, _, _ in client.calls))

    def test_recontact_filter_drift_is_repaired_without_deletes(self) -> None:
        client = FakeClient(
            include_sales_fields=True,
            include_recontact_view=True,
            include_managed_view_fields=True,
            include_recontact_filters=True,
        )
        recontact_view = next(
            view
            for view in client.views
            if view["name"] == workflow.RECONTACT_VIEW_NAME
        )
        recontact_filter = next(
            item
            for item in client.view_filters[recontact_view["id"]]
            if item["operand"] == "IS_IN_PAST"
        )
        recontact_filter["operand"] = "IS_IN_FUTURE"

        changes = workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(
            [(change.resource, change.name, change.action) for change in changes],
            [("viewFilter", "recontactAt", "update")],
        )
        self.assertEqual(recontact_filter["operand"], "IS_IN_PAST")
        self.assertFalse(any(method == "DELETE" for method, _, _ in client.calls))

    def test_duplicate_recontact_filters_fail_closed(self) -> None:
        client = FakeClient(
            include_sales_fields=True,
            include_recontact_view=True,
            include_managed_view_fields=True,
            include_recontact_filters=True,
        )
        recontact_view = next(
            view
            for view in client.views
            if view["name"] == workflow.RECONTACT_VIEW_NAME
        )
        duplicate = copy.deepcopy(client.view_filters[recontact_view["id"]][0])
        duplicate["id"] += "-duplicate"
        client.view_filters[recontact_view["id"]].append(duplicate)

        with self.assertRaisesRegex(workflow.ConfigurationError, "duplicate filters"):
            workflow.configure_sales_workflow(client, apply=True)

        self.assertFalse(any(method != "GET" for method, _, _ in client.calls))


def _drop_field(client: FakeClient, name: str) -> FakeClient:
    client.company["fields"] = [
        field for field in client.company["fields"] if field["name"] != name
    ]
    return client


def _writes(client: FakeClient) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [call for call in client.calls if call[0] != "GET"]


def _index_view_changes(changes: list[Any]) -> list[Any]:
    return [
        change
        for change in changes
        if change.details.get("viewKey") == workflow.INDEX_VIEW_KEY
    ]


def _label_agnostic(changes: list[Any]) -> list[tuple[Any, ...]]:
    # The index view's reported label is a rendered string. Every other part of
    # every change must come out identical whatever that string happens to be.
    normalized = []
    for change in changes:
        details = dict(change.details)
        if details.get("viewKey") == workflow.INDEX_VIEW_KEY:
            details["view"] = workflow.INDEX_VIEW_KEY
        normalized.append(
            (
                change.action,
                change.resource,
                change.name,
                sorted(details.items(), key=lambda item: item[0]),
            )
        )
    return normalized


# A live field whose isNullable or defaultValue has drifted from SALES_FIELDS
# behaves differently at runtime than the definition this configurator reports
# as converged, so the drift has to stop the run before any write. Nothing here
# repairs a drifted field: the fix is an operator decision, not this script's.
class ExistingFieldDriftTests(unittest.TestCase):
    MODES = (False, True)

    # One case per shape the check has to cover: a nullable SELECT carrying a
    # default, a non-nullable SELECT, a BOOLEAN whose default is False, and
    # nullable fields declared without a default at all.
    DRIFT_CASES = (
        ("callStatus", "isNullable", False),
        ("callStatus", "defaultValue", "'CONNECTED'"),
        ("salesLifecycleStatus", "isNullable", True),
        ("salesLifecycleStatus", "defaultValue", "'WORKING'"),
        ("salesActioned", "isNullable", True),
        ("salesActioned", "defaultValue", True),
        ("byronReviewed", "defaultValue", True),
        ("callAttempts", "isNullable", False),
        ("salesDisposition", "isNullable", False),
        ("recontactAt", "isNullable", False),
    )

    NO_DEFAULT_FIELD_NAMES = tuple(
        definition["name"]
        for definition in workflow.SALES_FIELDS
        if "defaultValue" not in definition
    )

    def _pending_client(self) -> FakeClient:
        # The sales fields exist, but the Recontact view and every managed
        # column do not: this workspace has writes queued behind the check.
        return FakeClient(include_sales_fields=True)

    def _reconciled_client(self) -> FakeClient:
        return FakeClient(
            include_sales_fields=True,
            include_recontact_view=True,
            include_managed_view_fields=True,
            include_recontact_filters=True,
        )

    def test_drift_fails_the_same_way_in_both_modes_before_any_write(self) -> None:
        for name, key, drifted in self.DRIFT_CASES:
            messages = []
            for apply in self.MODES:
                with self.subTest(field=name, key=key, apply=apply):
                    client = self._pending_client()
                    client.field_by_name(name)[key] = drifted

                    with self.assertRaises(workflow.ConfigurationError) as caught:
                        workflow.configure_sales_workflow(client, apply=apply)

                    self.assertIn(
                        f"Twenty field {name} has {key} {drifted!r}, expected ",
                        str(caught.exception),
                    )
                    self.assertEqual(_writes(client), [])
                    messages.append(str(caught.exception))

            self.assertEqual(messages[0], messages[1], msg=f"{name}.{key}")

    def test_drift_fails_on_an_otherwise_reconciled_workspace(self) -> None:
        # The workspace this bug is about: everything else already converges, so
        # without the check the run reports zero changes over a drifted field.
        for name, key, drifted in self.DRIFT_CASES:
            for apply in self.MODES:
                with self.subTest(field=name, key=key, apply=apply):
                    client = self._reconciled_client()
                    client.field_by_name(name)[key] = drifted

                    with self.assertRaisesRegex(
                        workflow.ConfigurationError, f"Twenty field {name} has {key} "
                    ):
                        workflow.configure_sales_workflow(client, apply=apply)

                    self.assertEqual(_writes(client), [])

    def test_a_field_declared_without_a_default_must_not_carry_one(self) -> None:
        self.assertIn("salesDisposition", self.NO_DEFAULT_FIELD_NAMES)
        for name in self.NO_DEFAULT_FIELD_NAMES:
            for apply in self.MODES:
                with self.subTest(field=name, apply=apply):
                    client = self._pending_client()
                    client.field_by_name(name)["defaultValue"] = "'DRIFTED'"

                    with self.assertRaisesRegex(
                        workflow.ConfigurationError,
                        f"Twenty field {name} has defaultValue",
                    ):
                        workflow.configure_sales_workflow(client, apply=apply)

                    self.assertEqual(_writes(client), [])

    def test_a_null_default_reads_the_same_as_an_absent_one(self) -> None:
        for apply in self.MODES:
            with self.subTest(apply=apply):
                client = self._reconciled_client()
                for name in self.NO_DEFAULT_FIELD_NAMES:
                    client.field_by_name(name)["defaultValue"] = None

                self.assertEqual(
                    workflow.configure_sales_workflow(client, apply=apply), []
                )
                self.assertEqual(_writes(client), [])

    def test_a_dropped_default_is_drift_rather_than_a_match(self) -> None:
        # A BOOLEAN default of False is not the same as no default: reading an
        # absent key as falsey would let a field that lost its default pass.
        for apply in self.MODES:
            with self.subTest(apply=apply):
                client = self._pending_client()
                del client.field_by_name("salesActioned")["defaultValue"]

                with self.assertRaisesRegex(
                    workflow.ConfigurationError,
                    "Twenty field salesActioned has defaultValue None, "
                    "expected False",
                ):
                    workflow.configure_sales_workflow(client, apply=apply)

                self.assertEqual(_writes(client), [])

    def test_matching_fields_are_accepted_unchanged_in_both_modes(self) -> None:
        for apply in self.MODES:
            with self.subTest(apply=apply):
                client = self._reconciled_client()

                self.assertEqual(
                    workflow.configure_sales_workflow(client, apply=apply), []
                )
                self.assertEqual(_writes(client), [])

    def test_a_matching_field_does_not_block_the_creation_of_a_missing_one(
        self,
    ) -> None:
        client = self._pending_client()
        _drop_field(client, "recontactAt")

        changes = workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(
            [
                change.name
                for change in changes
                if change.resource == "field" and change.action == "create"
            ],
            ["recontactAt"],
        )
        created = client.field_by_name("recontactAt")
        self.assertEqual(created["isNullable"], True)
        self.assertNotIn("defaultValue", created)


class ScriptedObjectClient:
    """Serves only GET /rest/metadata/objects, one scripted page per request.

    Anything else is a failure: every case below has to stop inside the object
    lookup, before the configurator reaches a view or a write.
    """

    def __init__(self, page_for_index: Any) -> None:
        self.page_for_index = page_for_index
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method != "GET" or not path.startswith("/rest/metadata/objects"):
            raise AssertionError(f"unexpected request: {method} {path}")
        return copy.deepcopy(self.page_for_index(len(self.calls) - 1))


COMPANY_OBJECT = {
    "id": "10000000-0000-0000-0000-000000000001",
    "nameSingular": "company",
    "fields": [],
}


def _object_page(
    items: list[dict[str, Any]],
    *,
    has_next_page: bool,
    end_cursor: Any = "cursor",
) -> dict[str, Any]:
    page_info: dict[str, Any] = {"hasNextPage": has_next_page}
    if end_cursor is not ...:
        page_info["endCursor"] = end_cursor
    return {"data": items, "pageInfo": page_info}


# Company is resolved from a paginated listing. Fetching only the first page
# let a workspace with enough objects report Company missing when it was merely
# further down, so the cursor is followed — under a bound, and refusing any
# page sequence that cannot be trusted to terminate.
class ObjectPaginationTests(unittest.TestCase):
    def test_company_on_a_later_page_is_configured_identically(self) -> None:
        paginated = FakeClient(filler_object_count=5, page_size=2)

        changes = workflow.configure_sales_workflow(paginated, apply=True)

        self.assertEqual(
            changes, workflow.configure_sales_workflow(FakeClient(), apply=True)
        )
        self.assertTrue(
            any("starting_after" in path for _, path, _ in paginated.calls)
        )

    def test_company_on_a_later_page_of_the_legacy_response_shape(self) -> None:
        paginated = FakeClient(
            filler_object_count=5, page_size=2, response_format="legacy"
        )

        changes = workflow.configure_sales_workflow(paginated, apply=True)

        self.assertEqual(
            changes, workflow.configure_sales_workflow(FakeClient(), apply=True)
        )

    def test_a_first_page_hit_stays_a_single_request(self) -> None:
        client = FakeClient(filler_object_count=5, page_size=200)

        workflow.configure_sales_workflow(client, apply=False)

        self.assertEqual(client.object_request_count(), 1)
        self.assertFalse(any("starting_after" in path for _, path, _ in client.calls))

    def test_a_listing_without_page_info_is_read_as_a_single_page(self) -> None:
        client = FakeClient()

        workflow.configure_sales_workflow(client, apply=False)

        self.assertEqual(client.object_request_count(), 1)

    def test_company_is_used_before_the_rest_of_the_listing_is_judged(self) -> None:
        # Company answered the question on page one. A server that then
        # describes its next page incoherently cannot retroactively fail a
        # lookup that already succeeded.
        client = ScriptedObjectClient(
            lambda index: _object_page(
                [COMPANY_OBJECT], has_next_page=True, end_cursor=None
            )
        )

        self.assertEqual(
            workflow._company_metadata(client)["nameSingular"], "company"
        )
        self.assertEqual(len(client.calls), 1)

    def test_a_terminated_listing_without_company_reports_company_missing(
        self,
    ) -> None:
        pages = [
            _object_page([{"nameSingular": "person"}], has_next_page=True),
            _object_page([{"nameSingular": "note"}], has_next_page=False),
        ]
        client = ScriptedObjectClient(lambda index: pages[index])

        with self.assertRaisesRegex(
            workflow.ConfigurationError, "Company object was not found"
        ):
            workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(_write_calls(client), [])


class PaginationFailsClosedTests(unittest.TestCase):
    MALFORMED_CURSORS = {
        "null": None,
        "absent": ...,
        "empty": "",
        "not a string": 7,
        "a list": ["cursor"],
    }

    def test_a_page_promising_a_successor_it_cannot_name_fails_closed(self) -> None:
        for label, end_cursor in self.MALFORMED_CURSORS.items():
            with self.subTest(cursor=label):
                client = ScriptedObjectClient(
                    lambda index: _object_page(
                        [{"nameSingular": "person"}],
                        has_next_page=True,
                        end_cursor=end_cursor,
                    )
                )

                with self.assertRaisesRegex(
                    workflow.ConfigurationError, "pagination did not advance"
                ):
                    workflow.configure_sales_workflow(client, apply=True)

                self.assertEqual(len(client.calls), 1)
                self.assertEqual(_write_calls(client), [])

    def test_a_cursor_that_stands_still_fails_closed(self) -> None:
        client = ScriptedObjectClient(
            lambda index: _object_page(
                [{"nameSingular": "person"}], has_next_page=True, end_cursor="stuck"
            )
        )

        with self.assertRaisesRegex(
            workflow.ConfigurationError, "pagination repeated a page"
        ):
            workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(_write_calls(client), [])

    def test_a_cursor_cycle_fails_closed_rather_than_looping(self) -> None:
        cycle = ("a", "b", "a")
        client = ScriptedObjectClient(
            lambda index: _object_page(
                [{"nameSingular": "person"}],
                has_next_page=True,
                end_cursor=cycle[index],
            )
        )

        with self.assertRaisesRegex(
            workflow.ConfigurationError, "pagination repeated a page"
        ):
            workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(len(client.calls), 3)
        self.assertEqual(_write_calls(client), [])

    def test_an_endless_listing_is_bounded(self) -> None:
        client = ScriptedObjectClient(
            lambda index: _object_page(
                [{"nameSingular": "person"}],
                has_next_page=True,
                end_cursor=f"cursor-{index}",
            )
        )

        with self.assertRaisesRegex(
            workflow.ConfigurationError,
            f"exceeded {workflow.MAX_OBJECT_PAGES} pages",
        ):
            workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(len(client.calls), workflow.MAX_OBJECT_PAGES)
        self.assertEqual(_write_calls(client), [])

    def test_an_unreadable_page_fails_closed(self) -> None:
        pages = [
            _object_page([{"nameSingular": "person"}], has_next_page=True),
            {"data": {"unexpected": "shape"}},
        ]
        client = ScriptedObjectClient(lambda index: pages[index])

        with self.assertRaisesRegex(
            workflow.ConfigurationError, "did not contain a list"
        ):
            workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(_write_calls(client), [])


def _write_calls(
    client: ScriptedObjectClient,
) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [call for call in client.calls if call[0] != "GET"]


# The Recontact Due view depends on the standard `name` and on the
# enrichment-owned lead fields. This configurator must not create any of them,
# so their absence has to stop the run before it writes anything.
class PreflightDependencyTests(unittest.TestCase):
    MODES = (False, True)

    def test_dependencies_are_the_managed_fields_this_script_does_not_own(
        self,
    ) -> None:
        self.assertEqual(
            workflow.DEPENDENCY_FIELD_NAMES,
            ("name", "leadPhone", "leadEmail", "leadQualityTier"),
        )
        self.assertFalse(
            set(workflow.DEPENDENCY_FIELD_NAMES) & workflow.OWNED_FIELD_NAMES
        )
        managed = {
            column["name"]
            for column in (*workflow.SALES_COLUMNS, *workflow.RECONTACT_COLUMNS)
        } | {definition["field"] for definition in workflow.RECONTACT_FILTERS}
        self.assertEqual(
            managed - workflow.OWNED_FIELD_NAMES,
            set(workflow.DEPENDENCY_FIELD_NAMES),
        )

    def test_a_missing_dependency_fails_the_same_way_in_both_modes(self) -> None:
        for name in workflow.DEPENDENCY_FIELD_NAMES:
            messages = []
            for apply in self.MODES:
                with self.subTest(field=name, apply=apply):
                    client = _drop_field(FakeClient(), name)

                    with self.assertRaises(workflow.ConfigurationError) as caught:
                        workflow.configure_sales_workflow(client, apply=apply)

                    self.assertIn(name, str(caught.exception))
                    self.assertIn("missing fields required by", str(caught.exception))
                    self.assertEqual(_writes(client), [])
                    messages.append(str(caught.exception))

            self.assertEqual(messages[0], messages[1], msg=name)

    def test_a_missing_dependency_fails_on_an_otherwise_reconciled_workspace(
        self,
    ) -> None:
        for name in workflow.DEPENDENCY_FIELD_NAMES:
            for apply in self.MODES:
                with self.subTest(field=name, apply=apply):
                    client = _drop_field(
                        FakeClient(
                            include_sales_fields=True,
                            include_recontact_view=True,
                            include_managed_view_fields=True,
                            include_recontact_filters=True,
                        ),
                        name,
                    )

                    with self.assertRaisesRegex(
                        workflow.ConfigurationError, "missing fields required by"
                    ):
                        workflow.configure_sales_workflow(client, apply=apply)

                    self.assertEqual(_writes(client), [])

    def test_every_missing_dependency_is_reported_at_once(self) -> None:
        client = FakeClient()
        for name in workflow.DEPENDENCY_FIELD_NAMES:
            _drop_field(client, name)

        with self.assertRaises(workflow.ConfigurationError) as caught:
            workflow.configure_sales_workflow(client, apply=True)

        self.assertIn(", ".join(workflow.DEPENDENCY_FIELD_NAMES), str(caught.exception))
        self.assertEqual(_writes(client), [])

    def test_an_owned_field_is_created_rather_than_required(self) -> None:
        client = FakeClient(include_sales_fields=True)
        _drop_field(client, "recontactAt")

        changes = workflow.configure_sales_workflow(client, apply=True)

        self.assertIn(
            ("create", "field", "recontactAt"),
            [(change.action, change.resource, change.name) for change in changes],
        )

    def test_a_complete_workspace_is_unaffected_by_the_preflight(self) -> None:
        dry_run_client = FakeClient()

        changes = workflow.configure_sales_workflow(dry_run_client, apply=False)

        self.assertEqual(
            [
                (change.action, change.resource, change.name)
                for change in changes
                if change.resource == "field"
            ],
            [
                ("create", "field", definition["name"])
                for definition in workflow.SALES_FIELDS
            ],
        )
        self.assertEqual(_writes(dry_run_client), [])
        # The preflight reuses the Company metadata already in hand; it must not
        # add a read of its own.
        self.assertEqual(
            len(
                [
                    call
                    for call in dry_run_client.calls
                    if call[1].startswith("/rest/metadata/objects")
                ]
            ),
            1,
        )

        reconciled_client = FakeClient(
            include_sales_fields=True,
            include_recontact_view=True,
            include_managed_view_fields=True,
            include_recontact_filters=True,
        )

        self.assertEqual(
            workflow.configure_sales_workflow(reconciled_client, apply=True), []
        )
        self.assertEqual(_writes(reconciled_client), [])


# Twenty stores the Company index view's name as `All {objectLabelPlural}` and
# renders it on every read, so relabelling Company to Lead — or running under any
# other workspace locale — changes the name that comes back. The view is matched
# on `key == "INDEX"` so that none of this reaches the write paths.
class IndexViewResolutionTests(unittest.TestCase):
    MODES = (False, True)

    RENDERED_NAMES = (
        "All Companies",
        "All Leads",
        "Alle Leads",
        "Tous les prospects",
        "すべてのリード",
    )

    OTHER_VIEW_ID = "20000000-0000-0000-0000-0000000000ff"

    def test_the_index_view_is_reconciled_after_company_is_relabelled_lead(
        self,
    ) -> None:
        client = FakeClient(index_view_name="All Leads")

        changes = workflow.configure_sales_workflow(client, apply=True)

        index_changes = _index_view_changes(changes)
        self.assertEqual(
            [change.name for change in index_changes],
            [column["name"] for column in workflow.SALES_COLUMNS],
        )
        # The rendered name is reported, because that is what the operator sees
        # in the UI, but it played no part in finding the view.
        self.assertTrue(
            all(change.details["view"] == "All Leads" for change in index_changes)
        )
        posted = [
            call
            for call in _writes(client)
            if call[1] == "/rest/metadata/viewFields"
            and call[2] is not None
            and call[2]["viewId"] == INDEX_VIEW_ID
        ]
        self.assertEqual(len(posted), len(workflow.SALES_COLUMNS))

    def test_writes_are_identical_under_any_rendered_name(self) -> None:
        baseline = FakeClient(index_view_name=DEFAULT_INDEX_VIEW_NAME)
        workflow.configure_sales_workflow(baseline, apply=True)

        for name in self.RENDERED_NAMES:
            with self.subTest(name=name):
                client = FakeClient(index_view_name=name)

                workflow.configure_sales_workflow(client, apply=True)

                self.assertEqual(_writes(client), _writes(baseline))

    def test_reported_changes_differ_only_by_the_rendered_label(self) -> None:
        baseline = _label_agnostic(
            workflow.configure_sales_workflow(
                FakeClient(index_view_name=DEFAULT_INDEX_VIEW_NAME), apply=False
            )
        )

        for name in self.RENDERED_NAMES:
            with self.subTest(name=name):
                changes = workflow.configure_sales_workflow(
                    FakeClient(index_view_name=name), apply=False
                )

                self.assertEqual(_label_agnostic(changes), baseline)

    def test_an_index_view_without_a_rendered_name_is_still_reconciled(self) -> None:
        for case, mutate in (
            ("absent", lambda view: view.pop("name")),
            ("empty", lambda view: view.update({"name": ""})),
            ("not a string", lambda view: view.update({"name": None})),
        ):
            with self.subTest(case=case):
                client = FakeClient()
                mutate(client.index_view)

                changes = workflow.configure_sales_workflow(client, apply=True)

                index_changes = _index_view_changes(changes)
                self.assertEqual(
                    [change.name for change in index_changes],
                    [column["name"] for column in workflow.SALES_COLUMNS],
                )
                self.assertTrue(
                    all(
                        change.details["view"] == workflow.INDEX_VIEW_DESCRIPTION
                        for change in index_changes
                    )
                )

    def test_a_missing_index_view_fails_closed_before_any_write(self) -> None:
        for case, mutate in (
            ("no index view at all", lambda client: client.views.remove(
                client.index_view
            )),
            ("key absent", lambda client: client.index_view.pop("key")),
            ("key renamed", lambda client: client.index_view.update({"key": "CUSTOM"})),
            ("deactivated", lambda client: client.index_view.update(
                {"isActive": False}
            )),
            ("soft deleted", lambda client: client.index_view.update(
                {"deletedAt": "2026-07-29T00:00:00.000Z"}
            )),
        ):
            for apply in self.MODES:
                with self.subTest(case=case, apply=apply):
                    # No sales field exists yet, so an apply that did not fail
                    # closed first would have created ten of them.
                    client = FakeClient()
                    mutate(client)

                    with self.assertRaisesRegex(
                        workflow.ConfigurationError,
                        f"0 live {workflow.INDEX_VIEW_KEY} views out of",
                    ):
                        workflow.configure_sales_workflow(client, apply=apply)

                    self.assertEqual(_writes(client), [])

    def test_a_duplicate_live_index_view_fails_closed_before_any_write(self) -> None:
        for apply in self.MODES:
            with self.subTest(apply=apply):
                client = FakeClient()
                duplicate = copy.deepcopy(client.index_view)
                duplicate["id"] = self.OTHER_VIEW_ID
                duplicate["name"] = "All Leads"
                client.views.append(duplicate)

                with self.assertRaisesRegex(
                    workflow.ConfigurationError,
                    f"2 live {workflow.INDEX_VIEW_KEY} views out of 2",
                ):
                    workflow.configure_sales_workflow(client, apply=apply)

                self.assertEqual(_writes(client), [])

    def test_a_deactivated_index_view_beside_a_live_one_is_ignored(self) -> None:
        client = FakeClient()
        stale = copy.deepcopy(client.index_view)
        stale["id"] = self.OTHER_VIEW_ID
        stale["isActive"] = False
        client.views.append(stale)
        client.view_fields[stale["id"]] = []

        workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(client.view_fields[stale["id"]], [])
        self.assertFalse(
            any(self.OTHER_VIEW_ID in call[1] for call in client.calls)
        )
        self.assertEqual(
            len(client.view_fields[INDEX_VIEW_ID]), 2 + len(workflow.SALES_COLUMNS)
        )

    def test_an_index_view_belonging_to_another_object_fails_closed(self) -> None:
        for apply in self.MODES:
            with self.subTest(apply=apply):
                client = FakeClient()
                client.index_view["objectMetadataId"] = "10000000-0000-0000-0000-00ff"

                with self.assertRaisesRegex(
                    workflow.ConfigurationError, "belongs to object"
                ):
                    workflow.configure_sales_workflow(client, apply=apply)

                self.assertEqual(_writes(client), [])

    def test_an_index_view_without_an_id_fails_closed(self) -> None:
        for apply in self.MODES:
            with self.subTest(apply=apply):
                client = FakeClient()
                client.index_view.pop("id")

                with self.assertRaisesRegex(
                    workflow.ConfigurationError, "without an id"
                ):
                    workflow.configure_sales_workflow(client, apply=apply)

                self.assertEqual(_writes(client), [])


# Dashboard Priority Call Queue and Recontact Due are operator-created, so their
# names are stored verbatim and stay the right way to find them.
class NamedViewResolutionTests(unittest.TestCase):
    MODES = (False, True)

    OTHER_VIEW_ID = "20000000-0000-0000-0000-0000000000fe"

    def test_a_missing_queue_view_fails_closed_before_any_write(self) -> None:
        for apply in self.MODES:
            with self.subTest(apply=apply):
                client = FakeClient()
                client.views.remove(client.view_named(workflow.QUEUE_VIEW_NAME))

                with self.assertRaisesRegex(
                    workflow.ConfigurationError,
                    f'"{workflow.QUEUE_VIEW_NAME}" was not found',
                ):
                    workflow.configure_sales_workflow(client, apply=apply)

                self.assertEqual(_writes(client), [])

    def test_a_duplicate_managed_name_fails_closed_before_any_write(self) -> None:
        for name in (workflow.QUEUE_VIEW_NAME, workflow.RECONTACT_VIEW_NAME):
            for apply in self.MODES:
                with self.subTest(name=name, apply=apply):
                    client = FakeClient(include_recontact_view=True)
                    duplicate = copy.deepcopy(client.view_named(name))
                    duplicate["id"] = self.OTHER_VIEW_ID
                    client.views.append(duplicate)

                    with self.assertRaisesRegex(
                        workflow.ConfigurationError,
                        f'returned 2 views named "{name}"',
                    ):
                        workflow.configure_sales_workflow(client, apply=apply)

                    self.assertEqual(_writes(client), [])

    def test_one_view_cannot_take_two_managed_roles(self) -> None:
        # The index view is found by key, so a name collision cannot silently
        # hand it a second column set that overwrites the first.
        for name, other_role in (
            (workflow.QUEUE_VIEW_NAME, workflow.QUEUE_VIEW_NAME),
            (workflow.RECONTACT_VIEW_NAME, workflow.RECONTACT_VIEW_NAME),
        ):
            for apply in self.MODES:
                with self.subTest(name=name, apply=apply):
                    client = FakeClient()
                    if name == workflow.QUEUE_VIEW_NAME:
                        # Leave the renamed index view as the only match.
                        client.views.remove(
                            client.view_named(workflow.QUEUE_VIEW_NAME)
                        )
                    client.index_view["name"] = name

                    with self.assertRaises(workflow.ConfigurationError) as caught:
                        workflow.configure_sales_workflow(client, apply=apply)

                    message = str(caught.exception)
                    self.assertIn("more than one managed role", message)
                    self.assertIn(workflow.INDEX_VIEW_DESCRIPTION, message)
                    self.assertIn(other_role, message)
                    self.assertEqual(_writes(client), [])


def _http_error(code: int) -> HTTPError:
    return HTTPError(
        "https://twenty.example/rest/metadata/fields",
        code,
        "error",
        None,
        io.BytesIO(b"upstream detail"),
    )


class _FakeResponse:
    status = 200

    def __init__(self, payload: Any) -> None:
        self._body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self, *args: Any) -> bytes:
        return self._body.read(*args)


# These exercise the real TwentyMetadataClient against a mocked urlopen so the
# retry policy itself is under test, not the FakeClient stand-in.
class ClientPolicyTests(unittest.TestCase):
    WRITE_METHODS = ("POST", "PATCH")

    def _client(self) -> Any:
        return workflow.TwentyMetadataClient("https://twenty.example", "key", 1)

    def _failures(self) -> tuple[tuple[str, BaseException], ...]:
        failures = (
            ("transport", URLError("boom")),
            ("timeout", TimeoutError("slow")),
            ("rate limit", _http_error(429)),
            ("server error", _http_error(503)),
        )
        for _, failure in failures:
            if isinstance(failure, HTTPError):
                self.addCleanup(failure.close)
        return failures

    def test_get_is_retried_on_every_transient_failure(self) -> None:
        for label, failure in self._failures():
            with self.subTest(failure=label):
                client = self._client()
                with (
                    mock.patch.object(
                        workflow, "urlopen", side_effect=failure
                    ) as opener,
                    mock.patch.object(workflow.time, "sleep") as sleeper,
                ):
                    with self.assertRaises(workflow.ConfigurationError):
                        client.request("GET", "/rest/metadata/objects?limit=100")

                self.assertEqual(opener.call_count, workflow.MAX_ATTEMPTS)
                self.assertEqual(sleeper.call_count, workflow.MAX_ATTEMPTS - 1)

    def test_a_successful_get_is_issued_once(self) -> None:
        client = self._client()
        with mock.patch.object(
            workflow, "urlopen", return_value=_FakeResponse({"data": []})
        ) as opener:
            self.assertEqual(
                client.request("GET", "/rest/metadata/views?viewId=1"),
                {"data": []},
            )

        self.assertEqual(opener.call_count, 1)

    def test_writes_are_never_retried_on_any_transient_failure(self) -> None:
        for method in self.WRITE_METHODS:
            for label, failure in self._failures():
                with self.subTest(method=method, failure=label):
                    client = self._client()
                    with (
                        mock.patch.object(
                            workflow, "urlopen", side_effect=failure
                        ) as opener,
                        mock.patch.object(workflow.time, "sleep") as sleeper,
                    ):
                        with self.assertRaises(workflow.ConfigurationError):
                            client.request(method, "/rest/metadata/fields", {})

                    self.assertEqual(opener.call_count, 1)
                    self.assertEqual(sleeper.call_count, 0)

    def test_an_ambiguous_write_reports_an_unknown_outcome(self) -> None:
        for method in self.WRITE_METHODS:
            for label, failure in self._failures():
                with self.subTest(method=method, failure=label):
                    client = self._client()
                    with (
                        mock.patch.object(workflow, "urlopen", side_effect=failure),
                        mock.patch.object(workflow.time, "sleep"),
                    ):
                        with self.assertRaisesRegex(
                            workflow.ConfigurationError,
                            f"outcome of {method} /rest/metadata/views "
                            "may be unknown; run a dry run before retrying",
                        ):
                            client.request(method, "/rest/metadata/views", {})

    def test_a_failed_get_does_not_claim_an_unknown_outcome(self) -> None:
        client = self._client()
        with (
            mock.patch.object(workflow, "urlopen", side_effect=URLError("boom")),
            mock.patch.object(workflow.time, "sleep"),
        ):
            with self.assertRaises(workflow.ConfigurationError) as caught:
                client.request("GET", "/rest/metadata/objects?limit=100")

        self.assertNotIn("may be unknown", str(caught.exception))

    def test_unsupported_verbs_are_rejected_before_any_request(self) -> None:
        for method in ("DELETE", "PUT", "HEAD", "delete"):
            with self.subTest(method=method):
                client = self._client()
                with mock.patch.object(workflow, "urlopen") as opener:
                    with self.assertRaisesRegex(
                        workflow.ConfigurationError, "not permitted"
                    ):
                        client.request(method, "/rest/metadata/fields/some-id")

                self.assertEqual(opener.call_count, 0)

    def test_the_configurator_only_uses_permitted_methods(self) -> None:
        client = FakeClient()

        workflow.configure_sales_workflow(client, apply=True)

        self.assertTrue(client.calls)
        self.assertTrue(
            all(method in workflow.ALLOWED_METHODS for method, _, _ in client.calls)
        )


if __name__ == "__main__":
    unittest.main()
