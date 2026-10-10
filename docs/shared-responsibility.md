# Shared responsibility (DAT-03)

Three parties share the security of a platform built with this repository:

| Party | Who | In ISO/IEC 27017 terms |
| --- | --- | --- |
| **Repo** | this repository and its maintainers: roles, playbooks, pinned versions, CI, documents | the supplier of the platform's configuration (a supplier to the company) |
| **Company** | the organisation that runs the repository against its own inventory: operators, product teams, the security owner | cloud service **customer** of the provider; cloud service **provider** to its own product teams and end customers |
| **Provider** | the hosting provider: data centre, hardware, hypervisor, physical network, block storage, provider console | cloud service **provider** to the company |

Legend: **R** = responsible (does the work), **S** = supports (provides a mechanism, evidence or
input), **-** = no part. "Company" includes configuring the repo's mechanisms in its inventory.
The repo is never responsible for a decision that only the company can make, such as retention
periods, who gets access, or which provider to use. It ships safe defaults and refuses unsafe
settings where it can.

This document is reviewed for **every release** (docs/releasing.md); `tools/release.py check`
fails CI when an ISO 27017 cloud control, or a checklist item mapped to one, is missing here.

## ISO/IEC 27017 cloud-specific controls (CLD)

| Control | Topic | Repo | Company | Provider | How it is met | Checklist |
| --- | --- | --- | --- | --- | --- | --- |
| CLD.6.3.1 | Shared roles and responsibilities within a cloud computing environment | S: this matrix, kept current each release | R: approves the matrix, records the provider's own responsibility statement and contract, assigns owners | R: publishes its responsibility model and its certifications (ISO 27001/27017, SOC 2) | This document; provider documents stored in the GRC tool | DAT-03 |
| CLD.8.1.5 | Removal of cloud service customer assets | R: `playbooks/data_export.yml` and `playbooks/data_delete.yml` for PostgreSQL, Valkey, RabbitMQ, Garage and Forgejo (encrypted export with manifest, deletion certificate with backup-expiry date) | R: decides what to delete and when, confirms each run, keeps the certificates, tells the data owner when backups expire | R: wipes or destroys disks and volumes when the company releases them (media sanitisation per its certification); returns or deletes provider-side snapshots | Deletion certificates in `/var/lib/compliance-evidence/data-deletion/`; provider's sanitisation statement | DAT-02 |
| CLD.9.5.1 | Segregation in virtual computing environments | R: separate management, application and data zones with host firewalls (NET-03); one database, vhost, bucket or instance with its own credentials per product (DAT-01) | R: assigns zones and per-product entries in the inventory; does not share credentials | R: isolates tenants on the hypervisor, the physical network and storage | Firewall rules generated from the inventory; DAT-01 uniqueness checks | NET-03, DAT-01 |
| CLD.9.5.2 | Virtual machine hardening | R: CIS Level 1 host hardening and CIS Docker benchmark (HRD-01, HRD-03); hardened containers (role contract) | R: runs the baseline on every host, approves documented exceptions | R: hardens the hypervisor and the images it offers; the company uses its own baseline on top | CIS scan reports as evidence | HRD-01, HRD-03 |
| CLD.12.1.5 | Administrator's operational security | R: documented procedures (playbooks with confirmation variables, runbooks in docs/ and docs/dr/), session recording of privileged sessions (IAM-07) | R: follows the procedures, restricts who may run them, reviews recordings | R: secures its own administrators and console; offers MFA and audit logs for the provider console | Session recordings in the SIEM; CI run logs | IAM-07 |
| CLD.12.4.5 | Monitoring of cloud services | R: central logging (LOG-01), Prometheus alerts, SIEM | R: watches the alerts, keeps the on-call rota, reads provider status and notices | R: monitors its infrastructure, publishes incidents and maintenance, gives the company its console and API audit log | Alertmanager routes; SIEM; provider audit log exported to the SIEM where possible | LOG-01 |
| CLD.13.1.4 | Alignment of security management for virtual and physical networks | R: host firewalls default-deny, admin access only through VPN or bastion (NET-02), consistent with the zones | R: aligns provider security groups or private networks with the zones in the inventory; keeps both in one reviewed change | R: physical network security, DDoS protection at its edge, enforcement of its security groups | Inventory zones vs. provider network export, reviewed each release | NET-02 |

