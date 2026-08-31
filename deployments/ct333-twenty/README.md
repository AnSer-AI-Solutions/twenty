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
sudo twenty-run provision-metrics-role
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

## CRM call-activity metrics

The `crm-metrics` sidecar exposes aggregate Prometheus metrics on CT 333 port
`9105`. It treats the Sales Activity table's Company `Last Called At` field as
the completed-call timestamp and uses Central calendar boundaries for today,
the last 7 days, and the retained last-30-days metric. Every aggregate excludes
Saturday and Sunday. The dashboard trend emits only the weekdays in the rolling
last-seven-calendar-day window.

Twenty stores only the latest `Last Called At` value on each Company. These
metrics therefore count active companies whose latest logged call falls in a
window, not every historical attempt. A second call to the same Company
replaces its earlier timestamp and remains one Company in the window.

The exporter discovers the one readable `workspace_*` schema and selects only
`company.id`, `deletedAt`, `lastCalledAt`, and `callStatus`. Its output contains
counts, bounded status labels, dates, and source freshness only. Company names,
phone numbers, record ids, notes, and other CRM fields never leave CT 333.

The existing Bitwarden project `CT333 - twenty-crm Runtime`
(`e6c761f9-7a4d-45a4-a780-b4860116e943`) must contain
`CRM_METRICS_DATABASE_PASSWORD`. The existing
`product-apps-runtime-readonly` CT 333 token consumes it through `twenty-run`;
no new machine account or token is required. Provision or rotate the scoped
database role before recreating the exporter:

```bash
sudo twenty-run provision-metrics-role
sudo twenty-run up
curl -fsS http://192.168.31.181:9105/health
curl -fsS http://192.168.31.181:9105/metrics | grep '^twenty_crm_'
```

The published socket is deliberately bound to the CT's private LAN address,
not loopback or every interface. Use that address for guest-local and CT329
acceptance checks.

`provision-metrics-role` creates or rotates login `twenty_crm_metrics`, forces
read-only transactions and a five-second statement timeout, and grants only
schema usage plus SELECT on the workspace Company table. Never give the
exporter the main `PG_DATABASE_PASSWORD` value.

## Sales workflow metadata

`configure_sales_workflow.py` keeps the Company sales workflow fields and the
standard Company index, `Dashboard Priority Call Queue`, and `Recontact Due`
views reproducible through Twenty's supported metadata API. It also owns the
`Ranked Lead Review Queue`, which is a saved table view rather than a forked
Twenty frontend.

