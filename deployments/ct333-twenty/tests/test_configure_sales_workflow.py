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
        include_queue_fields: bool = False,
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
            ],
        }
        self.view = {
            "id": "20000000-0000-0000-0000-000000000001",
            "name": workflow.QUEUE_VIEW_NAME,
        }
        self.view_fields = [
            {
                "id": "view-field-name",
                "fieldMetadataId": "field-name",
                "isVisible": True,
                "position": 0,
                "size": 220,
            },
            {
                "id": "view-field-industry",
                "fieldMetadataId": "field-industry",
                "isVisible": True,
                "position": 6,
                "size": 220,
            },
        ]
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        if include_sales_fields:
            self._add_sales_fields()
        if include_queue_fields:
            self._add_queue_fields()

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
            return [copy.deepcopy(self.view)]
        if method == "GET" and path.startswith("/rest/metadata/viewFields?"):
            return copy.deepcopy(self.view_fields)
        if method == "POST" and path == "/rest/metadata/fields":
            assert payload is not None
            field = copy.deepcopy(payload)
            field["id"] = f"field-{field['name']}"
            self.company["fields"].append(field)
            return field
        if method == "POST" and path == "/rest/metadata/viewFields":
            assert payload is not None
            view_field = copy.deepcopy(payload)
            view_field["id"] = f"view-field-{payload['fieldMetadataId']}"
            self.view_fields.append(view_field)
            return view_field
        if method == "PATCH" and path.startswith("/rest/metadata/viewFields/"):
            assert payload is not None
            view_field_id = path.rsplit("/", 1)[-1]
            target = next(item for item in self.view_fields if item["id"] == view_field_id)
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

    def _add_queue_fields(self) -> None:
        fields = {field["name"]: field for field in self.company["fields"]}
        for position, column in enumerate(workflow.QUEUE_COLUMNS, start=7):
            self.view_fields.append(
                {
                    "id": f"view-field-{column['name']}",
                    "fieldMetadataId": fields[column["name"]]["id"],
                    "isVisible": True,
                    "position": position,
                    "size": column["size"],
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
            [change.name for change in changes if change.resource == "viewField"],
            [column["name"] for column in workflow.QUEUE_COLUMNS],
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
        queue_creates = [
            call for call in writes if call[1] == "/rest/metadata/viewFields"
        ]
        self.assertEqual(len(queue_creates), len(workflow.QUEUE_COLUMNS))
        self.assertEqual(
            [call[2]["position"] for call in queue_creates if call[2]],
            [7, 8, 9, 10, 11],
        )
        self.assertEqual(client.view_fields[0]["position"], 0)
        self.assertEqual(client.view_fields[1]["position"], 6)
        self.assertTrue(changes)

    def test_repeated_apply_is_idempotent(self) -> None:
        client = FakeClient(include_sales_fields=True, include_queue_fields=True)

        changes = workflow.configure_sales_workflow(client, apply=True)

        self.assertEqual(changes, [])
        self.assertFalse(any(method != "GET" for method, _, _ in client.calls))

    def test_existing_target_mapping_is_updated_without_duplication(self) -> None:
        client = FakeClient(include_sales_fields=True)
        actioned = next(
            field
            for field in client.company["fields"]
            if field["name"] == "salesActioned"
        )
        client.view_fields.append(
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
                for item in client.view_fields
            ),
            1,
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


if __name__ == "__main__":
    unittest.main()
