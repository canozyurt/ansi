# Product playbooks

Product playbooks deploy the data services applications use: databases, caches, queues and object
storage. Unlike service roles (one Keycloak, one OpenBao), an organisation runs **many independent
instances** of each product: one PostgreSQL cluster per application or team, a separate one for
regulated data, and so on. PostgreSQL (`playbooks/postgres.yml`, `roles/postgres_cluster`) is the
first one and sets the pattern; Valkey, RabbitMQ and Garage copy it (their design notes:
docs/datastores.md).

## The pattern

| Piece | PostgreSQL | What a new product does |
| --- | --- | --- |
| Instance list | `postgres_clusters` (in `group_vars/all/<product>.yml`) | `<product>_clusters`, same shape |
| One entry | `name`, `group`, `ports`, secrets, `databases`, `resources`, `backup`, `openbao` | `name`, `group`, `ports`, secrets, product objects, `resources`, `backup` |
| Defaults | `postgres_cluster_defaults` in the role, merged recursively under each entry | `<role>_defaults` |
| Hosts | one inventory group per instance (3 nodes), all children of `postgres` | one group per instance under `<product>` |
| Playbook | `playbooks/postgres.yml`: `include_role` once per entry whose group contains the host | same loop (`loop_var: <role>`) |
| Upgrades | a play in `playbooks/upgrade.yml` with `<role>_upgrade: true`; major versions get a runbook playbook | same |
| Firewall | `firewall_rules_<product>_<instance>` in `group_vars/<instance group>/main.yml` | same |
| Monitoring | scrape entries derived from the instance list in `group_vars/monitoring/<product>.yml`; alert rules in `prometheus_rules_<product>` | same |

### Naming

Everything an instance creates carries its name, so instances never collide and are easy to find:

- containers `<name>-postgres`, `<name>-etcd`, `<name>-haproxy`, `<name>-exporter`;
- directories `/opt/postgres/<name>/…` (data directory overridable per entry for the encrypted volume);
- systemd units `postgres-backup-<name>-{full,diff,metrics}`, `postgres-restore-test-<name>`;
- metrics files `postgres-<name>-*.prom`; backup metrics use `name="postgres-<name>"`;
- Prometheus jobs `postgres-<name>` (exporter) and `patroni-<name>`;
- OpenBao objects `database/config/postgres-<name>` and roles `postgres-<name>-<db>-<access>`.

### Isolation between instances (DAT-01)

- **Credentials**: every secret is per entry. The role collects all secrets of all entries and fails
  when one is shared with another instance (or reused inside the instance). Application users exist
  only in their instance.
- **Network**: own ports (the role fails when two instances on one host share a port) and own firewall
  rule variable per instance.
- **Coordination**: each cluster runs its own etcd whose peers and clients authenticate with
  certificates from a CA that exists only for that cluster; another cluster cannot even talk to it.
- **Data and backups**: own data directory, own pgBackRest stanza and repository path, own repository
  passphrase.
- **Processes**: own containers with their own memory, PID and shared memory limits.

Several instances may share hosts (different ports) or use separate hosts; the inventory decides.

### What each instance gets pre-wired

| Hook | PostgreSQL |
| --- | --- |
| Secrets | passwords from Vault/OpenBao in the entry, rendered only into 0600 files; SCRAM verifiers computed on the host; optional OpenBao database secrets engine for short-lived application users |
| TLS | server certificate from `tls_cert` (internal PKI, supplied files or self-signed); clients and replication over TLS 1.2+ |
| Logging | PostgreSQL, pgaudit, Patroni (JSON), etcd (JSON) and HAProxy log to stdout/stderr, i.e. journald |
| Monitoring | postgres_exporter (HTTPS + basic auth) and Patroni `/metrics`; alert rules; backup metrics with the backup role's names |
| Backup | pgBackRest: continuous WAL archiving, weekly full, daily differential, monthly restore test |
| SSO | none for the database protocol (see below) |

**SSO.** PostgreSQL has no OIDC login for the wire protocol. People do not get personal database
passwords: operators reach a cluster through the bastion and short-lived OpenBao credentials (OpenBao
itself logs in through Keycloak), and applications use their own users or OpenBao-issued ones.
Products that have a web UI or an HTTP API (RabbitMQ management, Garage admin) do integrate with
Keycloak through `oidc_client`.

## PostgreSQL design decisions

- **Image.** No maintained image bundles PostgreSQL 18, Patroni, pgaudit and pgBackRest under terms
  that fit (Spilo is built for Kubernetes and bundles several majors; Crunchy images have their own
  licence terms). The role builds a small image on each host from `ubuntu:26.04` (pinned by date):
  PostgreSQL 18 comes from Ubuntu's main archive (Canonical security maintenance for the LTS),
  pgaudit and pgBackRest from the same signed archive, all at exact versions; Patroni comes from PyPI
  at an exact version, its wheel verified by SHA-256 (`versions_postgres` in roles/versions).
  The official `postgres:18` image would need the PGDG apt repository on top; the Ubuntu base gets
  every package from one signed source and builds where only the Ubuntu archive is reachable.
  Set `postgres_cluster_image_build: false` and `postgres_cluster_image` to pull the same image from
  your registry instead of building on every host.
