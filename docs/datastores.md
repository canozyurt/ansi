# Valkey, RabbitMQ and Garage

Three product playbooks that follow the pattern in docs/product-playbooks.md: a list of independent
instances (`valkey_clusters`, `rabbitmq_clusters`, `garage_clusters` in `group_vars/all/<product>.yml`),
one inventory group per instance under the product group, `firewall_rules_<product>_<instance>` in the
instance group's `main.yml`, scrape jobs in `prometheus_scrape_extra_<product>` and alert rules in
`prometheus_rules_<product>` (`group_vars/monitoring/<product>.yml`). Each role fails when two instances
share a secret or a host port (DAT-01) and when an instance has fewer than 3 nodes outside lab mode
(`<role>_allow_single_node`, HA-01). Version changes run only from `playbooks/upgrade.yml`, after a
backup, one member at a time (CHG-07). Product playbooks run on their own, like `playbooks/postgres.yml`.

| | Valkey | RabbitMQ | Garage |
| --- | --- | --- | --- |
| Licence | BSD-3-Clause | MPL-2.0 | AGPL-3.0 |
| Image (roles/versions) | `valkey/valkey`, `oliver006/redis_exporter` | `rabbitmq:<ver>-alpine` | `dxflrs/garage`, HAProxy (edge image) |
| Containers | `<name>-valkey`, `<name>-sentinel`, `<name>-exporter` | `<name>-rabbitmq` | `<name>-garage`, `<name>-garage-tls` |
| Directories | `/opt/valkey/<name>` | `/opt/rabbitmq/<name>` | `/opt/garage/<name>` |
| Client ports (TLS) | 6379, Sentinel 26379 | AMQPS 5671, UI/API 15671 | S3 3900 |
| Member ports | 6379, 26379 | epmd 4369, distribution 25672 | RPC 3901 |
| Metrics (TLS) | 9121 (basic auth) | 15691 (basic auth) | 3904 (bearer metrics token) |
| Prometheus job | `valkey-<name>` | `rabbitmq-<name>` | `garage-<name>` |
| Backup (`backup_register`) | `valkey-<name>`: BGSAVE + RDB copy | `rabbitmq-<name>`: definitions JSON | `garage-<name>`: metadata snapshot |

## Valkey

- **HA.** The first node of the group starts as primary, the others replicate it; one Sentinel per node
  (3 Sentinels, quorum 2) fails over. Applications use a Sentinel-aware client with every node listed.
  Changes roll replicas first, then a Sentinel failover, then the former primary.
- **TLS.** `port 0`: no plaintext listener. Clients, replication and Sentinel use TLS 1.2+ with the
  node certificate from `tls_cert`.
- **ACLs.** The `default` user is off. Built-in users each have one job: `admin` (automation and
  break-glass), `replicator`, `sentinel`, `exporter` and `backup`. Application users get their key
  prefix (`~<app>:*`), their channels and data commands only; CONFIG, DEBUG, FLUSHALL/FLUSHDB, KEYS,
  MODULE, ACL and other `@admin`/`@dangerous` commands are denied. `enable-debug-command`,
  `enable-module-command` and `enable-protected-configs` are `no`. The ACL file holds SHA-256 hashes only.
- **Persistence.** AOF `everysec` plus RDB snapshots. The backup pre-command runs BGSAVE as the
  `backup` user, waits for it and copies `dump.rdb`.

## RabbitMQ

- **Cluster.** 3 nodes per cluster, classic peer discovery with every member listed,
  `cluster_partition_handling = pause_minority`, `default_queue_type = quorum`. A host belongs to one
  RabbitMQ cluster only, because the Erlang port mapper (epmd) is per host.
- **TLS.** AMQP only on 5671 (no plaintext 5672), the management UI and HTTP API only on 15671 and
  the Prometheus plugin only on 15691, all TLS 1.2+.
- **Inter-node TLS.** Erlang distribution runs over `inet_tls`. The server and the CLI tools
  (`rabbitmqctl`, `rabbitmq-diagnostics`) load the same option file through
  `RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS` and `RABBITMQ_CTL_ERL_ARGS`, so they always speak the same
  protocol. A CLI tool that spoke plain TCP to a TLS node would fail with "TCP connection succeeded but
  Erlang distribution failed".
  - With certificates from the internal PKI or organisation files, the connecting side verifies the
    other node's certificate (`verify_peer`).
  - With temporary self-signed certificates (labs, and the first run before the PKI exists), the
    connection is encrypted but the certificate is not verified. Erlang's ssl application rejects a
    lone self-signed peer certificate (`{bad_cert, selfsigned_peer}`) even when that certificate is in
    `cacertfile`.
  - In both cases the per-cluster Erlang cookie authenticates members and CLI tools. No client
    certificates are requested, because the internal PKI issues server certificates.
