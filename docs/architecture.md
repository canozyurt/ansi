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
