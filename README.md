# compliant-infra-ansible

Ansible playbooks that deploy the infrastructure a company or SaaS product needs (identity, secrets,
logging, monitoring, backup, network security and more), integrated with each other and compliant by
default with **SOC 2** (Security, Availability, Confidentiality), **ISO/IEC 27001:2022** and
**ISO/IEC 27017**.

Every role ships hardened defaults and the evidence an auditor asks for, so compliance is a side effect
of deploying instead of a separate project.

> Status: every service in the catalog and every checklist requirement is written (54 of 54 at
> "implemented"). Most service roles have passed a single-node Molecule run on Ubuntu 24.04; the newer
> audit, identity, resilience and supply-chain work, multi-node HA, Debian 12 and Ubuntu 22.04 have not
> been run yet. Nothing is "verified" until its tests pass. See the [checklist](compliance/checklist.yml).

## Supported platforms

| OS | Versions |
| --- | --- |
| Debian | 12 (bookworm) |
| Ubuntu | 22.04 (jammy), 24.04 (noble) |

Platform-specific values live in `roles/*/vars/<Distribution>.yml`, so adding another OS family does
not touch shared logic.

## Quick start

Your inventories, vault files and pipelines live in **your own private deployment repository**, never
in this one. That repository pins the release of this project it deploys, and CI checks out that
release next to your inventories (layout and rules: [docs/inventories.md](docs/inventories.md)):

```
infra-deploy/                  # your private repository
  compliant-infra.ref          # commit SHA of the compliant-infra-ansible release to deploy
  inventories/production/      # one directory per environment, started from inventories/example
  inventories/staging/
  .platform/                   # this repository at that commit (git-ignored, not committed)
```

```bash
git init infra-deploy && cd infra-deploy
echo .platform/ > .gitignore
git clone https://github.com/canozyurt/ansi.git .platform
git -C .platform checkout vX.Y.Z && git -C .platform rev-parse HEAD > compliant-infra.ref
mkdir inventories && cp -r .platform/inventories/example inventories/production
$EDITOR inventories/production/hosts.yml inventories/production/group_vars/all/main.yml

cd .platform                   # playbooks run from here, so ansible.cfg applies
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ansible-galaxy collection install -r requirements.yml
ansible-playbook -i ../inventories/production playbooks/site.yml --check --diff   # preview
ansible-playbook -i ../inventories/production playbooks/site.yml
```

Every command in this repository's docs and playbook headers is written the same way: run from the
`.platform/` checkout with `-i ../inventories/<env>`.

To try the playbooks on throwaway VMs without a deployment repository, copy the example inside a
clone of this repository instead (`cp -r inventories/example inventories/lab`, then
`-i inventories/lab`). `.gitignore` keeps every inventory except `example/` out of git, but this is a
local try-out only: nothing you configure there is versioned, reviewed or deployable from CI.

Before the first run, set at least:

- `firewall_mgmt_cidrs`: the VPN or bastion networks allowed to reach SSH. The firewall refuses to
  start without it.
- an authorized key (or `ssh_hardening_trusted_user_ca_keys`) for the deploy user. Password logins are
  disabled, and the role refuses to continue if that would lock you out.
- `rbac_model_group_assignments`: which IdP groups get which default role.

In production, run playbooks only from CI (checklist item CHG-02) and schedule a daily
`--check --diff` run to detect drift (CHG-03). [docs/ci.md](docs/ci.md) has deploy, drift and image
mirror pipelines for GitHub and Forgejo Actions to copy into the private repository that holds your
inventories.

## What is in the repo

Platform playbooks (all in `playbooks/site.yml`):

