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


def _drop_field(client: FakeClient, name: str) -> FakeClient:
    client.company["fields"] = [
        field for field in client.company["fields"] if field["name"] != name
    ]
    return client


def _writes(client: FakeClient) -> list[tuple[str, str, dict[str, Any] | None]]:
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
