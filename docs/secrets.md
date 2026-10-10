# Service secrets at runtime (IAM-08, SEC-02)

Services read their passwords, keys and tokens from OpenBao at runtime. Nothing secret stays on disk
outside tmpfs, every read is in OpenBao's audit log, and generated secrets rotate on a schedule.
Status: written and statically checked; **not yet run** against OpenBao (Molecule is on hold).

## Backends

`secrets_backend` (in `group_vars/all/identity.yml`) is set once per inventory:

| Backend | Where values live | On the host | Rotation |
| --- | --- | --- | --- |
| `vault-file` (default, labs, first install) | inventory variables encrypted with Ansible Vault | 0600 root files and env files, as before | by hand: new vault value, rerun the playbook |
| `openbao` | KV v2 `secret/services/<service>` | rendered by OpenBao Agent into `/run/compliant-secrets/<service>/` (tmpfs, mode 0400, owned by the service's uid) | `playbooks/rotate_secrets.yml`, weekly, each key after `rotate_days` |

Switching to `openbao` is safe on a running system: the first run seeds each key from its vault value
(so nothing changes for the service), then OpenBao owns it. Keys the inventory must keep owning (shared
with something outside the service, e.g. a scrape password) are `source: inventory` and follow the vault
value on every run.

## roles/openbao_secrets

A shared role the service roles include:

```yaml
- ansible.builtin.include_role:
    name: openbao_secrets
  vars:
    openbao_secrets_service: grafana
    openbao_secrets_items:
      - { name: admin_password, seed: "{{ grafana_admin_password }}", rotate_days: 90 }
      - { name: secret_key, seed: "{{ grafana_secret_key }}", rotate_days: 0 }      # encrypts data: never
      - { name: prometheus_password, source: inventory, seed: "{{ grafana_prometheus_password }}" }
    openbao_secrets_files:
      - { name: admin_password }                                  # one value per file
      - { name: app.ini, content: "{{ template_with_refs }}" }   # or a template with references
    openbao_secrets_uid: 472
    openbao_secrets_on_change: ["docker restart grafana"]
```

With `openbao` it:

1. reads the KV secret and its metadata, generates missing keys (letters and digits, `length`), rotates
   keys whose age reached `rotate_days` when `openbao_secrets_rotate` is set, and writes back with
   check-and-set; rotation times are kept in the secret's custom metadata;
2. for keys with `postgres: {group, cluster, user}` applies the new password to PostgreSQL first (SCRAM
   verifier only, on the Patroni leader), so the database never lags the service;
3. writes a policy `svc-<service>` that can only read that one secret, and an AppRole
   `svc-<service>-<host>` bound to the host's address (secret_id 90 days, renewed 30 days before expiry;
   tokens 1 h, max 4 h);
4. runs one OpenBao Agent container per service and host (pinned `versions_images.openbao`, non-root as
   the service's uid, read-only, no capabilities) that renders the files into tmpfs and re-reads every
   `openbao_secrets_render_interval`;
5. installs a systemd path unit that runs `openbao_secrets_on_change` (restart or signal) when a rendered
   file changes, staggered per node for clustered services.

The calling role then points the service at the files: `*__FILE` variables (Grafana), a config file
(`KC_CONFIG_FILE` for Keycloak, `app.ini` for Forgejo), or an env file the container sources (GRC). With
`vault-file` the role only returns the inventory values and the calling role writes its files as before.

Services migrated: Keycloak (database and bootstrap admin password), Grafana (admin password, secret
key, OIDC client secret, metrics and datasource passwords), Forgejo (database password, SECRET_KEY,
INTERNAL_TOKEN, JWT secrets, S3/cache/metrics credentials), GRC (database password, Django key,
superuser and mail passwords), oauth2-proxy (client and cookie secrets), the VPN IdP sync client
secret, and application users of Valkey and RabbitMQ (stored and rotated; the applications read them
from OpenBao with the per-user policy `svc-<cluster>-<user>`). PostgreSQL users named by a service
(`openbao_secret: {service, key}`) take their password from that service's secret.

Not migrated yet (still `vault-file` values): OpenBao's own unseal/admin material (it cannot depend on
itself), Wazuh internal users, Patroni/etcd cluster secrets, Garage RPC/admin tokens, backup repository
passwords, Prometheus/Alertmanager basic-auth machine accounts. They stay in Ansible Vault, 0600 files.

## Rotation

```bash
ansible-playbook -i inventories/<env> playbooks/rotate_secrets.yml                     # due keys only
ansible-playbook -i inventories/<env> playbooks/rotate_secrets.yml \
  -e '{"openbao_secrets_force_rotate": ["grafana/*", "forgejo/internal_token"]}'      # after a leak
```

Run it weekly from CI. The last play writes `/var/lib/compliance-evidence/secret-rotation/<date>.json`
(service, key, source, rotation period, age; never values) and node_exporter metrics
(`openbao_secret_*`). Alerts: `ServiceSecretRotationOverdue`, `SecretRotationReportStale`
(`group_vars/monitoring/identity.yml`). Keys with `rotate_days: 0` (data-encryption keys such as Grafana's
secret key and Forgejo's SECRET_KEY) are reported as "not rotated" on purpose; rotating them needs a
re-encryption procedure.

## No secret left on disk

`playbooks/verify_secrets_on_disk.yml` (role task `openbao_secrets : verify`) reads every value OpenBao
holds for the services rendered on each host and scans `/etc` and `/opt` for them (files on tmpfs and
the agent's own directory are skipped). It fails when any value is found and keeps a report (paths and
key names only) under `/var/lib/compliance-evidence/secrets-on-disk/`. The roles also delete the files
they wrote under `vault-file` once the agent renders into tmpfs.

## Risks

- Not run: the KV/AppRole API calls, agent templates and the change hooks are statically checked only.
- If OpenBao is down, running services keep their rendered files until the host reboots (tmpfs);
  after a reboot they start only when OpenBao is reachable. Keep OpenBao's 3-node Raft cluster healthy.
- The AppRole secret_id is stored in `/etc/openbao-agent/<service>/` (0400, the service's uid); it is
  bound to the host's address and can read only that service's secret.
