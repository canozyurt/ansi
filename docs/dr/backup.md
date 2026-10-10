# DR runbook: backup servers

**Depends on:** hosts, the inventory, `backup_password_seed` (break-glass envelope).
**RPO:** n.a. (this is where the other services' RPOs are kept). **RTO:** 4 h.
**Backup:** each backup server holds an independent, append-only restic repository per client
host (`/srv/restic/<host>`). Optionally a copy goes to S3 with versioning and Object Lock
(`backup_s3_*`).

## Scenarios

- **One backup server lost.** Nothing is lost: every client also writes to the other server(s).
  Rebuild the server (below) and let the next nightly backup fill it. To restore old history,
  copy the repositories from a surviving server (`restic copy`) or from S3.
- **All backup servers lost, clients intact.** Rebuild the servers. The next backup run writes
  fresh repositories. Until then the clients have no backup history: run
  `systemctl start compliant-backup` on every host at once.
- **Everything lost (site or provider).** Restore the repositories from the S3 copy (step 3), then
  continue with [README.md](README.md) step 2.

## Rebuild

1. Create the hosts with the same inventory names, on encrypted volumes (docs/disk-encryption.md).
2. `ansible-playbook -i inventories/<env> playbooks/site.yml --limit backup -t baseline,backup_server`
   (or the whole `playbooks/backup.yml` for the `backup` group).
3. Only if the repositories must come from the S3 copy: with the service stopped
   (`systemctl stop rest-server`), sync the bucket prefix back into `/srv/restic/`
   (`rclone sync` or `aws s3 sync`, using the read-only S3 key from the envelope), restore the
   ownership the role sets, and start it again.
4. Rerun `playbooks/backup.yml` for all hosts so clients pick up the servers' new TLS certificates.

## Verify

- `compliant-backup snapshots --name host-etc` on any client lists the old snapshots (scenario 3)
  or a new one (scenario 2).
- `BackupFailed`, `BackupNeverRan` and `BackupRPOExceeded` clear in Prometheus after the next
  backup run.
- Run the monthly restore test once by hand: `systemctl start compliant-restore-test`.

## Drill

The backup servers are exercised by every DR drill, because each drill restores from them. Once a
year, rebuild one backup server in the lab from the S3 copy (step 3).
