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

### Guarded arguments

`up` and `down` take no arguments. Anything extra is refused with exit status
`2` and a usage line, before the runtime token is read and before `bws` or
`docker` is executed:

```bash
sudo twenty-run up --force-recreate   # exit 2, nothing runs
sudo twenty-run down -v               # exit 2, nothing runs
```

Both of those are one keystroke from an outage. `--force-recreate` replaces
every healthy container, and `-v` deletes the `db-data` volume the CRM lives
on. Forwarding them was possible until the guard existed, so `twenty-run up`
now always means exactly `up -d --remove-orphans` and `twenty-run down` always
means exactly `down`; what gets deployed is whatever `compose.yaml` and the
pinned tag say, not what was typed after the verb.

`pull`, `ps`, and `logs` still forward their arguments unchanged
(`sudo twenty-run logs -f worker`) — none of them can remove state. `config`
ignores extra arguments, as it always has. An unknown verb exits `2` with the
same usage line. A missing token still exits `1`, and now does so only for a
verb the wrapper accepts.

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

`DELETE` is rejected by the HTTP client, and only `GET` is retried. A `POST` or
`PATCH` that fails on a rate limit, a server error, or a transport timeout is
never replayed, because a write that may already have landed cannot be repeated
safely; the error says the outcome may be unknown, so repeat the dry run to see
what actually applied before retrying.

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

It also reconciles the columns of each object's table view, described below.

It creates metadata only. It does not read or write any lead record, and it
does not touch the Company object, its fields, its labels, or its views —
Company keeps the lead pipeline that `configure_sales_workflow.py` manages, and
the two scripts share no state. Customer-to-Company provenance is deliberately
out of scope here.

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
- Table columns are reconciled last, once every object and field it needs
  exists, and a missing field raises before that object's view is touched.

Creating a custom object runs a real workspace schema migration and brings its
own system fields, an index view, and a record page layout. Creating each
business and relation field afterwards makes Twenty register a hidden view
field for it on that object's index view.

### Customers and Callers table columns

The same configurator reconciles the columns of each object's index view:

| Customers | width | | Callers | width |
| --- | --- | --- | --- | --- |
| `name` | 220 | | `name` | 220 |
| `customerStatus` | 150 | | `callerPhone` | 180 |
| `accountPhone` | 180 | | `customer` | 200 |
| `accountEmail` | 240 | | | |
| `callers` | 160 | | | |

No view is created and no filter is set: the index view Twenty generates with
the object is the deliverable.

- **The view is matched on `key == "INDEX"`, never by name.** Twenty stores the
  generated view under the literal name `All {objectLabelPlural}` and rewrites
  it on every read against the workspace locale, so the name that comes back is
  a rendered string, not a stable key.
- **Exactly one live index view must exist.** A second active `INDEX` view, or
  none, raises before anything is written for that object rather than picking
  one. Deactivated and soft-deleted index views are ignored, matching the views
  Twenty itself registers new fields on.
- **Auto-created mappings are patched, never re-posted.** Creating a field makes
  Twenty add a hidden mapping on every active index view of that object, and the
  migration builder rejects a second mapping for the same field and view, so a
  `POST` over one would fail the run. A `POST` is issued only when a mapping is
  genuinely absent. More than one mapping for the same field raises.
- **Unmanaged columns are preserved.** Nothing is deleted or hidden, and the
  managed columns are appended after the highest position this configurator
  does not own, so an operator's own columns keep their positions and widths.
  On a freshly created object that means the five columns Twenty creates
  visible by default (`name`, `createdAt`, `createdBy`, `updatedAt`,
  `updatedBy`) stay visible, and the managed block — including `name`, which
  moves — lands after `createdAt`, `createdBy`, `updatedAt` and `updatedBy`.
  Hiding or reordering those four is a manual choice, left to whoever owns the
  view.
- **Company is untouched here too.** Views are resolved from the Customer and
  Caller object ids only, and the object id on the returned view and the view id
  on each returned mapping are both re-checked before any write, so no Company
  view or column can be reconciled even if the server ignored the query filter.

Twenty v2.20 exposes each relation's direction but not its target object
through the metadata REST response. The configurator therefore validates both
named sides and their relation types, but cannot independently prove that an
existing relation pair points to the expected object. On any failed write, its
outcome may be unknown; run the dry run again before retrying. A partially
applied run is safe to resume because every object, field and column request is
atomic and the configurator skips compatible metadata that already exists.

`GET /rest/metadata/viewFields` filters out soft-deleted mappings but not
deactivated ones, and a deactivated mapping is indistinguishable from a live one
in that response. A target column whose only mapping had been deactivated would
therefore be patched rather than re-created, and would stay off the table. Only
deleting a column through the UI can produce that state; this configurator never
does.

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

Run the focused tests for both configurators and the wrapper with:

```bash
python3 -m unittest discover \
  -s deployments/ct333-twenty/tests \
  -p 'test_*.py'
```

`tests/test_twenty_run.py` covers the wrapper's argument guard. It sources
`twenty-run` with `run_compose` stubbed to record the argument vector docker
compose would have received, and it runs the wrapper end to end against a
`PATH` that resolves only fake `docker` and `bws` shims and a token path that
does not exist. No test reaches Bitwarden, Docker, or CT333.
