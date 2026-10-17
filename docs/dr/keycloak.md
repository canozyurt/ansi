# DR runbook: Keycloak (SSO)

**Depends on:** PostgreSQL (the `keycloak` database), OpenBao (TLS), the edge.
**RPO:** that of the PostgreSQL cluster holding `keycloak_db_name`. **RTO:** 2 h.
**Backup:** all state is in the database: realms, clients, users, credentials, sessions that
survive restarts. Before every version change, `playbooks/upgrade.yml` also writes a `pg_dump` to
`/var/backups/keycloak` (the newest 5 are kept). The realm configuration this repository manages
is also in the inventory.

## Rebuild

1. Restore the database first ([postgres.md](postgres.md)), or check that it survived.
2. `ansible-playbook -i ../inventories/<env> playbooks/identity.yml`. The nodes form a cluster again
   over JDBC_PING. The role reconciles the realm, clients, roles and identity providers from the
   inventory.
3. If the database was lost and only a pre-upgrade dump is newer than the cluster backup, load the
   dump into an empty `keycloak` database before step 2:
   `pg_restore -d keycloak /var/backups/keycloak/<file>.dump` (with the postgres client image).
4. Users who registered or changed credentials after the restore point must reset them. Send the
   notice from the incident.

## Verify

- `https://<sso host>/realms/<realm>/.well-known/openid-configuration` answers through the edge.
- A test user signs in to Grafana with MFA. The break-glass admin login raises its alert as
  designed.

## Drill

Covered by the PostgreSQL drill (its database). The HA drill (`keycloak-ha`, nightly) proves node
loss. The yearly game day rebuilds Keycloak on a restored database in the lab and signs in.
