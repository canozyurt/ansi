# Disaster recovery

These runbooks rebuild each service after a disaster: the loss of a whole cluster, a site or
provider region, or data corruption that has already replicated to every node. The loss of **one
node** is not a disaster. HA handles it (HA-01, `playbooks/ha_drill.yml`): replace the node and run
the service's playbook.

Every runbook has the same parts: what the service depends on, its RPO and RTO, what the backup
holds, the rebuild steps, how to verify the result, and what the yearly drill covers.

## Yearly drill (HA-02)

```sh
ansible-playbook -i inventories/<env> playbooks/dr_drill.yml -e dr_drill_services=all -e dr_drill_confirm=yes
```

The drill restores each service's latest backup into an isolated scratch directory on one of its
nodes, verifies it with the service's own tools (PostgreSQL: a full pgBackRest restore that is
started and queried; Valkey: `valkey-check-rdb`; RabbitMQ: import into a scratch broker; Forgejo:
`git fsck` of every repository in the dump; OpenBao: snapshot checksums; and every backup's own
`verify_command`). It times the run against the RTO and checks the backup's age against the RPO,
then writes a report to `/var/lib/compliance-evidence/dr-drill/` and exports metrics:

| Alert | Fires when |
| --- | --- |
| `DRDrillOverdue` | no successful drill of a service for 13 months |
| `DRDrillNeverRan` | no drill metrics exist at all for 30 days |
| `DRDrillFailed` | the last drill of a service failed |
| `DRDrillSlowerThanRTO` | restore plus verification took longer than the RTO |

Run it once per backup server (`-e dr_drill_repo=<server>`) to prove each copy. Once a year, also
walk through a **full rebuild** in an isolated lab inventory, following the order below, as a game
day. Store its notes with the drill reports.

## Before a disaster: what must exist outside the platform

- A clone of this repository and of the company's inventory repository, on at least two admins'
  machines and in the off-site backup.
- The Ansible Vault password, the OpenBao unseal or recovery keys and the root-token procedure, the
  LUKS recovery keys and the backup password seed (`backup_password_seed`). Keep them in the sealed
  break-glass envelope (two copies, two locations). Without the seed, restic repositories cannot be
  read. Without the unseal keys, OpenBao snapshots cannot be used.
- The provider account and its MFA devices, with at least two people able to use them.
- The backup servers' data in two places: two backup servers in different locations, or one plus
  provider-independent object storage.

## Rebuild order

Dependencies decide the order: secrets and PKI before everything that gets certificates, the
database before Keycloak, Keycloak before anything that signs in with SSO.

| Step | Service | Runbook | Depends on | RTO |
| --- | --- | --- | --- | --- |
| 1 | Backup servers (the copies everything else is restored from) | [backup.md](backup.md) | hosts, the backup seed | 4 h |
| 2 | Host baseline, admin access: VPN, bastion, DNS | [access.md](access.md) | hosts, the inventory | 2 h |
| 3 | OpenBao: secrets, internal PKI, SSH CA | [openbao.md](openbao.md) | 1, 2; unseal keys | 4 h |
| 4 | PostgreSQL clusters (Keycloak, GRC and product databases) | [postgres.md](postgres.md) | 1, 3 | 4 h |
| 5 | Keycloak (SSO) | [keycloak.md](keycloak.md) | 3, 4 | 2 h |
| 6 | Mail relay (alerts and notifications) | [mail-relay.md](mail-relay.md) | 3 | 1 h |
| 7 | Monitoring and alerting | [monitoring.md](monitoring.md) | 3, 5, 6 | 2 h |
| 8 | Container registry | [registry.md](registry.md) | 1, 3, 5 | 4 h |
| 9 | Edge: HAProxy, WAF, VIPs | [edge.md](edge.md) | 3 | 1 h |
| 10 | SIEM (Wazuh) | [siem.md](siem.md) | 1, 3, 5 | 8 h |
| 11 | GRC tool | [grc.md](grc.md) | 1, 3, 5 | 8 h |
| 12 | Status page | [status-page.md](status-page.md) | 9 | 1 h |
| 13 | Forgejo and its runners | [forgejo.md](forgejo.md) | 1, 3, 4, 5, 9 | 4 h |
| 14 | Valkey instances | [valkey.md](valkey.md) | 1, 3 | per instance |
| 15 | RabbitMQ clusters | [rabbitmq.md](rabbitmq.md) | 1, 3, 5 | per instance |
| 16 | Garage clusters | [garage.md](garage.md) | 1, 3 | per instance |

The RTO of each service is its `*_backup_rto_hours` (or the instance's `backup.rto_hours`) in the
inventory. The figures above are the defaults. The platform's total RTO is the longest path through
this table. With the defaults, that is about one working day for the platform, with product data
services in parallel afterwards.

Until step 7 is done there is no alerting. Watch the playbook output and the provider console.
Until step 13 is done, production changes run from an admin machine with the inventory clone
(CHG-02 exception during a declared incident; record it in the incident log,
docs/incident-response.md).

## After a rebuild

1. Run `playbooks/site.yml` and each product playbook once more. Every run must be idempotent.
2. Run `playbooks/ha_drill.yml -e ha_drill_services=all` in a quiet hour, to check that HA works
   again.
3. Run `playbooks/dr_drill.yml -e dr_drill_services=all`, so the evidence shows the restored
   platform can restore its own new backups.
4. Write the post-incident review (docs/templates/postmortem.md), with the actual time of each
   step against the RTO.
