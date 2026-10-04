# backup

restic (BSD-2) client on every host. Backs up every declaration in
`/etc/compliant-infra/backup.d/*.json` (written by `roles/backup_register`) to every server in the
`backup` group, tests restores monthly and publishes Prometheus textfile metrics.

- **What**: each service role declares paths, excludes, `pre_command` (consistent dump or snapshot),
  `post_command`, `verify_command`, `rpo_hours` and `rto_hours` through `backup_register`. This role adds
  its own declaration `host-etc` for `/etc` (`backup_host_essentials_*`).
- **Where**: one private, append-only repository per host on each backup server
  (`rest:https://<server>:8000/<inventory_hostname>/`): two independent encrypted copies, plus the
  servers' optional S3 copy. A backup server skips itself when other servers exist.
- **Encryption** (SEC-04): restic encrypts everything with AES-256 (authenticated with Poly1305) under
  the host's repository password before it leaves the host.
- **Secrets**: REST login and repository password are derived from the Vault/OpenBao secret
  `backup_password_seed` (or set per host as `backup_rest_password` / `backup_repo_password`), written
  to `/etc/compliant-infra/backup/repos/<server>.env` (0600 root) and handed to restic in its
  environment, never on a command line. Keep an offline copy of the seed: it is needed to decrypt.
- **When** (BCK-04): `compliant-backup.timer` runs at an interval derived from the smallest declared
  RPO (largest of 1, 2, 3, 4, 6, 8, 12 or 24 hours that does not exceed it), never less often than
  daily. The role fails if the schedule would not meet an RPO.
- **How**: `/usr/local/sbin/compliant-backup backup` runs each `pre_command`, backs up with
  `--host <inventory_hostname> --tag <declaration name>`, then runs `post_command`. Output goes to
  journald (`journalctl -u compliant-backup`); a failed run leaves the unit failed.
- **Restore tests** (BCK-03): `compliant-restore-test.timer` (1st of each month) restores the latest
  snapshot of each declaration from each server into a scratch directory under
  `/var/tmp/compliant-restore-test` (needs room for the largest declaration), checks the declared paths
  are there, runs `verify_command` with `RESTORE_DIR` set, records the duration against the RTO and
  deletes the scratch directory.

## Metrics

`/var/lib/node_exporter/textfile_collector/backup.prom` (written atomically):

| Metric | Labels |
| --- | --- |
| `backup_last_success_timestamp_seconds`, `backup_last_run_timestamp_seconds`, `backup_last_status`, `backup_last_duration_seconds` | name, repo |
| `backup_rpo_seconds`, `backup_rto_seconds` | name |
| `backup_restore_test_last_success_timestamp_seconds`, `backup_restore_test_last_status`, `backup_restore_test_duration_seconds` | name, repo |

## Manual restore

`playbooks/restore.yml` restores one declaration on one host:

1. List the snapshots:
   `ansible-playbook -i inventories/<env> playbooks/restore.yml -e restore_host=<host> -e restore_name=<name> -e restore_list=true`
2. Restore the latest (or `-e restore_snapshot=<id>`, optionally `-e restore_repo=<backup server>`):
   `... -e restore_host=<host> -e restore_name=<name> -e restore_confirm=<name>`.
   Files land in `/var/restore/<name>-<UTC timestamp>/` with their original absolute paths below it.
3. Stop the service, move the files (or load the dump) into place as the service's runbook says, start
   the service and check it.
4. To restore straight over the live paths, the service must be stopped first; add
   `-e restore_target=/ -e restore_in_place_confirm=<name>`.

To rebuild a lost host, create it with the same inventory name, run the baseline and
`playbooks/backup.yml` (it regenerates the same credentials from the seed), then restore. On a host
itself: `compliant-backup snapshots --name <name>` and
`compliant-backup restore --name <name> --snapshot <id> --target <dir>`.