- **Management UI login (IAM-01).** OAuth 2.0 with Keycloak (authorization code with PKCE, public
  client registered through `oidc_client`, plus an audience mapper so tokens name the cluster). The
  realm's `roles` claim maps through scope aliases:
  - platform-admin → administrator;
  - operator → monitoring and policymaker (vhost access without message access);
  - developer → management, with configure, write and read on the entry's `developer_vhosts`;
  - auditor and security-admin → monitoring (read-only).

  Basic auth is disabled in the UI and remains for the HTTP API, where the break-glass admin and
  Ansible use it.
- **Users.** Vhosts, users and permissions are reconciled through the HTTP API, with passwords sent as
  salted hashes. Application users get permissions on their own vhost only and no tags. Undeclared
  users, `guest` included, are deleted.
- **Backups.** `rabbitmqctl export_definitions` covers vhosts, users with their hashes, permissions,
  policies, queues, exchanges and bindings. Messages are transient by design and are not backed up:
  quorum queues replicate them across the members, and producers must be able to resend. Restore with
  `rabbitmqctl import_definitions`.

## Garage

- **Cluster.** 3 nodes per cluster. `replication_factor = 3` and `consistency_mode = consistent` are
  set at creation and cannot change later. Ansible applies the layout through the admin API v2: it
  connects the nodes, then stages and applies each node's zone (`garage_cluster_zone` per host) and
  capacity. It never removes a node from the layout automatically.
- **TLS.** Garage serves plain HTTP only, so an HAProxy sidecar using the edge role's pinned image
  terminates TLS 1.2+:
  - S3 on 3900, passed through as TCP, so SigV4 signatures stay valid;
  - only `/metrics` and `/health` on 3904.

  Garage's own S3 and admin listeners are bound to 127.0.0.1. RPC between members (3901) is encrypted
  and authenticated with the per-cluster RPC secret.
- **Keys and buckets.** Access keys are generated outside Garage (`GK` + 24 hex, 64-hex secret), kept
  in Vault/OpenBao and imported. Keys cannot create buckets. Each bucket grants read, write or owner to
  the keys it names, and grants that are not declared are revoked. The admin token stays on the host;
  Prometheus uses a separate metrics token.
- **Data at rest (SEC-04).** Garage does not encrypt blocks or metadata, so LUKS (or attested provider
  encryption) on the data and metadata directories is a host prerequisite. The role checks it and
  publishes `datastore_data_dir_encrypted`. Applications that need per-object keys can use SSE-C
  (customer-provided keys, supported by Garage's S3 API), keeping the keys in OpenBao.
- **Backups.** Garage takes a metadata snapshot (`garage meta snapshot`, plus automatic snapshots every
  6 h), and `backup_register` copies the snapshots directory. Object data relies on 3 replicas. Buckets
  that need an off-cluster copy must be copied to another S3 target.
- **Garage as the S3 target for other roles.** Each consumer gets its own bucket and key, with no key
  shared. Use endpoint `https://<node or load-balanced name>:3900`, region `garage` and path-style
  addressing, and trust the internal CA. Consumers:
  - the backup role's `backup_s3_*` (restic copy);
  - Wazuh indexer snapshots;
  - Forgejo's object storage (`forgejo_s3_*`).

  Do not back up a Garage cluster into itself.

## Known limits

- Molecule scenarios run each product on one node in lab mode (single Valkey without failover, a
  one-node RabbitMQ cluster, replication factor 1 in Garage). Failover, partitions and rolling
  replacement across 3 nodes have not been exercised.
- RabbitMQ: without the internal PKI, inter-node TLS is encrypted but unverified (see above). No OIDC
  login exists for AMQP; applications use their own users.
- Garage: the admin API is reachable only on the node itself; Ansible uses it locally. No OIDC login.
- Valkey has no SSO for the wire protocol. People reach it through the bastion with the admin user
  from OpenBao.
