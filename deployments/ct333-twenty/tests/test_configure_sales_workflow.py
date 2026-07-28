from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys
import unittest
from typing import Any

MODULE_PATH = pathlib.Path(__file__).parents[1] / "configure_sales_workflow.py"
SPEC = importlib.util.spec_from_file_location("configure_sales_workflow", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
workflow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workflow
SPEC.loader.exec_module(workflow)


class FakeClient:
    def __init__(
        self,
        *,
        include_sales_fields: bool = False,
        include_recontact_view: bool = False,
        include_managed_view_fields: bool = False,
        include_recontact_filters: bool = False,
    ) -> None:
        self.company = {
            "id": "10000000-0000-0000-0000-000000000001",
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
                "id": "20000000-0000-0000-0000-000000000001",
                "name": "All Companies",
            },
            {
                "id": "20000000-0000-0000-0000-000000000002",
                "name": "Dashboard Priority Call Queue",
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
            return {"data": [copy.deepcopy(self.company)]}
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
            view["id"] = "20000000-0000-0000-0000-000000000003"
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
            "id": "20000000-0000-0000-0000-000000000003",
            "name": workflow.RECONTACT_VIEW_NAME,
        }
        self.views.append(view)
        self.view_fields[view["id"]] = []
        self.view_filters[view["id"]] = []

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
                for view_name in workflow.EXISTING_VIEW_NAMES
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
            len(workflow.EXISTING_VIEW_NAMES) * len(workflow.SALES_COLUMNS)
            + len(workflow.RECONTACT_COLUMNS),
        )
        self.assertEqual(
            len([call for call in writes if call[1] == "/rest/metadata/viewFilters"]),
            len(workflow.RECONTACT_FILTERS),
        )
        existing_view_ids = {
            view["id"]
            for view in client.views
            if view["name"] in workflow.EXISTING_VIEW_NAMES
        }
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
        standard_view = next(
            view for view in client.views if view["name"] == "All Companies"
        )
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
            and change.details["view"] == "All Companies"
        ]
        self.assertEqual(
            [change.name for change in standard_changes],
            [column["name"] for column in workflow.SALES_COLUMNS],
        )
        self.assertTrue(
            all(
                change.details == {"view": "All Companies", "isVisible": True}
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


if __name__ == "__main__":
    unittest.main()
