# DR runbook: PostgreSQL clusters

**Depends on:** backup servers or the S3 repository (pgBackRest), OpenBao (TLS certificates, the
repository cipher key), admin access.
**RPO:** WAL archiving is continuous, so normally minutes. The full-backup RPO is the cluster's
`backup.rpo_hours`. **RTO:** `backup.rto_hours` (default 4 h).
**Backup:** pgBackRest full, differential and WAL archive per cluster (stanza = cluster name), in
each configured repository (`aes-256-cbc` encrypted). The monthly restore test (`postgres-backup
restore-test`) proves every repository.

## Scenarios

- **One member lost:** Patroni fails over. Replace the node and run `playbooks/postgres.yml`. It
  re-clones from the leader. Not a disaster.
- **All members lost, or the data was destroyed by a bad change** (`DROP`, a faulty migration):
  restore the cluster from pgBackRest. For a bad change, restore to a point in time just before it.
- **One database or table needed back, cluster fine:** restore into scratch (the restore test's
  procedure), `pg_dump` what you need from the scratch copy and load it. Do not roll back the
  whole cluster.

## Rebuild the whole cluster

The role creates an empty cluster. It does not bootstrap from a backup by itself, so the restore
is a manual step between two playbook runs:

1. Restore the hosts (access runbook). Run `playbooks/postgres.yml -e postgres_only=<name>`
   (or the whole playbook) so that containers, configuration, certificates and the pgBackRest
   configuration exist. Patroni initialises an empty cluster.
2. Stop Patroni on **all** members (`docker stop <name>-postgres`, replicas first), then remove the
   cluster from etcd so it can be bootstrapped again:
   `docker exec <name>-etcd etcdctl del --prefix /service/<name>/` (the role's namespace; check with
   `etcdctl get --prefix --keys-only /`).
3. On the **first** member, empty the data directory and restore into it, using the server image
   with the cluster's pgBackRest configuration and repository mounts (the same mounts as
   `/usr/local/sbin/postgres-backup restore-test` uses, see `/etc/compliant-infra/postgres/<name>/backup.json`):

   ```sh
   docker run --rm --user <uid> -v /opt/postgres/<name>/data:/var/lib/postgresql/data \
     -v /opt/postgres/<name>/pgbackrest:/etc/pgbackrest:ro [-v <posix repo>:<ct path>:ro] \
     --entrypoint pgbackrest <server image> --stanza=<name> --pg1-path=/var/lib/postgresql/data \
     --delta restore                                  # latest
     # point in time: --type=time --target="2026-10-09 13:59:00+00" --target-action=promote
   ```

4. Start Patroni on the first member. It finds a valid data directory and no cluster in etcd, takes
   the leader key and starts. Then start the other members. They re-clone from the new leader.
5. Run `playbooks/postgres.yml` again. Users, databases, pgaudit settings and passwords are
   reconciled from the inventory, which also restores passwords rotated after the backup.
6. Take a new full backup right away (`postgres-backup --config ... backup --type full`). After a
   point-in-time restore the cluster is on a new timeline.

## Verify

- `patronictl list` shows one leader and streaming replicas. `PatroniNoLeader` and `PostgresDown`
  are clear.
- The cluster's `backup.verify_sql` returns true, and the applications' own health checks pass.
- `PostgresWALArchiveFailing` stays clear after the first WAL switch.

## Drill

`dr_drill_postgres` runs `postgres-backup restore-test` on the first member: a full restore of the
latest backup plus WAL, from every repository, into a scratch container without network. The copy
is started, every database is checked with `verify_sql`, and the run is timed against the RTO. The
yearly game day does steps 1 to 6 in the lab, including one point-in-time restore.
