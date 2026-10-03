# compliant-infra-ansible

Ansible playbooks that deploy the infrastructure a company or SaaS product needs (identity, secrets,
logging, monitoring, backup, network security and more), integrated with each other and compliant by
default with **SOC 2** (Security, Availability, Confidentiality), **ISO/IEC 27001:2022** and
**ISO/IEC 27017**.

Every role ships hardened defaults and the evidence an auditor asks for, so compliance is a side effect
of deploying instead of a separate project.

> Status: early development. The host baseline (OS hardening, SSH, firewall, time sync, audit logging,
> automatic updates) and the container runtime are in place. Service roles (IdP, secrets manager,
> logging, monitoring, backup, ...) come next. See [the checklist](compliance/checklist.yml) for what is
> done.

## Supported platforms

| OS | Versions |
| --- | --- |
| Debian | 12 (bookworm) |
| Ubuntu | 22.04 (jammy), 24.04 (noble) |

Platform-specific values live in `roles/*/vars/<Distribution>.yml`, so adding another OS family does
not touch shared logic.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ansible-galaxy collection install -r requirements.yml

cp -r inventories/example inventories/prod     # inventories other than example/ are git-ignored
$EDITOR inventories/prod/hosts.yml inventories/prod/group_vars/all/main.yml

ansible-playbook -i inventories/prod/hosts.yml playbooks/site.yml --check --diff   # preview
ansible-playbook -i inventories/prod/hosts.yml playbooks/site.yml
```

Before the first run, set at least:

- `firewall_mgmt_cidrs`: the VPN or bastion networks allowed to reach SSH. The firewall refuses to
  start without it.
- an authorized key (or `ssh_hardening_trusted_user_ca_keys`) for the deploy user. Password logins are
  disabled, and the role refuses to continue if that would lock you out.
- `rbac_model_group_assignments`: which IdP groups get which default role.

In production, run playbooks only from CI (checklist item CHG-02) and schedule a weekly
`--check --diff` run to detect drift (CHG-03).

## What is in the repo

| Path | Purpose |
| --- | --- |
| `playbooks/site.yml` | Full deployment: role model validation, host baseline, container hosts |
| `roles/common` | Packages, time sync (chrony), persistent journald, auditd rules, automatic security updates, login banner |
| `roles/os_hardening` | CIS Level 1 derived kernel, module, core dump, account, cron, sudo and service hardening |
| `roles/ssh_hardening` | Key or certificate only SSH, no root, modern crypto, verbose logging, SSH CA support |
| `roles/firewall` | Default-deny nftables, SSH only from management networks, declared ports, control of published container ports |
| `roles/container_runtime` | Docker Engine from the official repository with CIS Docker Benchmark daemon settings |
| `roles/rbac_model` | Default organization roles and the group-to-role map every service role uses |
| `compliance/checklist.yml` | Machine-readable compliance checklist mapped to SOC 2, ISO 27001 and ISO 27017 |
| `tools/checklist.py` | Validates the checklist against the code; prints coverage and control matrices |
| `molecule/default` | Tests every role on every supported OS: converge, idempotence, verify |

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

## Testing

```bash
molecule test                              # all platforms
molecule test --platform-name ubuntu2404   # one platform
```

Tests run in privileged systemd containers. Settings that would reach the host kernel (sysctl, auditd,
the clock) are written and checked but not applied inside the containers.

## Licence

Apache License 2.0. Tools adopted by this project must be usable under an OSI licence with SSO, audit
logs and high availability in their free edition (see [CONTRIBUTING.md](CONTRIBUTING.md)).
