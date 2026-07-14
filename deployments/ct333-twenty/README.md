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