- **DCS.** etcd v3 (3 members, on the cluster's own hosts), not Patroni's Raft mode: Raft has been
  deprecated since Patroni 3.0 ("no guarantee nor responsibility"). etcd uses mutual TLS with a
  per-cluster CA (peers and clients), TLS 1.2+.
- **Client access.** HAProxy on every node: port 5000 always reaches the leader (Patroni
  `GET /primary`), 5001 balances over healthy replicas (`GET /replica`). HAProxy passes TLS through,
  so the client's TLS session ends at PostgreSQL. Applications list all members
  (`host=data-1,data-2,data-3 port=5000 target_session_attrs=read-write`) or a DNS name with all of
  them; a single address is possible with `roles/keepalived` on the cluster group, tracking HAProxy.
  The edge VIP is not used: the edge is an HTTP proxy in the app zone, and database traffic should not
  cross it.
- **pg_hba.** Every network line is `hostssl … scram-sha-256` or an explicit `reject`; the only other
  line is `local all postgres peer` on the container's private socket, which pgBackRest needs. The
  superuser may log in over the network only from loopback; replication only from the members.
  Behind HAProxy, PostgreSQL sees the HAProxy node as the client, so source restrictions for
  applications are enforced by the firewall rule of the client ports.
- **Roles.** Per database: `<db>_owner` (owns the database and schema; DDL), `<db>_rw` (DML) and
  `<db>_ro` (SELECT), all NOLOGIN. Login users are members of exactly one; owner-level users start
  their sessions with `SET ROLE <db>_owner` so objects belong to the group role. `CONNECT` on every
  database (including `postgres`) is revoked from PUBLIC.
- **Audit (LOG-01).** pgaudit logs DDL and role changes; `log_connections`/`log_disconnections` are on;
  everything goes to stderr and journald. The role's own maintenance session turns pgaudit off so
  password verifiers never reach the log and logs each change as a `CHANGED:` warning instead.
- **Data at rest (SEC-04).** PostgreSQL does not encrypt its files. The data directory must be on a
  LUKS (dm-crypt) volume or a provider-encrypted volume. The role looks for a `crypt` device under the
  data directory, warns (or fails with `postgres_cluster_require_encrypted_storage`) and publishes
  `postgres_data_dir_encrypted`; `postgres_cluster_storage_encryption_attested` records provider-side
  encryption the host cannot see. Backups are encrypted by pgBackRest (aes-256-cbc).
- **Backups.** `archive_command` pushes every WAL segment to every repository and `archive_timeout`
  (60 s) bounds the data loss to about a minute; full backups weekly, differential daily, both taken by
  whichever member leads when the timer fires. Repositories: S3 (`postgres_backup_s3_*`, off-site) and/or
  a posix path that every member mounts (e.g. NFS from the backup servers). The first node publishes the
  repository state with the backup role's metric names (`backup_last_success_timestamp_seconds{name=
  "postgres-<name>",repo="repoN"}`), so the existing BackupRPOExceeded/RestoreTest alerts apply, and runs
  the monthly restore test: a scratch container restores the latest backup, recovers, checks every
  database and `backup.verify_sql`, and records the duration against the RTO. `backup_register` is not
  used: restic would copy live data files, and pgBackRest already produces encrypted, consistent,
  off-host copies.
- **Changes.** Settings live in Patroni's dynamic configuration and are reconciled through its REST API
  (read, compare, `PATCH /config`); settings that need a restart are applied replicas first, then the
  leader. Container changes and minor upgrades replace one member at a time (replicas, switchover,
  former leader); version changes only run from `playbooks/upgrade.yml`, after a differential backup.
  Major versions: `playbooks/postgres_major_upgrade.yml` (logical replication into a new instance).

## Adding an instance

1. Add an inventory group with three hosts under `postgres` (or `<product>`).
2. Add an entry to `postgres_clusters` with new ports if it shares hosts with another instance, and new
   secrets in the vault (`openssl rand -base64 32`).
3. Add `firewall_rules_postgres_<name>` in `group_vars/<group>/main.yml`.
4. Run the baseline (firewall), then `playbooks/postgres.yml -e postgres_only=<name>`, then
   `playbooks/monitoring.yml` so Prometheus picks up the new targets.

## Operations

- Cluster state: `docker exec <name>-postgres patronictl -c /etc/patroni-config/patroni.yml list`.
- Planned switchover: `patronictl … switchover`, or `POST /switchover` on the REST API (basic auth).
- Backups: `postgres-backup --config /etc/compliant-infra/postgres/<name>/backup.json backup --type full`;
  `docker exec <name>-postgres pgbackrest --stanza=<name> info`.
- Point-in-time restore: stop Patroni on all members, `pgbackrest restore --type=time --target=…` on
  one member into an empty data directory, remove the cluster from etcd (`patronictl remove <name>`),
  start that member, then the others (they re-clone). Practise it with the restore test container.
- Replacing a member: etcd members join with `--initial-cluster-state new` only when the cluster is
  created; to replace a node, remove its etcd member (`etcdctl member remove`), add the new one
  (`etcdctl member add`), and deploy it with `ETCD_INITIAL_CLUSTER_STATE=existing` (not automated yet).
- etcd holds only Patroni's state; it is not backed up. If it is lost, Patroni rebuilds it from the
  running members.

## Known limits

- Backups run on the leader (`backup-standby` would need pgBackRest to reach the leader over SSH or its
  TLS server from the standby).
- No `/dev/watchdog` in containers: fencing relies on the leader key TTL (30 s).
- Synchronous replication is off by default (`patroni.synchronous_mode` per entry).
- People's access to data is through OpenBao-issued credentials, not SSO.
