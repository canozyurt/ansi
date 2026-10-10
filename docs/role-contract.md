# Service role contract

How to write a service role so it plugs into the rest of the repo. `roles/keycloak` is the
reference implementation; copy its structure.

## Layout

```
roles/<svc>/
  defaults/main.yml      # every variable prefixed <svc>_; image from versions_images.<svc>
  meta/main.yml          # dependencies: versions (and rbac_model if it maps roles)
  tasks/main.yml         # asserts, then import_tasks: deploy.yml, config, health
  tasks/health.yml       # waits until this node is healthy (used by upgrade and rolling reboot)
  tasks/upgrade_prepare.yml / upgrade_apply.yml   # stateful services: see Upgrades
  handlers/main.yml      # restarts use throttle: 1
  templates/
molecule/<svc>/          # single-node lab test: converge, idempotence, verify
```

## Rules

| Area | Rule | Checklist |
| --- | --- | --- |
| Versions | Image comes from `versions_images.<svc>` in `roles/versions/defaults/main.yml`, pinned to an exact tag. `roles/versions` rewrites it to `image_registry_mirror` when set; list `image_verify` in `meta/main.yml` with the role's images so their signatures are checked before any container is created. | CHG-05, CHG-08 |
| HA | Assert at least `<svc>_min_nodes` (3, or 2 plus a witness) unless `<svc>_allow_single_node`. | HA-01 |
| Containers | `community.docker.docker_container`, `network_mode: host`, non-root user, `cap_drop: [ALL]` (add back only what is needed, with a comment), `security_opts: [no-new-privileges:true]`, `read_only: true` where the image allows, memory and pids limits, `restart_policy: unless-stopped`. | HRD-02 |
| Secrets | Passwords and tokens are role variables the inventory fills from Ansible Vault or OpenBao lookups. Render them only into 0600 root files (env files) with `no_log: true`. Never on a command line. | SEC-01, SEC-02 |
| TLS | Every listener is TLS 1.2+. Get the certificate with `include_role: tls_cert` (it uses the internal PKI when `pki_openbao_addr` is set, organisation files, or self-signed for labs); restart when `tls_cert_changed`. | SEC-03, SEC-05 |
| Firewall | Declare inbound ports as `firewall_rules_<svc>` in `inventories/example/group_vars/<group>/main.yml`, restricted to the CIDRs that need them, with a `class` (admin, database, service, cluster, metrics, public). Admin and database ports allow only `firewall_mgmt_cidrs` plus named client hosts or zones, never `any` or the whole mgmt zone; traffic from another zone needs a flow in `compliance/zone-flows.yml`. Regenerate `docs/network.md` with `tools/firewall_audit.py`. | NET-01, NET-02, NET-03 |
| Identity | Web UIs and APIs log in through Keycloak: `include_role: oidc_client`, then map the `roles` claim (default roles: platform-admin, security-admin, operator, developer, auditor, member) to native permissions. The only local account is a break-glass admin. | IAM-01, IAM-03, IAM-09 |
| Logging | Containers log to journald (Docker's default here). Application audit logs go to stdout or a file the SIEM agent reads; say which in the role README section of docs/architecture.md. | LOG-01 |
| Monitoring | Expose Prometheus metrics (TLS where the service supports it) and report the endpoint (port, path, scheme) so the monitoring catalog can scrape it; add alert rules for the service. | LOG-05 |
| Backup | Stateful services `include_role: backup_register` with their paths, a pre-command that produces a consistent copy (dump or snapshot), and RPO/RTO. | BCK-01, BCK-04 |
| Upgrades | Stateful services refuse a version change outside `playbooks/upgrade.yml` (one node at a time), back up first, and ship `molecule/<svc>-upgrade` when an older pinned version exists. | CHG-07 |
| Evidence | Tag tasks with `chk_<domain>_<nn>`; tag the Molecule verify task that proves it with the same tag. | EVD-01 |

## Testing

Molecule scenarios run Docker inside a privileged Ubuntu 24.04 test container. Copy
`molecule/keycloak/molecule.yml` (anonymous `/var/lib/docker` volume, `storage-driver: vfs`,
`default-ulimits: {}`) and `molecule/keycloak/converge.yml` (installs `container_runtime` and routes
image pulls through `MOLECULE_HTTPS_PROXY` when set). Single-node lab settings go in the scenario's
`group_vars`. Verify with tagged asserts against the running service, not just the files.

Before a pull request:

```bash
yamllint -s .
ansible-lint
python3 tools/checklist.py check
molecule test -s <svc>
```
