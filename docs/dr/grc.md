# DR runbook: GRC tool (CISO Assistant)

**Depends on:** backup servers, OpenBao, Keycloak (SSO).
**RPO:** `grc_backup_rpo_hours`. **RTO:** `grc_backup_rto_hours`.
**Backup:** `grc` declaration: a `pg_dump` of the GRC database (`/var/backups/grc/grc.dump`) and the
attachments (`/opt/grc/data`). The declaration's `verify_command` lists the dump with `pg_restore`.

The GRC tool runs on one node by design, a documented exception to HA-01. Recovery is a restore.

## Rebuild

1. Restore the host. Deploy the tool once (`playbooks/governance.yml`) so the database exists, then
   stop the backend and frontend containers.
2. Restore the declaration into a scratch directory with `playbooks/restore.yml`. Copy the
   attachments back to `/opt/grc/data` and load the dump into the database with the postgres
   client image (`pg_restore --clean --if-exists`).
3. Start the containers. Run `playbooks/governance.yml` again.

## Verify

- `GRCDown` is clear. Sign in through Keycloak. The newest evidence item and its attachment open.

## Drill

`dr_drill_grc`: restores into scratch and runs the declaration's `pg_restore --list` check.
