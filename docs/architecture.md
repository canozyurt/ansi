# Architecture

## Layers

```
 product playbooks   databases, object storage, queues; many independent instances per product
        |
 service roles       Keycloak (IdP/SSO)  <- implemented
                     secrets + PKI, logging/SIEM, monitoring, backup, VPN, bastion, edge proxy, ...
        |
 container runtime   Docker Engine with CIS Docker Benchmark settings (services run as containers)
        |
 host baseline       common, os_hardening, ssh_hardening, firewall    <- implemented
        |
 Debian 12 / Ubuntu 22.04 / Ubuntu 24.04 hosts (existing VMs or bare metal; provisioning is out of scope)
```

Hosts are placed in three network zones (`mgmt`, `app`, `data`, see the example inventory). Management
ports are reachable only from the VPN or bastion networks, and traffic between zones is allowed only by
explicit rule.

## Integration contract

Every service role plugs into the same five hooks. A service role is complete only when all five are in
place and tested.

| Hook | What the role does | Checklist |
| --- | --- | --- |
| Identity | Authenticates users through the IdP (OIDC preferred, SAML or LDAP otherwise) and maps the default roles from `rbac_model_role_groups` to native permissions. No local accounts except a break-glass account. | IAM-01, IAM-03, IAM-09 |
| Logging | Ships application, audit and admin logs to central logging; container logs go to journald, which the log shipper reads. | LOG-01 |
| Monitoring | Registers metrics endpoints, health checks and alert rules. | LOG-05 |
| Backup | Declares its stateful paths, an RTO/RPO and a backup job with restore test. | BCK-01, BCK-03, BCK-04 |
| Secrets and certificates | Reads secrets from the secrets manager at runtime and gets TLS certificates from the internal PKI. | SEC-02, SEC-03 |

Roles declare inbound ports through `firewall_rules_<role>` and published container ports through
`firewall_published_container_ports`.

## Default role model

`roles/rbac_model` defines six organization roles with a permission level per capability:

| Role | identity | secrets | logs | monitoring | hosts | delivery | backups | evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| platform-admin | admin | admin | admin | admin | admin | admin | admin | read |
| security-admin | operate | read | admin | read | read | read | read | admin |
| operator | read | operate | read | write | operate | operate | operate | read |
| developer | none | operate | read | read | none | write | none | none |
| auditor | read | none | read | read | none | read | read | read |
| member | self | none | none | none | none | none | none | none |

Organizations set `rbac_model_group_assignments` (IdP group to roles) and may add roles through
`rbac_model_extra_roles`. The role publishes `rbac_model_role_groups` (role to IdP groups), which service
roles translate into their own permission system. Rules no configuration can override: no deleting logs
or backups inside their retention period, and no approving one's own change.

## Identity provider (Keycloak)

- Runs as a container with host networking on the `keycloak` group, 3 nodes minimum; nodes discover each
  other through the shared PostgreSQL database (JDBC_PING), so no multicast is needed.
- HTTPS only (TLS 1.2+); until the PKI role exists, each node gets a self-signed certificate.
- Realm `org`: TLS required for every client, brute force lockout, 14-character password policy, 30-minute
  idle and 10-hour maximum sessions, user and admin events with details, logs in JSON to journald.
- Browser logins use the `browser-mfa` flow: password, then WebAuthn for anyone holding a role that
  includes `privileged-access` (platform-admin, security-admin), OTP for everyone else.
- Realm roles mirror `rbac_model`; IdP groups from `rbac_model_group_assignments` are created and mapped.
- The master realm keeps one break-glass admin (`keycloak_admin_user`); Keycloak flags it as a temporary
  bootstrap admin until a permanent admin flow is added.

## Monitoring and alerting

- `playbooks/monitoring.yml`: node_exporter on every host; Prometheus, Alertmanager, blackbox_exporter and
  Grafana on the `monitoring` group. All run as hardened containers (non-root, no capabilities,
  read-only root, memory and PID limits) with host networking.
- Every listener is HTTPS (TLS 1.2+, certificates from `tls_cert`). Prometheus, Alertmanager and the exporters
  require HTTP basic auth (bcrypt) via the upstream web config; nothing is anonymous. Prometheus has no
  SSO: people use Grafana, and the Prometheus/Alertmanager accounts are for machines and break-glass use.
