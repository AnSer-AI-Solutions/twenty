from __future__ import annotations

import copy
import importlib.util
import pathlib
import sys
import unittest
from typing import Any
from unittest import mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

MODULE_PATH = pathlib.Path(__file__).parents[1] / "configure_lead_labels.py"
SPEC = importlib.util.spec_from_file_location("configure_lead_labels", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
labels = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = labels
SPEC.loader.exec_module(labels)

COMPANY_ID = "10000000-0000-0000-0000-000000000001"
COMPANY_VIEW_ID = "10000000-0000-0000-0000-0000000000ff"
COMPANY_PATH = f"/rest/metadata/objects/{COMPANY_ID}"

RESPONSE_FORMATS = ("new", "legacy")

# How Twenty stores an accepted label update. A standard object keeps its
# shipped label on the column and records the change in `overrides`; a custom
# object is written in place. Company is standard on CT333, but the
# configurator has to settle under either.
LABEL_STORAGES = ("overrides", "direct")

# FLAT_OBJECT_METADATA_EDITABLE_PROPERTIES.standard in v2.20. Anything else in
# an update payload is rejected outright with "Cannot edit standard object
# metadata properties", which is why nameSingular, namePlural and
# isLabelSyncedWithName can never appear in this configurator's PATCH.
STANDARD_EDITABLE_PROPERTIES = (
    "color",
    "description",
    "icon",
    "isActive",
    "isSearchable",
    "labelPlural",
    "labelSingular",
)

EXPECTED_PATCH_PAYLOAD = {"labelSingular": "Lead", "labelPlural": "Leads"}


class FakeClient:
    def __init__(
        self,
        *,
        response_format: str = "new",
        label_storage: str = "overrides",
        relabelled: bool = False,
        filler_object_count: int = 0,
        page_size: int = 200,
    ) -> None:
        self.response_format = response_format
        self.label_storage = label_storage
        self.page_size = page_size
        self.company: dict[str, Any] = {
            "id": COMPANY_ID,
            "nameSingular": "company",
            "namePlural": "companies",
            "labelSingular": "Company",
            "labelPlural": "Companies",
            "isLabelSyncedWithName": False,
            "isActive": True,
            "icon": "IconBuildingSkyscraper",
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
                    "type": "PHONES",
                },
                {
                    "id": "company-field-lifecycle",
                    "name": "salesLifecycleStatus",
                    "label": "Lifecycle Status",
                    "type": "SELECT",
                },
            ],
        }
        # The neighbours the CT333 configurators own. None of them may be read
        # for a write or relabelled by this one.
        self.objects: list[dict[str, Any]] = [
            self.company,
            self._other_object(
                "20000000-0000-0000-0000-000000000001",
                "person",
                "people",
                "Person",
                "People",
            ),
            self._other_object(
                "30000000-0000-0000-0000-000000000001",
                "customer",
                "customers",
                "Customer",
                "Customers",
            ),
            self._other_object(
                "30000000-0000-0000-0000-000000000002",
                "caller",
                "callers",
                "Caller",
                "Callers",
            ),
        ]
        for index in range(filler_object_count):
            self.objects.append(
                self._other_object(
                    f"40000000-0000-0000-0000-{index:012d}",
                    f"filler{index}",
                    f"filler{index}s",
                    f"Filler {index}",
                    f"Filler {index}s",
                )
            )
        # Company's saved views and their columns, so a configurator that
        # reconciled anything beyond the object row would show up here.
        self.views: list[dict[str, Any]] = [
            {
                "id": COMPANY_VIEW_ID,
                "name": "All Companies",
                "key": "INDEX",
                "isActive": True,
                "deletedAt": None,
                "objectMetadataId": COMPANY_ID,
            }
        ]
        self.view_fields: dict[str, list[dict[str, Any]]] = {
            COMPANY_VIEW_ID: [
                {
                    "id": f"view-field-{COMPANY_VIEW_ID}-{field['id']}",
                    "viewId": COMPANY_VIEW_ID,
                    "fieldMetadataId": field["id"],
                    "isVisible": True,
                    "position": position,
                    "size": 180,
                }
                for position, field in enumerate(self.company["fields"])
            ]
        }
        # Lead records. The metadata API cannot reach these, which is the point.
        self.records: list[dict[str, Any]] = [
            {"id": "record-1", "name": "Acme Plumbing"},
            {"id": "record-2", "name": "Bell Roofing"},
        ]
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        if relabelled:
            self._store_labels(dict(labels.MANAGED_LABELS))

    @staticmethod
    def _other_object(
        object_id: str,
        name_singular: str,
        name_plural: str,
        label_singular: str,
        label_plural: str,
    ) -> dict[str, Any]:
        return {
            "id": object_id,
            "nameSingular": name_singular,
            "namePlural": name_plural,
            "labelSingular": label_singular,
            "labelPlural": label_plural,
            "isLabelSyncedWithName": False,
            "fields": [],
        }

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
        if method == "GET" and path.startswith("/rest/metadata/objects?"):
            return self._objects_response(path)
        if method == "PATCH" and path.startswith("/rest/metadata/objects/"):
            assert payload is not None
            self._update_object(path.rsplit("/", 1)[-1], payload)
            return self._wrap_update()
        raise AssertionError(f"unexpected request: {method} {path}")

    def _objects_response(self, path: str) -> dict[str, Any]:
        query = parse_qs(urlsplit(path).query)
        limit = min(int(query.get("limit", ["60"])[0]), 200)
        cursor = query.get("starting_after", [None])[0]
        # The server paginates by id cursor, descending.
        ordered = sorted(
            self.objects, key=lambda item: item.get("id", ""), reverse=True
        )
        if cursor is not None:
            ordered = [item for item in ordered if item.get("id", "") < cursor]
        page = ordered[: min(limit, self.page_size)]
        page_info = {
            "hasNextPage": len(ordered) > len(page),
            "startCursor": page[0].get("id") if page else None,
            "endCursor": page[-1].get("id") if page else None,
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

    def _wrap_update(self) -> Any:
        updated = copy.deepcopy(self.company)
        if self.response_format == "legacy":
            return {"data": {"updateOneObject": updated}}
        return updated

    # --- mutation --------------------------------------------------------

    def _update_object(self, object_id: str, payload: dict[str, Any]) -> None:
        if object_id != COMPANY_ID:
            raise AssertionError(f"unexpected object update: {object_id}")
        rejected = [
            key for key in payload if key not in STANDARD_EDITABLE_PROPERTIES
        ]
        if rejected:
            raise AssertionError(
                "Cannot edit standard object metadata properties: "
                + ", ".join(rejected)
            )
        self._store_labels(payload)

    def _store_labels(self, payload: dict[str, Any]) -> None:
        if self.label_storage == "direct":
            self.company.update(copy.deepcopy(payload))
            return
        # computeMetadataOverridesBlob: an overridable property is diverted into
        # `overrides`, and an override equal to the shipped value is dropped. An
        # empty blob is stored as null and omitted from the response.
        overrides = dict(self.company.get("overrides") or {})
        for key, value in payload.items():
            if value == self.company.get(key):
                overrides.pop(key, None)
            else:
                overrides[key] = value
        if overrides:
            self.company["overrides"] = overrides
        else:
            self.company.pop("overrides", None)

    # --- assertion helpers -----------------------------------------------

    def object_by_name(self, name: str) -> dict[str, Any]:
        for item in self.objects:
            if item["nameSingular"] == name:
                return item
        raise AssertionError(f"unknown object {name}")

    def effective_labels(self) -> tuple[Any, Any]:
        overrides = self.company.get("overrides") or {}
        return (
            overrides.get("labelSingular", self.company["labelSingular"]),
            overrides.get("labelPlural", self.company["labelPlural"]),
        )

    @property
    def writes(self) -> list[tuple[str, str, dict[str, Any] | None]]:
        return [call for call in self.calls if call[0] != "GET"]


class NoOpUpdateClient(FakeClient):
    """Accepts the update and stores nothing.

    Models a server that answers 200 without applying the labels, which would
    otherwise leave the run reporting success on a workspace that never settles.
    """

    def _store_labels(self, payload: dict[str, Any]) -> None:
        return None


def _change_tuples(changes: list[Any]) -> list[tuple[str, str, str]]:
    return [
        (change.action, change.resource, change.name) for change in changes
    ]


EXPECTED_RELABEL = [("update", "object", "company")]


class DryRunTests(unittest.TestCase):
    def test_dry_run_reports_the_relabel_without_writing(self) -> None:
        client = FakeClient()

        changes = labels.configure_lead_labels(client, apply=False)

        self.assertEqual(_change_tuples(changes), EXPECTED_RELABEL)
        self.assertEqual(client.writes, [])

    def test_dry_run_leaves_the_whole_workspace_untouched(self) -> None:
        client = FakeClient()
        before = copy.deepcopy(
            (client.objects, client.views, client.view_fields, client.records)
        )

        labels.configure_lead_labels(client, apply=False)

        self.assertEqual(
            (client.objects, client.views, client.view_fields, client.records),
            before,
        )

    def test_dry_run_reports_the_labels_being_replaced(self) -> None:
        client = FakeClient()

        [change] = labels.configure_lead_labels(client, apply=False)

        self.assertEqual(
            change.as_dict(),
            {
                "action": "update",
                "resource": "object",
                "name": "company",
                "labelSingular": {"from": "Company", "to": "Lead"},
                "labelPlural": {"from": "Companies", "to": "Leads"},
                "nameSingular": "company",
                "namePlural": "companies",
            },
        )

    def test_dry_run_on_a_relabelled_workspace_reports_nothing(self) -> None:
        client = FakeClient(relabelled=True)

        self.assertEqual(labels.configure_lead_labels(client, apply=False), [])
        self.assertEqual(client.writes, [])


class ApplyTests(unittest.TestCase):
    def test_apply_issues_exactly_one_patch_on_the_company_object(self) -> None:
        client = FakeClient()

        changes = labels.configure_lead_labels(client, apply=True)

        self.assertEqual(_change_tuples(changes), EXPECTED_RELABEL)
        self.assertEqual(
            client.writes, [("PATCH", COMPANY_PATH, EXPECTED_PATCH_PAYLOAD)]
        )

    def test_the_patch_body_carries_the_two_labels_and_nothing_else(
        self,
    ) -> None:
        client = FakeClient()

        labels.configure_lead_labels(client, apply=True)

        [(_, _, payload)] = client.writes
        self.assertEqual(payload, {"labelSingular": "Lead", "labelPlural": "Leads"})
        self.assertNotIn("nameSingular", payload)
        self.assertNotIn("namePlural", payload)
        self.assertNotIn("isLabelSyncedWithName", payload)

    def test_apply_makes_the_ui_labels_read_lead(self) -> None:
        for label_storage in LABEL_STORAGES:
            with self.subTest(label_storage=label_storage):
                client = FakeClient(label_storage=label_storage)

                labels.configure_lead_labels(client, apply=True)

                self.assertEqual(client.effective_labels(), ("Lead", "Leads"))

    def test_apply_preserves_the_internal_names(self) -> None:
        for label_storage in LABEL_STORAGES:
            with self.subTest(label_storage=label_storage):
                client = FakeClient(label_storage=label_storage)

                labels.configure_lead_labels(client, apply=True)

                self.assertEqual(client.company["nameSingular"], "company")
                self.assertEqual(client.company["namePlural"], "companies")
                self.assertIs(
                    client.company["isLabelSyncedWithName"], False
                )

    def test_apply_preserves_every_company_field_view_and_record(self) -> None:
        client = FakeClient()
        fields_before = copy.deepcopy(client.company["fields"])
        views_before = copy.deepcopy(client.views)
        view_fields_before = copy.deepcopy(client.view_fields)
        records_before = copy.deepcopy(client.records)

        labels.configure_lead_labels(client, apply=True)

        self.assertEqual(client.company["fields"], fields_before)
        self.assertEqual(client.views, views_before)
        self.assertEqual(client.view_fields, view_fields_before)
        self.assertEqual(client.records, records_before)


class IdempotencyTests(unittest.TestCase):
    def test_a_relabelled_workspace_needs_no_write(self) -> None:
        for label_storage in LABEL_STORAGES:
            with self.subTest(label_storage=label_storage):
                client = FakeClient(
                    label_storage=label_storage, relabelled=True
                )

                self.assertEqual(
                    labels.configure_lead_labels(client, apply=True), []
                )
                self.assertEqual(client.writes, [])

    def test_apply_twice_settles_to_no_changes(self) -> None:
        for label_storage in LABEL_STORAGES:
            with self.subTest(label_storage=label_storage):
                client = FakeClient(label_storage=label_storage)

                labels.configure_lead_labels(client, apply=True)
                changes = labels.configure_lead_labels(client, apply=True)

                self.assertEqual(changes, [])
                self.assertEqual(len(client.writes), 1)

    def test_a_dry_run_after_apply_reports_no_change(self) -> None:
        client = FakeClient()

        labels.configure_lead_labels(client, apply=True)

        self.assertEqual(labels.configure_lead_labels(client, apply=False), [])

    def test_a_half_applied_relabel_is_completed_in_one_patch(self) -> None:
        client = FakeClient()
        client.company["overrides"] = {"labelSingular": "Lead"}

        changes = labels.configure_lead_labels(client, apply=True)

        self.assertEqual(_change_tuples(changes), EXPECTED_RELABEL)
        self.assertEqual(
            client.writes, [("PATCH", COMPANY_PATH, EXPECTED_PATCH_PAYLOAD)]
        )
        self.assertEqual(client.effective_labels(), ("Lead", "Leads"))

    def test_an_unrelated_override_is_not_disturbed(self) -> None:
        client = FakeClient()
        client.company["overrides"] = {"icon": "IconTargetArrow"}

        labels.configure_lead_labels(client, apply=True)

        self.assertEqual(
            client.company["overrides"],
            {
                "icon": "IconTargetArrow",
                "labelSingular": "Lead",
                "labelPlural": "Leads",
            },
        )


class ResponseFormatTests(unittest.TestCase):
    def test_both_formats_report_the_same_dry_run(self) -> None:
        reports = [
            _change_tuples(
                labels.configure_lead_labels(
                    FakeClient(response_format=response_format), apply=False
                )
            )
            for response_format in RESPONSE_FORMATS
        ]

        self.assertEqual(reports[0], reports[1])
        self.assertEqual(reports[0], EXPECTED_RELABEL)

    def test_both_formats_issue_the_same_write(self) -> None:
        results = {}
        for response_format in RESPONSE_FORMATS:
            client = FakeClient(response_format=response_format)
            labels.configure_lead_labels(client, apply=True)
            results[response_format] = client.writes

        self.assertEqual(results["new"], results["legacy"])
        self.assertEqual(
            results["new"], [("PATCH", COMPANY_PATH, EXPECTED_PATCH_PAYLOAD)]
        )

    def test_both_formats_are_idempotent(self) -> None:
        for response_format in RESPONSE_FORMATS:
            with self.subTest(response_format=response_format):
                client = FakeClient(
                    response_format=response_format, relabelled=True
                )

                self.assertEqual(
                    labels.configure_lead_labels(client, apply=True), []
                )
                self.assertEqual(client.writes, [])


class PaginationTests(unittest.TestCase):
    def test_company_is_resolved_from_a_later_page(self) -> None:
        for response_format in RESPONSE_FORMATS:
            with self.subTest(response_format=response_format):
                # Company sorts last under the descending id cursor, so it is
                # only reachable once every page has been followed.
                client = FakeClient(
                    response_format=response_format,
                    filler_object_count=5,
                    page_size=2,
                )

                changes = labels.configure_lead_labels(client, apply=True)

                self.assertEqual(_change_tuples(changes), EXPECTED_RELABEL)
                self.assertTrue(
                    any(
                        "starting_after" in path
                        for method, path, _ in client.calls
                        if method == "GET"
                    )
                )

    def test_a_cursor_that_does_not_advance_fails_closed(self) -> None:
        class StuckCursorClient(FakeClient):
            def _objects_response(self, path: str) -> dict[str, Any]:
                payload = super()._objects_response(path)
                payload["pageInfo"] = {
                    "hasNextPage": True,
                    "startCursor": COMPANY_ID,
                    "endCursor": COMPANY_ID,
                }
                return payload

        client = StuckCursorClient()

        with self.assertRaisesRegex(
            labels.ConfigurationError, "pagination did not advance"
        ):
            labels.configure_lead_labels(client, apply=True)

        self.assertEqual(client.writes, [])

    def test_an_endless_listing_fails_closed(self) -> None:
        class EndlessClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.page = 0

            def _objects_response(self, path: str) -> dict[str, Any]:
                self.page += 1
                return {
                    "data": [],
                    "pageInfo": {
                        "hasNextPage": True,
                        "startCursor": f"cursor-{self.page}",
                        "endCursor": f"cursor-{self.page}",
                    },
                    "totalCount": 0,
                }

        client = EndlessClient()

        with self.assertRaisesRegex(labels.ConfigurationError, "exceeded"):
            labels.configure_lead_labels(client, apply=True)

        self.assertEqual(client.writes, [])
        self.assertEqual(client.page, labels.MAX_OBJECT_PAGES)


class DriftTests(unittest.TestCase):
    def _assert_fails_before_writes(self, client: FakeClient, message: str) -> None:
        with self.assertRaisesRegex(labels.ConfigurationError, message):
            labels.configure_lead_labels(client, apply=True)
        self.assertEqual(client.writes, [])

    def test_a_missing_company_fails_before_writes(self) -> None:
        client = FakeClient()
        client.objects.remove(client.company)

        self._assert_fails_before_writes(client, "expected exactly one")

    def test_a_duplicated_company_fails_before_writes(self) -> None:
        client = FakeClient()
        duplicate = copy.deepcopy(client.company)
        duplicate["id"] = "10000000-0000-0000-0000-000000000002"
        client.objects.append(duplicate)

        self._assert_fails_before_writes(client, "expected exactly one")

    def test_a_renamed_singular_fails_before_writes(self) -> None:
        client = FakeClient()
        client.company["nameSingular"] = "lead"

        self._assert_fails_before_writes(client, "has nameSingular 'lead'")

    def test_a_renamed_plural_fails_before_writes(self) -> None:
        # Still resolved on nameSingular, so the drift is reported precisely
        # rather than as a missing object.
        client = FakeClient()
        client.company["namePlural"] = "leads"

        self._assert_fails_before_writes(client, "has namePlural 'leads'")

    def test_a_fully_renamed_company_is_not_relabelled(self) -> None:
        client = FakeClient()
        client.company["nameSingular"] = "lead"
        client.company["namePlural"] = "leads"

        self._assert_fails_before_writes(client, "expected exactly one")

    def test_a_label_synced_object_fails_before_writes(self) -> None:
        client = FakeClient()
        client.company["isLabelSyncedWithName"] = True

        self._assert_fails_before_writes(client, "isLabelSyncedWithName")

    def test_a_company_without_an_id_fails_before_writes(self) -> None:
        client = FakeClient()
        del client.company["id"]

        with self.assertRaisesRegex(labels.ConfigurationError, "without an id"):
            labels.configure_lead_labels(client, apply=True)
        self.assertEqual(client.writes, [])

    def test_an_invalid_metadata_response_fails_before_writes(self) -> None:
        class BrokenClient(FakeClient):
            def _objects_response(self, path: str) -> Any:
                return {"data": {"unexpected": []}}

        client = BrokenClient()

        self._assert_fails_before_writes(client, "did not contain a list")

    def test_a_rename_during_the_update_is_caught_after_the_write(self) -> None:
        class RenamingClient(FakeClient):
            def _store_labels(self, payload: dict[str, Any]) -> None:
                super()._store_labels(payload)
                self.company["nameSingular"] = "lead"
                self.company["namePlural"] = "leads"

        client = RenamingClient()

        with self.assertRaisesRegex(
            labels.ConfigurationError, "expected exactly one"
        ):
            labels.configure_lead_labels(client, apply=True)

        self.assertEqual(
            client.writes, [("PATCH", COMPANY_PATH, EXPECTED_PATCH_PAYLOAD)]
        )

    def test_an_accepted_but_ineffective_update_is_reported(self) -> None:
        client = NoOpUpdateClient()

        with self.assertRaisesRegex(
            labels.ConfigurationError, "still reports labels"
        ):
            labels.configure_lead_labels(client, apply=True)

        self.assertEqual(
            client.writes, [("PATCH", COMPANY_PATH, EXPECTED_PATCH_PAYLOAD)]
        )


class IsolationTests(unittest.TestCase):
    def test_only_the_object_metadata_collection_is_ever_addressed(
        self,
    ) -> None:
        client = FakeClient()

        labels.configure_lead_labels(client, apply=True)

        for method, path, _ in client.calls:
            self.assertTrue(
                path.startswith("/rest/metadata/objects"),
                msg=f"{method} {path}",
            )

    def test_no_field_view_or_record_endpoint_is_touched(self) -> None:
        client = FakeClient()

        labels.configure_lead_labels(client, apply=True)

        forbidden = (
            "/rest/metadata/fields",
            "/rest/metadata/views",
            "/rest/metadata/viewFields",
            "/rest/metadata/viewFilters",
            "/rest/companies",
        )
        for method, path, _ in client.calls:
            for prefix in forbidden:
                self.assertFalse(
                    path.startswith(prefix), msg=f"{method} {path}"
                )

    def test_no_post_or_delete_is_ever_issued(self) -> None:
        client = FakeClient()

        labels.configure_lead_labels(client, apply=True)

        self.assertEqual(
            [method for method, _, _ in client.calls if method != "GET"],
            ["PATCH"],
        )

    def test_no_other_object_is_relabelled(self) -> None:
        client = FakeClient()
        before = {
            item["nameSingular"]: copy.deepcopy(item)
            for item in client.objects
            if item is not client.company
        }

        labels.configure_lead_labels(client, apply=True)

        for name, snapshot in before.items():
            self.assertEqual(client.object_by_name(name), snapshot)

    def test_no_other_object_id_appears_in_a_write(self) -> None:
        client = FakeClient()
        other_ids = [
            item["id"] for item in client.objects if item is not client.company
        ]

        labels.configure_lead_labels(client, apply=True)

        for method, path, payload in client.writes:
            for object_id in other_ids:
                self.assertNotIn(object_id, path, msg=f"{method} {path}")
                self.assertNotIn(object_id, repr(payload))


class ClientPolicyTests(unittest.TestCase):
    def _client(self) -> Any:
        return labels.TwentyMetadataClient("https://twenty.example", "key", 1)

    def test_get_is_retried_on_transport_failure(self) -> None:
        client = self._client()
        with (
            mock.patch.object(
                labels, "urlopen", side_effect=URLError("boom")
            ) as opener,
            mock.patch.object(labels.time, "sleep"),
        ):
            with self.assertRaises(labels.ConfigurationError):
                client.request("GET", "/rest/metadata/objects")

        self.assertEqual(opener.call_count, labels.MAX_ATTEMPTS)

    def test_the_patch_is_never_retried(self) -> None:
        client = self._client()
        with (
            mock.patch.object(
                labels, "urlopen", side_effect=URLError("boom")
            ) as opener,
            mock.patch.object(labels.time, "sleep") as sleeper,
        ):
            with self.assertRaisesRegex(
                labels.ConfigurationError,
                "outcome of PATCH .* may be unknown; run a dry run",
            ):
                client.request("PATCH", COMPANY_PATH, EXPECTED_PATCH_PAYLOAD)

        self.assertEqual(opener.call_count, 1)
        self.assertEqual(sleeper.call_count, 0)

    def test_post_and_delete_are_rejected_by_the_client(self) -> None:
        for method in ("POST", "DELETE"):
            with self.subTest(method=method):
                client = self._client()

                with self.assertRaisesRegex(
                    labels.ConfigurationError, "not permitted"
                ):
                    client.request(method, "/rest/metadata/objects", {})

    def test_credentials_are_required(self) -> None:
        with self.assertRaisesRegex(labels.ConfigurationError, "TWENTY_API_URL"):
            labels.TwentyMetadataClient("", "key")
        with self.assertRaisesRegex(labels.ConfigurationError, "TWENTY_API_KEY"):
            labels.TwentyMetadataClient("https://twenty.example", "")


if __name__ == "__main__":
    unittest.main()
