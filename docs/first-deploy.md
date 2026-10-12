# First deploy on new hosts

A single `site.yml` run cannot build a new environment. The bastion accepts only certificates from the
OpenBao SSH CA, so it needs the CA's public key in the inventory (`ssh_hardening_trusted_user_ca_keys`),
and that key exists only after OpenBao has been initialised. `site.yml` therefore refuses to start while
the key is empty, and a first deploy runs in three stages.

| Stage | Run from | Command | Result |
| --- | --- | --- | --- |
| 1. Bootstrap | your machine, deploy key | `playbooks/bootstrap.yml` | host baseline, OpenBao initialised, SSH CA and PKI ready |
| 2. Full deploy | your machine, deploy key | `playbooks/site.yml` | everything, with the SSH CA trusted on every host; CI-only staged |
| 3. Hand over to CI | CI | `tools/ansible-run playbooks/site.yml` | CI-only deploys enforced (CHG-02) |

## Before stage 1

Fill in the inventory as described in the [README quick start](../README.md#quick-start): at least
`firewall_mgmt_cidrs`, an authorized key for the deploy user, `rbac_model_group_assignments`,
`openbao_pki_allowed_domains` and, with Keycloak SSO, `openbao_oidc_client_secret` (Ansible Vault).
Leave `ssh_hardening_trusted_user_ca_keys: []`.

## Stage 1: bootstrap

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/bootstrap.yml
```

This runs `rbac.yml`, `baseline.yml`, `container_hosts.yml` and `secrets.yml`. OpenBao is initialised and
unsealed, the configuration token is created and the root token is revoked. Keycloak does not exist yet, so
the OpenBao SSO login is left for `identity.yml` (`openbao_oidc_deferred`). The run writes to
`.openbao-init/` on your machine (git-ignored, mode 0700):

| File | Goes into the inventory as |
| --- | --- |
| `openbao-init-<time>.json` | `openbao_unseal_keys` (from `unseal_keys_b64`, shamir seal only) and `openbao_admin_token` (from `admin_token`), both in Ansible Vault |
| `openbao-ssh-user-ca.pub` | `ssh_hardening_trusted_user_ca_keys: ['<the line in the file>']` in `group_vars/all` |
| `openbao-pki-approle.json` | `pki_openbao_role_id` and `pki_openbao_secret_id` (Ansible Vault), plus `pki_openbao_addr` |

The file names start with `openbao_cluster_name`. Move the init file to offline storage once its values
are in the vault (see [secrets.md](secrets.md)). Stage 1 is safe to rerun: an initialised cluster is not
initialised again, and a new AppRole secret_id is issued each time.

For CI-only deploys (stage 3), also do the OpenBao part of the setup in
[audits.md](audits.md#production-changes-only-from-ci-chg-02): the `secret/ci/ansible-vault` secret and the CI's
`BAO_DEPLOY_ROLE_ID` and `BAO_DEPLOY_SECRET_ID`.

## Stage 2: full deploy

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/site.yml
```

With the CA key set, `baseline.yml` makes every host trust the SSH CA, `access.yml` deploys the bastion,
and `identity.yml` deploys Keycloak and then connects OpenBao to it. TLS certificates come from the
internal PKI from this run on.

With `ci_only_deploys: true`, a run from outside CI only stages CI-only mode: it maps the `ci-deploy`
principal to the deploy account and creates the break-glass account, but keeps the deploy key and its sudo,
so this run finishes from your machine.

## Stage 3: hand over to CI

Run `site.yml` from the deploy pipeline. It logs in with the CI certificate, and that run turns CI-only
enforcement on: the deploy key is removed and sudo needs the CI certificate. From then on only CI deploys,
and people use the break-glass account for emergencies
([audits.md](audits.md#production-changes-only-from-ci-chg-02)).

## Later runs

`site.yml` alone, from CI. `bootstrap.yml` is only for new environments; `secrets.yml` and `identity.yml`
on their own keep working for the services they deploy.