| Playbook | What it deploys |
| --- | --- |
| `baseline.yml`, `container_hosts.yml` | OS and SSH hardening, nftables firewall, time sync, auditd, automatic updates, Docker |
| `secrets.yml` | OpenBao (Raft HA): secrets, internal PKI, SSH CA, transit keys |
| `access.yml` | WireGuard VPN pair, SSH bastion with short-lived certificates, session recording, Unbound DNS |
| `identity.yml` | Keycloak cluster with MFA and the default roles |
| `monitoring.yml` | Prometheus pair, Alertmanager cluster, Grafana, node and blackbox exporters |
| `edge.yml` | HAProxy with the Coraza WAF and a keepalived VIP |
| `backup.yml` | restic to append-only rest-servers, optional S3 copy, restore tests |
| `siem.yml` | Wazuh indexer, manager and dashboard, agents on every host (logs, FIM, CIS checks) |
| `security_scanning.yml` | ClamAV on file-accepting hosts, weekly Trivy image and host scans |
| `governance.yml` | Gatus status page, CISO Assistant GRC |
| `delivery.yml` | Forgejo with protected branches, hardened Actions runners |
| `registry.yml`, `mail_relay.yml` | Optional: Zot OCI registry, Postfix outbound relay with DKIM |

Product playbooks (run separately, any number of instances, see
[docs/product-playbooks.md](docs/product-playbooks.md)):

| Playbook | What it deploys |
| --- | --- |
| `postgres.yml` | PostgreSQL clusters with Patroni, etcd, HAProxy, pgBackRest and OpenBao dynamic credentials |
| `valkey.yml` | Valkey with Sentinel, TLS and per-application ACL users |
| `rabbitmq.yml` | RabbitMQ clusters with quorum queues and Keycloak login for the management UI |
| `garage.yml` | Garage S3-compatible object storage (replication factor 3) |

Operations: `upgrade.yml` (one node at a time, backup first), `rolling_reboot.yml`,
`postgres_major_upgrade.yml`, `ha_drill.yml` (stop one node, prove failover), `dr_drill.yml` (yearly
restore into scratch, runbooks in [docs/dr/](docs/dr/README.md)), `data_export.yml` and `data_delete.yml`
(encrypted export, deletion certificate). Releases: [docs/releasing.md](docs/releasing.md); who owns what:
[docs/shared-responsibility.md](docs/shared-responsibility.md). Architecture notes per service are in
[docs/architecture.md](docs/architecture.md); access, upgrades, incident response and GRC have their own
pages under `docs/`. `tools/checklist.py` validates the checklist and `tools/evidence_export.py` exports
evidence for auditors.

## Compliance checklist

`compliance/checklist.yml` lists every requirement the project must meet, the framework controls each
one supports, and how it is checked. Tasks and tests carry tags such as `chk_iam_06`, and CI fails if a
requirement's status claims more than the code backs.

```bash
python3 tools/checklist.py check        # what CI runs
python3 tools/checklist.py report --md  # status and evidence per requirement
python3 tools/checklist.py matrix --md  # framework control -> checklist items
```

## Default role model

Every service role maps the same default roles to its native permissions, so an organization only creates
groups in its IdP and assigns roles to them:

| Role | For |
| --- | --- |
| platform-admin | Infrastructure team (privileged, time-limited elevation) |
| security-admin | Security team (privileged) |
| operator | On-call and SRE |
| developer | Product engineers |
| auditor | Auditors (read-only, time-boxed) |
| member | Every employee |

No role may delete logs or backups inside their retention period, and nobody approves their own change.
See `roles/rbac_model/defaults/main.yml` and [docs/architecture.md](docs/architecture.md).

## Upgrades

Each release tag is a tested bundle of pinned versions (`roles/versions`). Move to a new tag and run
`playbooks/upgrade.yml`: it backs up, then upgrades one node at a time with health checks. Versions are
bumped by maintainers by hand once an upstream release is judged stable. Before moving, check the inventory
against the new tag with `tools/inventory_check.py --release vX.Y.Z`: [CHANGELOG.md](CHANGELOG.md) and
`inventory-changes.yml` record every renamed, removed or newly required variable, and deprecated names keep
working until the next major release. See [docs/upgrades.md](docs/upgrades.md).

## Testing

```bash
molecule test                              # baseline roles, all platforms
molecule test -s keycloak                  # one service scenario (see molecule/ and the CI matrix)
```

Tests run in privileged systemd containers. Settings that would reach the host kernel (sysctl, auditd,
the clock) are written and checked but not applied inside the containers.

## Licence

Apache License 2.0. Tools adopted by this project must be usable under an OSI licence with SSO, audit
logs and high availability in their free edition (see [CONTRIBUTING.md](CONTRIBUTING.md)).
