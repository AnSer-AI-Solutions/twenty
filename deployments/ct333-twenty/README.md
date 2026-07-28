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
`Dashboard Priority Call Queue` columns reproducible through Twenty's supported
metadata API.

It owns these Company fields:

- `callStatus`, `callAttempts`, `lastCalledAt`, `nextFollowUpAt`, and
  `callNotes`
- `salesActioned` (label `Actioned`) and `byronReviewed` (label
  `Byron Reviewed`)

The priority queue keeps its existing columns and appends `Actioned`,
`Byron Reviewed`, `Call Notes`, `Call Attempts`, and `Next Follow-Up`. The
configurator does not read or write Company records, delete fields, or remove
view columns.

Creating a missing custom field invokes Twenty's normal workspace schema
migration. Twenty may also register that field in its standard Company views
using the product defaults (hidden in the standard table and available on the
record page). This configurator explicitly controls visibility and ordering
only in `Dashboard Priority Call Queue`.

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

Run the focused tests with:

```bash
python3 -m unittest discover \
  -s deployments/ct333-twenty/tests \
  -p 'test_*.py'
```
