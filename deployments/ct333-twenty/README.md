# CT333 Twenty deployment

This deployment runs Twenty CRM on `twenty-crm-01` (`192.168.31.181`) with
PostgreSQL, Redis, the Twenty worker, and a Dozzle agent.

- Twenty is pinned to `v2.20.0`.
- Runtime secrets come from Bitwarden Secrets Manager project
  `CT333 - twenty-crm Runtime` (`e6c761f9-7a4d-45a4-a780-b4860116e943`).
- The CT-specific token is generated from the read-only machine account
  `product-apps-runtime-readonly` with label `product-apps-ct333-twenty` and
  belongs at `/etc/bitwarden-sm/product-apps-runtime-readonly.access-token`
  with mode `0600`.
- The wrapper injects only the explicit CT333 project id even though the shared
  machine account can see the wider approved product-app project set.
- No dotenv file is used.

Install the wrapper as `/usr/local/bin/twenty-run`, then use:

```bash
sudo twenty-run config
sudo twenty-run pull
sudo twenty-run up
sudo twenty-run ps
```

## Sales workflow metadata

`configure_sales_workflow.py` keeps the Company sales workflow fields and the
`All Companies`, `Dashboard Priority Call Queue`, and `Recontact Due` views
reproducible through Twenty's supported metadata API.

It owns these Company fields:

- `callStatus`, `callAttempts`, `lastCalledAt`, `nextFollowUpAt`, and
  `callNotes`
- `salesActioned` (label `Actioned`) and `byronReviewed` (label
  `Byron Reviewed`)
- `salesLifecycleStatus`, `salesDisposition`, and `recontactAt`

The two existing sales table views keep their existing columns and append
`Actioned`, `Byron Reviewed`, `Call Notes`, `Call Attempts`, and
`Next Follow-Up`. The configurator also appends lifecycle, disposition, and
recontact fields. The `Recontact Due` view shows only `NURTURE` companies
whose `Recontact At` date is in the past. The configurator does not read or
write Company records, delete fields, remove view columns, or remove
saved-view filters.

Lead records are retained regardless of lifecycle state. Use `NURTURE` for a
lead that should return later; the CT332 daily CRM sync fills a blank
`Recontact At` with the date exactly six calendar months from that run.
Existing recontact dates are never overwritten. `DO_NOT_CONTACT`,
`DISQUALIFIED`, `WON`, and `CLOSED_BUSINESS` records are not eligible because
the scheduler queries only `NURTURE`.

Creating a missing custom field invokes Twenty's normal workspace schema
migration. Twenty may also register that field in its standard Company views
using the product defaults. This configurator explicitly controls visibility
and ordering in `All Companies` and `Dashboard Priority Call Queue`; Twenty's
record-page defaults remain unchanged.

The script is dry-run by default. Run it from a merged `main` checkout by
streaming it into the CT332 CRM sync container, which already has the scoped
Twenty API URL and key:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec -i leads-crm-sync-1 python -' \
  < deployments/ct333-twenty/configure_sales_workflow.py
```

Review the reported changes before applying:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec -i leads-crm-sync-1 python - --apply' \
  < deployments/ct333-twenty/configure_sales_workflow.py
```

Repeat the dry run after apply. A reconciled workspace reports
`"changeCount": 0`.

## Customer and Caller objects

`configure_crm_objects.py` is a separate, standalone configurator for the two
custom objects that sit alongside the lead pipeline:

- `Customer` (`customer` / `customers`) — the post-conversion account, with
  `customerStatus` (a required select defaulting to `ACTIVE`, plus `PAUSED` and
  `CHURNED`), `accountPhone`, and `accountEmail`.
- `Caller` (`caller` / `callers`) — an individual who calls in on a customer's
  behalf, with `callerPhone`.
- A nullable `MANY_TO_ONE` relation `caller.customer`, posted on the many side
  so Twenty generates the `customer.callers` inverse itself.

It creates metadata only. It does not read or write any lead record, and it
does not touch the Company object, its fields, its labels, or its views —
Company keeps the lead pipeline that `configure_sales_workflow.py` manages, and
the two scripts share no state. Customer-to-Company provenance and the
`Customers`/`Callers` view columns are deliberately out of scope here.

Safety properties, each covered by a test in
`tests/test_configure_crm_objects.py`:

- Dry run by default; `--apply` is required to write, and a reconciled
  workspace reports `"changeCount": 0`.
- Object ids are always re-read from `GET /rest/metadata/objects` after a
  create, never parsed out of a POST response, because that response shape
  depends on the `IS_REST_METADATA_API_NEW_FORMAT_DIRECT` workspace flag. Both
  shapes are handled, and the object listing follows `pageInfo` cursors.
- Objects are created before their fields, and both objects exist before the
  relation is posted.
- The relation is create-once: an existing `caller.customer` suppresses the
  POST rather than duplicating the pair.
- Incompatible existing metadata — a drifted object label or plural name, a
  field with the wrong type, nullability, default, or select options, a
  relation with a missing or wrong relation type, or a half-created relation
  pair — raises before the first write of that phase.
- `DELETE` is rejected by the HTTP client, and `POST`/`PATCH` are never
  retried automatically, since a metadata write that may already have landed
  cannot be replayed safely.

Creating a custom object runs a real workspace schema migration and brings its
own system fields, an `All Customers` / `All Callers` index view, and a record
page layout. Creating each business and relation field afterwards makes Twenty
register a hidden view field for it on that object's index view. Those columns
remain hidden until the separate view-column change lands; this configurator
leaves the hidden entries alone.

Twenty v2.20 exposes each relation's direction but not its target object
through the metadata REST response. The configurator therefore validates both
named sides and their relation types, but cannot independently prove that an
existing relation pair points to the expected object. On any failed write, its
outcome may be unknown; run the dry run again before retrying. A partially
applied run is safe to resume because every object or field request is atomic
and the configurator skips compatible metadata that already exists.

Stream it into the CT332 CRM sync container the same way, dry run first:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec -i leads-crm-sync-1 python -' \
  < deployments/ct333-twenty/configure_crm_objects.py
```

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec -i leads-crm-sync-1 python - --apply' \
  < deployments/ct333-twenty/configure_crm_objects.py
```

## Tests

Run the focused tests for both configurators with:

```bash
python3 -m unittest discover \
  -s deployments/ct333-twenty/tests \
  -p 'test_*.py'
```