- Prometheus runs as 2+ identical replicas. Each scrapes everything in `prometheus_scrape_catalog` (targets
  are the hosts of each entry's inventory group, when it exists) and keeps 30 days (`prometheus_retention_days`).
  Alerts carry a `replica` external label that is dropped before sending, so Alertmanager deduplicates.
- Alertmanager runs as a 3-node gossip cluster over mutual TLS (peers verified against the internal CA once
  the PKI exists). Routes: default (email + webhook), critical (shorter repeat), Watchdog to a heartbeat
  webhook (dead man's switch); critical alerts and a down host inhibit lesser ones.
- blackbox_exporter next to each replica probes the TLS handshake of every https endpoint in the catalog
  plus `blackbox_extra_targets`; certificates renew at 30 days (tls_cert) and alert at 14 (SEC-05).
- node_exporter exports timex (clock drift, LOG-03), systemd unit states (log shipper running, LOG-06) and
  a textfile collector (`/var/lib/node_exporter/textfile_collector`). A systemd timer and an apt hook write
  pending security updates, the age of the oldest one (counted from when this host first saw it) and
  the reboot-required flag (HRD-04).
- Grafana nodes are stateless: each has its own SQLite database, and everything that matters (datasources
  for every Prometheus replica, dashboards, organisation roles from the IdP) is provisioned or comes with
  the login. Losing a node loses only sessions and personal preferences, so there is nothing to back up or
  replicate. Behind the edge proxy, use sticky sessions; a user moved to another node just signs in
  again through SSO.
- Grafana logs in through Keycloak (generic OAuth, PKCE). The `roles` claim maps platform-admin to Admin,
  operator and security-admin to Editor, developer and auditor to Viewer; anything else (member) is refused
  (role_attribute_strict). The login form is hidden; the break-glass admin works through the API with basic
  auth, or the form is re-enabled with `grafana_break_glass_login_form` during an incident.
- Logs: all containers log to journald; Grafana logs JSON with request logging (`router_logging`).
  Prometheus TSDB and Alertmanager silences are not backed up (replicated across replicas/members; metrics
  older than retention are not evidence).

## Edge proxy and WAF (HAProxy, Coraza)

- HAProxy 3.4 LTS runs as a container with host networking on the `edge` group (2 nodes minimum);
  keepalived (distribution package) moves one or more VIPs between them over unicast VRRP and
  tracks HAProxy's local health endpoint, which also fails when the WAF agent is down.
- The container runs as uid 99 with no capabilities and a read-only root; the role lowers
  `net.ipv4.ip_unprivileged_port_start` to 80 on edge hosts instead of granting CAP_NET_BIND_SERVICE.
- TLS 1.2+ (Mozilla intermediate ciphers) with one certificate per service from `tls_cert`
  (organisation-supplied files, the internal PKI, or self-signed in labs); HTTP only redirects.
  Backends are re-encrypted and verified against the internal CA when one is configured.
- Services come from `edge_services` (`name, hostnames, backend_group, backend_port, backend_tls,
  health_path, public`); servers are generated from the inventory group. Keycloak (sso) is public
  with its admin console and master realm limited to `edge_admin_cidrs`; Grafana and OpenBao are
  admin-only.
- Every response gets HSTS, nosniff, X-Frame-Options, Referrer-Policy, Permissions-Policy and a
  conservative CSP when the backend sends none (services may override or keep their own).
- Stick tables limit connections and requests per source IP and per source IP per service (429).
- Coraza SPOA (Apache-2.0) with the OWASP CRS embedded in the pinned release inspects each request
  through SPOE; blocking at paranoia level 1 by default, detection-only per service or globally,
  per-service exclusions in SecLang. If the agent is unreachable requests fail closed (503).
- Access logs are JSON on stdout (journald) with request ID, service, status, TLS version and the
  WAF verdict and rule IDs; Coraza logs every rule match as JSON. Metrics: HAProxy and Coraza on
  8405 over TLS, monitoring networks only; VRRP state through the node_exporter textfile collector.

## Backups (restic and rest-server)

- Every host runs the restic client (`roles/backup`); the `backup` group (2+ nodes in separate failure
  domains) runs rest-server in a container (`roles/backup_server`), non-root, read-only, TLS 1.2+.
- Service roles declare paths, a dump `pre_command`, a `verify_command` and RPO/RTO through
  `backup_register`; the client also backs up `/etc`. Each declaration goes to every server, so there
  are two independent copies; servers can copy nightly to an S3 bucket (versioning + Object Lock recommended).
- restic encrypts on the client (AES-256); per-host REST logins and repository passwords are derived
  from one Vault/OpenBao secret and live only in 0600 root files and process environments.
- rest-server runs `--append-only --private-repos`: a host can add snapshots but cannot delete or read
  another host's. Retention (14 daily, 8 weekly, 12 monthly) runs `restic forget --prune` on the
  servers, which hold the repository passwords in a 0600 root file; root on a backup server can therefore
  decrypt its backups.
- The backup timer follows the smallest declared RPO (at least daily); a monthly restore test restores
  each declaration into a scratch directory, runs its verify command and times it against the RTO.
- Results are Prometheus textfile metrics (`backup.prom`, `backup_server.prom`) and journald logs
  (`compliant-backup`, `compliant-restore-test`, `compliant-backup-prune`); rest-server's access log goes to
  journald through the container log. Manual restores: `playbooks/restore.yml`.

## Compliance as code

1. `compliance/checklist.yml` is the source of truth for requirements and their control mappings.
2. Tasks and tests carry `chk_*` tags; `tools/checklist.py check` fails CI when a status claims more than
   the code backs.
3. CI runs lint, secret scanning, the checklist check and Molecule on every supported OS for each pull
   request.
4. In production, a weekly `--check --diff` run against the real inventory reports drift (CHG-03), and
   run logs are kept as evidence (EVD-02).

## Notes and known limits

- Docker manages its own nftables/iptables tables. The firewall role only manages `table inet compliance`
  and never flushes the whole ruleset, but stopping `nftables.service` runs the distribution's flush and
  removes Docker's rules too; restart Docker after doing so.
- Published container ports bypass the input chain. The firewall's forward chain drops new connections
  to any published port that is not listed in `firewall_published_container_ports`.
- Audit rules are immutable (`-e 2`): rule changes apply after the next reboot.