## ISO/IEC 27017 extended guidance on ISO 27002 controls

| Control | Topic | Repo | Company | Provider | Checklist |
| --- | --- | --- | --- | --- | --- |
| ext. 10.1.2 | Key management: who holds and rotates which keys | R: OpenBao for secrets, PKI and encryption keys with rotation periods; restic and pgBackRest repository keys; LUKS recovery keys stored in OpenBao (docs/disk-encryption.md) | R: holds the OpenBao unseal keys and break-glass material, enforces rotation, chooses customer-managed keys at the provider where offered | R: provider-side volume encryption keys and its KMS, if used (`disk_encryption_provider`) | SEC-06, SEC-04 |
| ext. 12.3.1 | Backup: what the provider backs up vs. the company | R: encrypted, append-only restic backups to at least two backup servers, monthly restore tests, yearly DR drills | R: places backup servers in different provider locations or with a second provider, sets retention | S: provider snapshots are optional extras, never the only copy | BCK-01..04, HA-02 |
| ext. 16.1.2 | Reporting security events between the parties | S: incident runbook and templates (docs/incident-response.md) | R: reports incidents to customers and authorities; subscribes to provider notices | R: notifies the company of incidents affecting it within its contract terms | INC-* |
| ext. 18.1.1 | Applicable legislation and data location | - | R: picks provider regions that meet its legal requirements, records them | R: states where data and backups are stored and processed | - |

## By layer

| Layer | Repo | Company | Provider |
| --- | --- | --- | --- |
| Physical data centre, hardware, media destruction | - | Chooses a certified provider, keeps its reports | R |
| Hypervisor, tenant isolation, provider network | - | Configures security groups and private networks to match the zones | R |
| Block storage encryption | S: checks LUKS or a declared provider (SEC-04) | R: encrypts at build time or declares and evidences provider encryption | R when declared (`disk_encryption_provider`) |
| Operating system (Debian/Ubuntu) | R: hardening, automatic security updates, rolling reboots | R: runs the baseline, approves exceptions, schedules reboots | Supplies base images (the company may use its own) |
| Containers and images | R: pinned images, scanning in CI, hardened run options, SBOMs | R: applies releases, follows vulnerability alerts | - |
| Platform services (identity, secrets, monitoring, SIEM, backups, edge, Git, registry) | R: roles, HA, upgrade paths, DR runbooks | R: operates them, holds credentials, runs drills | - |
| Data services (PostgreSQL, Valkey, RabbitMQ, Garage) | R: clusters, per-product isolation, backup, export and deletion playbooks | R: owns the data, approves exports and deletions | - |
| Product applications and their data | S: offers the data services and their controls | R: the product teams own their code, data model and data | - |
| Identities and access | R: SSO, MFA, RBAC model, access reviews tooling | R: grants and reviews access, runs joiner/mover/leaver | R: provider console accounts and MFA (the company manages its own console users) |
| Logging and monitoring | R: collection, alert rules, retention defaults | R: on-call, triage, retention decisions | R: provider-side logs and status |
| Backups and disaster recovery | R: tooling, restore tests, DR drill (HA-02) | R: second location, yearly drill, decisions during a disaster | R: availability of its regions; the company does not rely on one provider location for backups |
| Change management | R: CI gates, release bundles, tested upgrades | R: approvals, maintenance windows, production runs from CI | R: its own changes, with notice |
| Compliance evidence | R: produces evidence (tagged tasks, reports, metrics) | R: keeps evidence for 12 months, runs the audit | R: provides its certifications and reports |

## When the provider changes

Moving to another provider changes the **Provider** column. Before the move, the company records
the new provider's responsibility statement and certifications, re-checks every row marked R for
the provider, and decides whether disk encryption stays provider-side or moves to LUKS
(docs/disk-encryption.md).