It resolves the generated index view by its stable `key == "INDEX"` identity,
not its rendered name. The operator-created views keep their literal
name-based identity. Applying the Lead labels can therefore happen before or
after sales-workflow reconciliation; see [Lead labels](#lead-labels).

It owns these Company fields:

- `callStatus`, `callAttempts`, `lastCalledAt`, `nextFollowUpAt`, and
  `callNotes`
- `salesActioned` (label `Actioned`) and `byronReviewed` (label
  `Byron Reviewed`)
- `emailSent` (label `Email Sent`) and `emailSentDate` (label
  `Email Sent Date`)
- `salesLifecycleStatus`, `salesDisposition`, and `recontactAt`
- `leadFitScore`, `leadFitRank`, `leadFitPriority`, `leadFitConfidence`,
  `leadFitReason`, `leadFitModelVersion`, and `leadFitScoredAt`
- `leadReviewQueue` and `leadReviewRank`

The standard Company table and priority queue keep their existing columns and
append `Actioned`, `Byron Reviewed`, `Call Notes`, `Call Attempts`, and
`Next Follow-Up`. `Sales Activity` receives only `Email Sent` and `Email Sent
Date`, leaving every other view and its own existing column visibility and order
untouched. CT332 fills a blank date with the current Central calendar date after
`Email Sent` is checked. It never clears or overwrites an existing date. The
configurator also appends lifecycle, disposition, and recontact fields. The
`Recontact Due` view shows only `NURTURE` companies whose `Recontact At` date is
in the past.

The `Ranked Lead Review Queue` shows the current diversified human-review
slate, not every scored record. It filters to `Fit Review Queue = true`,
`Actioned = false`, and lifecycle `NEW` or `WORKING`, then sorts by
`Review Rank` ascending. Its columns expose company, review rank, fit score,
fit band, fit confidence, industry, the short fit explanation, contact
fields, and the existing sales action fields. Marking a lead Actioned or moving
it out of `NEW`/`WORKING` removes it from this queue without deleting the
record. `NURTURE` remains a retained lifecycle state.

The configurator does not read or write Company records, delete fields, remove
view columns, remove saved-view filters, or remove saved-view sorts.

A preflight runs before the first write, identically in both modes. The managed
columns also reference Company fields this configurator does not own: the
standard `name`, plus `leadPhone`, `leadEmail`, `leadQualityTier`, and
`leadIndustry`, which the enrichment pipeline owns. If any of them is absent,
the run fails with the same error in a dry run and in an apply, having written
nothing. Add the missing field upstream, where it is owned; the configurator
will not create it, because a definition invented here would drift from the
owner's.

The Company object itself is resolved from a paginated listing. Reading only
the first page let a workspace with enough objects report Company missing when
it was merely further down, so the configurator follows `pageInfo` cursors, in
both the direct and legacy response shapes. Company on the first page stays a
single request. Later pages are fetched only while Company is still missing,
and the walk refuses to continue past a page that claims a successor without
naming a usable, unseen cursor, past a repeated page, or past
`MAX_OBJECT_PAGES`. Each of those raises before the first write.

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

## Lead-fit ranking snapshot

`apply_lead_fit_ranking.py` is the separate record-level importer for the
reviewed `client-fit-v1` v2 snapshot under `rankings/`. The committed CSV contains
only source identity, Twenty record identity, score/rank/band/confidence, review
rank, and the short fit explanation. It excludes names, phone numbers, email
addresses, and street addresses.

The importer is dry-run by default and fails before the first record write
unless all of these remain true:

- the CSV SHA-256, row count, 50-row review count, and priority counts match the
  committed manifest
- rank and review-rank sequences are consecutive, identities are unique, and
  scores are within 0–100
- all lead-fit field types already match the reviewed metadata configuration
- the input source keys exactly equal the live Twenty company population
- every input `twenty_record_id` still matches its live `sourceLeadKey`

The record payload is allowlisted to the nine `leadFit*` / `leadReview*`
fields. It cannot update lifecycle, disposition, Actioned, Byron Reviewed,
call status, call notes, contact fields, names, or addresses. It never deletes
records. The score is an advisory relative priority, not a conversion
probability, and the `NURTURE` fit band never authorizes deletion or permanent
disqualification. The importer normalizes the snapshot time to Twenty's
millisecond precision and evenly spaces at most 60 requests per 60 seconds,
below the live API's 100-request limit. A full initial import therefore takes
roughly 29 minutes. Apply mode reports progress every 30 confirmed writes.

After this PR is merged, stage the merged importer and snapshot over the LAN
into the existing CT332 CRM sync container. Do not use a Proxmox node or QEMU
guest agent as a file-transfer workspace:

```bash
ssh ops@192.168.31.164 \
  'mkdir -p /tmp/ct333-fit-import/rankings'
scp \
  deployments/ct333-twenty/apply_lead_fit_ranking.py \
  deployments/ct333-twenty/rankings/cold-lead-ranking-v2.csv \
  deployments/ct333-twenty/rankings/cold-lead-ranking-v2.manifest.json \
  ops@192.168.31.164:/tmp/ct333-fit-import/
ssh ops@192.168.31.164 \
  'mv /tmp/ct333-fit-import/cold-lead-ranking-v2.* /tmp/ct333-fit-import/rankings/ && sudo docker exec leads-crm-sync-1 mkdir -p /tmp/ct333-fit-import/rankings'
ssh ops@192.168.31.164 \
  "sudo docker exec -i leads-crm-sync-1 sh -c 'umask 077; dd of=/tmp/ct333-fit-import/apply_lead_fit_ranking.py status=none' < /tmp/ct333-fit-import/apply_lead_fit_ranking.py"
ssh ops@192.168.31.164 \
  "sudo docker exec -i leads-crm-sync-1 sh -c 'umask 077; dd of=/tmp/ct333-fit-import/rankings/cold-lead-ranking-v2.csv status=none' < /tmp/ct333-fit-import/rankings/cold-lead-ranking-v2.csv"
ssh ops@192.168.31.164 \
  "sudo docker exec -i leads-crm-sync-1 sh -c 'umask 077; dd of=/tmp/ct333-fit-import/rankings/cold-lead-ranking-v2.manifest.json status=none' < /tmp/ct333-fit-import/rankings/cold-lead-ranking-v2.manifest.json"
```

Run the importer without `--apply` first:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec leads-crm-sync-1 python /tmp/ct333-fit-import/apply_lead_fit_ranking.py'
```

The reviewed snapshot expects exactly 1,735 live companies and reports 50
review-queue rows. If live delivery has added another company, stop and
regenerate the ranking rather than weakening the exact-population preflight.
After reviewing the dry-run counts, apply once:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec leads-crm-sync-1 python /tmp/ct333-fit-import/apply_lead_fit_ranking.py --apply'
```

Repeat the dry run; a completed import reports `"changeCount": 0`. Then remove
only the exact temporary directory from the container and CT:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec leads-crm-sync-1 rm -rf /tmp/ct333-fit-import && rm -rf /tmp/ct333-fit-import'
```

If a PATCH fails, it is not retried because its outcome may be unknown. The
importer stops and reports the number of confirmed writes. Repeat the dry run
before resuming; already matching rows are skipped.

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

## Lead labels

`configure_lead_labels.py` is a third standalone configurator. Its only mutation
is `PATCH /rest/metadata/objects/{companyId}` carrying `labelSingular` `Lead`
and `labelPlural` `Leads`.

### Why relabelling Company creates the switch

The sales desk works from three entries: Leads, Customers, and Callers.
`configure_crm_objects.py` already creates the last two. The first is Company,
which has held the lead pipeline since CT332 began syncing into it but still
reads `Companies` everywhere in the UI.

Twenty builds navigation entries, page titles, and generated view names from an
object's labels, so changing those two labels is the whole change. The sidebar
reads `Leads` beside `Customers` and `Callers`, and promoting a lead to a
customer stays a manual decision someone makes in the UI. There is no second
object, no record migration, and no relation between Company and Customer;
customer-to-lead provenance is still deliberately out of scope, as it is for
`configure_crm_objects.py`.

### Why the enrichment contract survives

Twenty derives its API surface from an object's *internal* names, never from its
labels. `nameSingular` stays `company` and `namePlural` stays `companies`, so
the CT332 daily CRM sync keeps posting to `/rest/companies` and keeps reading and
writing the same Company field names. `configure_sales_workflow.py` keeps owning
the same fields under the same names.

Nothing else moves. In v2.20 a label-only update has no migration side effects:
`handleFlatObjectMetadataUpdateSideEffect` recomputes indexes, view fields, and
related morph field names only when a *name* or the label identifier changes.
Every Company field, saved view, view column, saved filter, and record is left
exactly as it was.

### The generated index view follows the effective label safely

The Company index view is stored as the literal template
`All {objectLabelPlural}`, and `GET /rest/metadata/views` renders it against the
effective object label on every read. Once the labels are applied that view
comes back named `All Leads`.

`configure_sales_workflow.py` matches that view on `key == "INDEX"`, requires
exactly one live match for Company, and reports the rendered name only for
operator-readable output. The same reconciliation therefore works when the
view reads `All Companies`, `All Leads`, or a localized name. `Dashboard
Priority Call Queue` and `Recontact Due` remain literal names and are
unaffected.

### Safety properties

Each is covered by a test in `tests/test_configure_lead_labels.py`:

- Dry run by default; `--apply` is required to write, and a relabelled
  workspace reports `"changeCount": 0`.
- The single `PATCH` body carries exactly `labelSingular` and `labelPlural`. The
  HTTP client rejects `POST` and `DELETE` outright, so nothing is created or
  removed, and no field, view, or record endpoint is ever addressed.
- Company is resolved from `GET /rest/metadata/objects` on either internal name,
  following `pageInfo` cursors, in both response formats. A missing, duplicated,
  or renamed Company, or one without an id, raises before the write.
- An object with `isLabelSyncedWithName` set raises. Its names are meant to
  track its labels, and `Lead`/`Leads` do not derive `company`/`companies`.
- `PATCH` is never retried, because a write that may already have landed cannot
  be replayed safely. Only `GET` is.
- After the write the object is read back. The labels must read `Lead`/`Leads`
  and the internal names must still read `company`/`companies`, or the run
  reports an error.

Company is a standard object, and v2.20 records a label change on a standard
object in the object's `overrides` blob rather than on the `labelSingular` and
`labelPlural` columns; reads spread that blob back over the row. The
configurator compares override-then-column, which is what the UI shows, so it
settles to `"changeCount": 0` whichever way the workspace stores the label.
`FLAT_OBJECT_METADATA_EDITABLE_PROPERTIES.standard` excludes `nameSingular`,
`namePlural`, and `isLabelSyncedWithName`, so v2.20 rejects those keys on
Company outright and the payload carries only the two labels. The API key needs
the `DATA_MODEL` settings permission, as it does for the other two
configurators.

### Running it

Stream it into the CT332 CRM sync container the same way, dry run first:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec -i leads-crm-sync-1 python -' \
  < deployments/ct333-twenty/configure_lead_labels.py
```

Review the reported change, then apply:

```bash
ssh ops@192.168.31.164 \
  'sudo docker exec -i leads-crm-sync-1 python - --apply' \
  < deployments/ct333-twenty/configure_lead_labels.py
```

Repeat the dry run after apply. A relabelled workspace reports
`"changeCount": 0`.

## Tests

Run the focused tests for all three configurators, the lead-fit importer, and
the wrapper with:

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
