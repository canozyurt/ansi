# SSO matrix (IAM-01, IAM-03, IAM-04, IAM-09)

Every person signs in through Keycloak. The only local accounts are sealed break-glass accounts and
machine accounts. This page lists, for every UI and login path, how SSO works, what stays local, how
long a login lasts, and what happens when a user is disabled.

Status: the configuration below is written and passes static checks. The end-to-end tests
(`playbooks/test_role_matrix.yml`, `playbooks/test_offboarding.yml`) are written but **not yet run**.

## Login paths

| UI / path | SSO mechanism | Role mapping (source) | Local login | Session / token lifetime | When the user is disabled |
| --- | --- | --- | --- | --- | --- |
| Keycloak account console | Keycloak itself (realm `org`), MFA flow: WebAuthn for privileged roles, TOTP otherwise | realm roles from groups (`rbac_model_group_assignments`) | none in `org`; master realm admin console = break-glass only | SSO session 30 min idle / 10 h max; offline sessions 24 h max; access tokens 5 min | login refused at once; refresh tokens refused |
| OpenBao UI and CLI | OIDC auth method (`roles/openbao/tasks/oidc.yml`), `roles` claim -> identity groups | `openbao_role_policies` | none; root token only via generate-root with unseal key holders | tokens 1 h, max 10 h (`openbao_oidc_token_max_ttl`, capped at 24 h) | no new token; existing token expires within its TTL |
| SSH to hosts | short-lived certificates from the OpenBao SSH CA after an OIDC login (`roles/admin_login`) | `openbao_ssh_role_principals` | console only (break-glass) | certificates 8 h (`openbao_ssh_cert_ttl`, capped at 24 h) | no new certificate; the current one expires within 8 h |
| WireGuard VPN | peer keys registered in the inventory, each tied to its owner's Keycloak account by the IdP sync timer (`wireguard-idp-sync`) | owner must hold one of `wireguard_gateway_idp_required_roles` | n.a. | peer expiry date (max 366 days) | peer removed at the next sync (every 15 min) and kept out by the minute check |
| Grafana | generic OAuth (`roles/grafana`), `role_attribute_strict` | `grafana_oidc_role_map` | `break-glass-admin`, API basic auth; login form hidden unless `grafana_break_glass_login_form` | 30 min idle, 10 h max (capped at 24 h); access token refreshed every 5 min | refresh fails within 5 min; session ends |
| Prometheus UI/API | oauth2-proxy (`roles/oauth2_proxy`) on :9443, PKCE, `roles` claim checked | rbac_model capability `monitoring` >= read | basic auth on :9090 for machines (Grafana, the other replica) and break-glass | cookie 10 h max (capped at 24 h), refreshed with Keycloak every 5 min | refresh fails within 5 min; session ends |
| Alertmanager UI/API | oauth2-proxy on :9446 | capability `monitoring` >= write | basic auth on :9093 for Prometheus and break-glass | as Prometheus | as Prometheus |
| Wazuh dashboard / indexer | OpenSearch security `openid` backend, `roles` claim | `wazuh_indexer_role_mapping`, `wazuh_manager_api_role_mapping` | `break-glass-admin` (login form kept for it) and service users | IdP session | new logins refused; existing dashboard session until it expires |
| RabbitMQ management | OAuth 2.0 + PKCE, scope aliases | `rabbitmq_cluster_role_scopes` | HTTP API basic auth for the automation/break-glass `admin` | access token 5 min | token not renewed; UI asks to log in again |
| Forgejo | OIDC auth source; admin and restricted groups from the `roles` claim | `forgejo_oidc_admin_role`, `forgejo_oidc_restricted_role` | `break-glass-admin`, password sign-in off unless `forgejo_break_glass_login` | Forgejo session | new logins refused; personal access tokens must be revoked (see below) |
| GRC (CISO Assistant) | OIDC, `grc_force_sso` | `grc_role_user_groups` | break-glass superuser (`grc_admin_email`) | app session | new logins refused |
| Registry (Zot) | OIDC for browsers; `roles` claim as groups | `registry_*_roles` | `break-glass-admin` (htpasswd; also Ansible's upload account) | app session | new logins refused; API keys must be revoked |
| Status page (Gatus) | public read-only page, no login | n.a. | none | n.a. | n.a. |
| Datastores (PostgreSQL, Valkey, RabbitMQ AMQP, Garage S3, etcd) | machine-to-machine only; people go through the bastion | n.a. | per-service accounts from OpenBao (`roles/openbao_secrets`) | rotated passwords | n.a. |

The full permission matrix (which role gets what in each service) is `compliance/role-matrix.yml`,
checked against the code by `tools/role_matrix.py` in CI.

## Rules enforced by code

- **Roles, never people (IAM-03).** `tools/rbac_lint.py` runs in CI and fails when a role-mapping variable
  names something that is not a default role, when a group assignment key is an e-mail address, and on
  per-user constructs (oauth2-proxy e-mail allow lists, role expressions comparing the login, OpenBao
  roles bound to a user's claim, userpass users, per-user Keycloak role mappings, named sudoers lines,
  literal `users:` lists). Exceptions need an entry with a reason in `compliance/rbac-lint-allow.yml`.
- **Role matrix (IAM-09).** `tools/role_matrix.py` checks that `compliance/role-matrix.yml` lists every
  default role for every service and matches the variables that implement it, and that every
  role-mapping variable has a row.
- **Session cap (IAM-04).** `session_max_hours` (24) is checked by asserts in roles/keycloak (SSO and
  offline sessions), roles/openbao (OIDC tokens, SSH certificates), roles/grafana and roles/oauth2_proxy.

## Break-glass accounts

| Service | Account | Where the secret is | How it is used |
| --- | --- | --- | --- |
| Keycloak master realm | `break-glass-admin` | Ansible Vault (`vault_keycloak_admin_password`), sealed copy offline | Admin console when Keycloak's `org` realm is broken. Ansible also uses it through `admin-cli` (see Gaps). |
| Grafana | `break-glass-admin` | Vault, or OpenBao `services/grafana` | API basic auth; form only with `grafana_break_glass_login_form` |
| Forgejo | `break-glass-admin` | Vault | only with `forgejo_break_glass_login` |
| Wazuh indexer/dashboard | `break-glass-admin` | Vault | login form |
| Registry | `break-glass-admin` | Vault | htpasswd |
| GRC | superuser `grc_admin_email` | Vault | local login |
| OpenBao | root token | not stored; `generate-root` with unseal key holders | revoke right after use |
| Hosts | console root | sealed | out-of-band console |

### Central break-glass alert

One list, `break_glass_accounts` (`inventories/<env>/group_vars/all/identity.yml`), feeds the SIEM rules
in `roles/wazuh_manager/templates/compliant_infra_rules.xml.j2`:

- **100120**: any decoded successful login whose log line contains `break-glass`;
- **100121**: any decoded successful login (sshd, PAM, su, ...) whose user is in the list;
- **100122**: application log lines with a login word and a listed account (Keycloak `type=LOGIN` events,
  Grafana, Forgejo, Wazuh security audit, OpenBao audit logins);
- **100123**: an OpenBao root token generated (`sys/generate-root`) or used (`"policies":["root"]`).

All are level 12, so the manager forwards them to Alertmanager (`wazuh_manager_alert_forward_level`, 10) and
mails them (`wazuh_manager_email_alert_level`, 12). Break-glass use without an
incident ticket is itself an incident (docs/incident-response.md).

### Procedure

1. Open an incident; note who uses which account and why.
2. Unseal the credential (two people for Keycloak master and OpenBao root).
3. Use it for the minimum time; the alert fires and is acknowledged against the incident.
4. Afterwards rotate the credential (new value in the vault, run the service's playbook) and attach the
   alert and session recording to the incident.

## Offboarding

Disabling the user in Keycloak is the single required step. Within a day at most, and in practice
within minutes, it ends: Keycloak logins and refresh tokens (at once), VPN (next IdP sync, 15 min),
oauth2-proxy and Grafana sessions (next token refresh, 5 min), OpenBao tokens (TTL 1 h, max 10 h), SSH
certificates (8 h). For an immediate cut also: Keycloak "Sign out all sessions" for the user, revoke the
user's OpenBao entity tokens, and revoke Forgejo/registry personal tokens (they are not bound to the IdP
session). `playbooks/test_offboarding.yml` tests the single-step path.

## Gaps and risks

- The end-to-end tests have not run yet; mappings are checked statically only.
- Ansible's Keycloak tasks use the master-realm break-glass account through `admin-cli`. Master realm
  events are not enabled, so these logins (and a person's use of the master admin console) raise no
  Keycloak event; the alert depends on the Keycloak access log line. Moving automation to a dedicated
  service-account client and enabling master realm events would close this.
- Machine accounts with basic auth (Prometheus, Alertmanager, RabbitMQ `admin`) are not personal; their
  use is visible only in the service's access log.
- Registry `registry_read_roles` defaults to every role, so `member` can pull images (see the matrix).
