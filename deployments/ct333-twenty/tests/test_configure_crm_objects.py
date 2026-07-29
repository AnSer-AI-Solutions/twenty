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
OBJECT_IDS = {
    "customer": "30000000-0000-0000-0000-000000000001",
    "caller": "30000000-0000-0000-0000-000000000002",
}
# Returned by every create so a configurator that reads ids out of a POST
# response instead of re-reading the collection fails loudly.
DECOY_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"

RESPONSE_FORMATS = ("new", "legacy")


class FakeClient:
    def __init__(
        self,
        *,
        response_format: str = "new",
        include_objects: bool = False,
        include_fields: bool = False,
        include_relation: bool = False,
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
        if method == "POST" and path == "/rest/metadata/objects":
            assert payload is not None
            self._create_object(payload)
            return self._wrap_create("createOneObject", payload)
        if method == "POST" and path == "/rest/metadata/fields":
            assert payload is not None
            self._create_field(payload)
            return self._wrap_create("createOneField", payload)
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
        self.objects.append(
            {
                "id": object_id,
                "nameSingular": name,
                "namePlural": payload["namePlural"],
                "labelSingular": payload["labelSingular"],
                "labelPlural": payload["labelPlural"],
                "fields": [
                    {
                        "id": f"{name}-field-name",
                        "name": "name",
                        "label": "Name",
                        "type": "TEXT",
                    }
                ],
            }
        )

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

    def _create_inverse_field(
        self, relation: dict[str, Any], source: dict[str, Any]
    ) -> None:
        target = self._object_by_id(relation["targetObjectMetadataId"])
        inverse_name = crm.CALLER_CUSTOMER_RELATION["inverseFieldName"]
        target["fields"].append(
            {
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
        )

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

    @property
    def writes(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [call for call in self.calls if call[0] != "GET"]


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


def _change_tuples(changes: list[Any]) -> list[tuple[str, str, str]]:
    return [(change.action, change.resource, change.name) for change in changes]


EXPECTED_FULL_CHANGES = [
    ("create", "object", "customer"),
    ("create", "object", "caller"),
    ("create", "field", "customerStatus"),
    ("create", "field", "accountPhone"),
    ("create", "field", "accountEmail"),
    ("create", "field", "callerPhone"),
    ("create", "relation", "caller.customer"),
]


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
            write_paths,
            ["/rest/metadata/objects"] * 2 + ["/rest/metadata/fields"] * 5,
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
        self.assertEqual(client.writes[-1][2], payload)
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
            include_objects=True, include_fields=True, include_relation=True
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
            [entry for entry in EXPECTED_FULL_CHANGES if entry[1] == "field"],
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
            include_objects=True, include_fields=True, include_relation=True
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
                )

                self.assertEqual(
                    crm.configure_crm_objects(client, apply=True), []
                )
                self.assertEqual(client.writes, [])


class CompanyIsolationTests(unittest.TestCase):
    def test_company_object_and_fields_are_never_written(self) -> None:
        client = FakeClient()

        crm.configure_crm_objects(client, apply=True)

        company_ids = {COMPANY_ID} | {
            field["id"] for field in client.company["fields"]
        }
        for method, path, payload in client.writes:
            serialized = f"{path} {json.dumps(payload, sort_keys=True)}"
            for company_id in company_ids:
                self.assertNotIn(company_id, serialized, msg=f"{method} {path}")
        self.assertEqual(
            [field["name"] for field in client.company["fields"]],
            ["name", "leadPhone", "salesLifecycleStatus"],
        )
        self.assertEqual(client.company["labelPlural"], "Companies")

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
