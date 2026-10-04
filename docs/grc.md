# GRC and compliance evidence (CISO Assistant)

`roles/grc` deploys [CISO Assistant](https://github.com/intuitem/ciso-assistant-community)
Community Edition on the `grc` group (`playbooks/governance.yml`): frameworks, controls, evidence,
audits and risk registers in one place, fed by this repository's compliance checklist.

## Licence and free-tier check (CONTRIBUTING rule 7)

- Code outside the repository's top-level `enterprise/` directory is AGPL-3.0. Upstream's
  `LICENSE.md` states that the images under `ghcr.io/intuitem/ciso-assistant-community/` are AGPL-3.0;
  images directly under `ghcr.io/intuitem/` are the commercial edition. The role pins
  `ghcr.io/intuitem/ciso-assistant-community/backend:v4.1.0` and `.../frontend:v4.1.0`.
- **SSO (OIDC and SAML) is in the community edition** (checked in v4.1.0): `backend/iam/sso/`,
  the OIDC views and the SSO settings API are outside `enterprise/`, as are just-in-time
  provisioning (the `jit_provisioning` feature flag is defined by the community serializer) and
  IdP group sync (claims mapped to IdP groups, each granting CISO Assistant user groups). No
  authenticating proxy is needed.
- Audit log: in the community edition (django-auditlog; `AUDITLOG_RETENTION_DAYS`, set to 365 here).
- HA: not available in any edition as active/active for this deployment shape (see below).
- AGPL-3.0: running it unmodified for internal use has no source-disclosure obligation; modifying it
  and offering it to others over a network does.

## Architecture

- Containers on the single `grc` node, host networking, all non-root with no capabilities, no
  privilege escalation, read-only root filesystems (tmpfs `/tmp`), memory and PID limits:
  `grc-backend` (Django/gunicorn, uid 1001; runs migrations and stores the framework libraries at
  start), `grc-worker` (Huey task queue, uid 1001), `grc-frontend` (SvelteKit, uid 1000, bound to
  127.0.0.1) and `grc-proxy` (HAProxy, uid 99, the image the edge uses).
- TLS: the backend and frontend speak plain HTTP, so `grc-proxy` terminates TLS 1.2+ on
  `grc_https_port` (8443) with a certificate from `tls_cert`, routes `/api/` to the backend and the
  rest to the frontend. The backend's gunicorn binds every address (fixed upstream); the host
  firewall (default deny; only 8443 is declared in `firewall_rules_grc`) keeps 8000 local.
- Database: PostgreSQL from the data layer (`grc_db_*`, TLS `require` by default, its own database
  and user, DAT-01). Credentials only in 0600 env files.
- Edge: admin-only service. Suggested catalog entry:

  ```yaml
  - name: grc
    hostnames: [grc.example.internal]
    backend_group: grc
    backend_port: 8443
    backend_tls: true
    health_path: /api/health/
    public: false
    admin_paths: [/metrics]
    # The SvelteKit front end sets its own headers; keep them.
    csp: ""
  ```

## Identity (IAM-01, IAM-03, IAM-09)

- `oidc_client` registers the `grc` client in Keycloak (redirect
  `<grc_hostname>/api/accounts/oidc/keycloak/login/callback/`).
- The seed step configures OIDC through the API: provider `openid_connect`, issuer
  `<keycloak>/realms/org`, PKCE, the `roles` claim as the IdP group claim, JIT provisioning on, and
  **Force SSO**, which removes local passwords from every user except those with "keep local login"
  (the break-glass superuser `grc_admin_email`).
- Role mapping (`grc_role_user_groups`, from the rbac_model capability `evidence`):
  security-admin -> Administrator (`BI-UG-ADM`); platform-admin, operator and auditor -> Global
  reader (`BI-UG-GAD`); developer and member get no group (they can sign in but see nothing).
  Edit the IdP groups in the UI to grant domain-scoped rights (e.g. auditee) per framework.

## Availability: the documented exception to HA-01

CISO Assistant cannot run active/active against one database: attachments (evidence files) and the
Huey task queue live on the node's disk (`/opt/grc/data`), and every instance runs migrations and
scheduled jobs at start. The role therefore asserts exactly one node (`grc_min_nodes: 1`,
`grc_max_nodes: 1`).

This is acceptable because GRC is an internal tool: its loss does not affect the availability of
any customer-facing service (SOC 2 A1), compliance work can wait hours, and the evidence it holds is
also in git, CI logs and central logging. Recovery: rebuild or re-point the `grc` group to another
host, run `playbooks/governance.yml`, then `playbooks/restore.yml` for the `grc` declaration and
`pg_restore` the dump, within `grc_backup_rto_hours` (8 h; RPO 24 h). `GRCDown` alerts after 15
minutes (warning).

## Backups (BCK-01, BCK-04)

`backup_register` declares `grc`: paths `/var/backups/grc` (database dump) and `/opt/grc/data`
(attachments, task queue, generated keys); pre-command `/usr/local/sbin/grc-db-dump` (`pg_dump
--format=custom` through the pinned `postgres_client` image, credentials from a 0600 env file,
written atomically, mode 0600); restore-test verify command `pg_restore --list` on the restored dump.
Version changes refuse to run outside `playbooks/upgrade.yml` and dump the database first
(migrations run at start, CHG-07).

## Logs and metrics

- Backend logs JSON (`LOG_FORMAT=json`) to stdout -> journald; the TLS terminator logs JSON access
  lines. The application audit log (who changed which control, evidence, assessment) is kept in the
  database for 12 months and exportable from the UI.
- Prometheus metrics (`EXPOSE_METRICS=True`) at `https://<node>:8443/metrics/`, allowed only from
  `grc_metrics_allowed_cidrs` (monitoring networks) by the TLS terminator: instance counters
  (users, frameworks, controls, evidences, assessments).

## Frameworks

The seed step imports the libraries CISO Assistant ships: **SOC 2** (`urn:intuitem:risk:library:soc2-2017`,
2017 Trust Services Criteria) and **ISO/IEC 27001:2022** (`urn:intuitem:risk:library:iso27001-2022`).
**ISO/IEC 27017 is not shipped** as a CISO Assistant library (v4.1.0; the standard's text is
licensed). Options: build a custom framework from your licensed copy with CISO Assistant's Excel
library converter, or track the 27017 references this repository already maps per checklist item
(the `iso27017` column of the evidence export).

## Importing this repository's evidence

`tools/evidence_export.py` exports `compliance/checklist.yml` with, for every item, its status,
progress, SOC 2 / ISO 27001 / ISO 27017 controls, and the files and task names that implement and
verify it (from the `chk_*` tags, via `tools/checklist.py`):

```bash
tools/evidence_export.py --format json -o evidence.json
tools/evidence_export.py --format csv  -o evidence.csv
```

Run it in CI on every release (or monthly) so each export matches a git revision (the JSON records
it), and keep the CI run log next to it (EVD-02). Then, in CISO Assistant:

1. **Applied controls.** One applied control per checklist item (reference = the item ID, e.g.
   `BCK-01`; name = requirement; status from the export: `implemented` -> active, `designed` ->
   in progress, `not_started` -> to do). Create them once with the CSV through the data import
   wizard (Applied controls), or through the API (`POST /api/applied-controls/`) from the JSON.
2. **Map to frameworks.** Open the SOC 2 and ISO 27001 audits (compliance assessments) and link
   each requirement to the applied controls whose `soc2` / `iso27001` columns list it. The mapping is
   in the export, so this is mechanical; re-check it when the checklist changes.
3. **Evidence.** For each release, create one evidence per checklist item (or one per domain) with
   the export file and the CI run log attached, linked to the applied control. The
   `implemented_by` / `verified_by` lists say which roles and Molecule tests produced it, so an
   auditor can follow each claim to code and to a passing test.
4. **Incidents.** Attach the list of closed incident issues and postmortems (INC-01) monthly.

What this does not do yet: the import is manual (no scheduled job pushes the export through the
API), and statuses are taken from the checklist as people set them; `tools/checklist.py check`
ensures they do not claim more than the code backs.

## What was tested

Molecule `governance` (lab, one node): the TLS terminator (TLS 1.1 refused, 1.2/1.3 served,
`/metrics/` refused from non-monitoring sources), the database dump against a throwaway PostgreSQL,
the restore verification command, the backup declaration and the 0600 secret files. **Not run:**
the backend, worker and frontend containers, their health over TLS, and the API seeding (libraries,
OIDC, IdP groups): the images are on ghcr.io, whose blob storage the test sandbox cannot reach
(and the backend image is about 1.1 GB). They need a run in CI or a lab with ghcr.io access.
