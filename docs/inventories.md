# Inventories per environment (CHG-04)

Dev, test, staging and production each have their own inventory, and production data never reaches
the others. `tools/inventory_lint.py` enforces the parts that can be checked statically.

## Layout

Real inventories never live in this repository (`.gitignore` ignores everything under `inventories/`
except `example`). They live in your private **deployment repository**, which also holds your CI
pipelines and pins the release of this project it deploys. It has one directory per environment:

```
infra-deploy/
  compliant-infra.ref                # commit SHA of the compliant-infra-ansible release to deploy
  inventories/
    production/
      hosts.yml
      group_vars/all/environment.yml   # inventory_environment: production
      group_vars/all/vault.yml         # ansible-vault encrypted
      group_vars/<group>/main.yml
      host_vars/
    staging/   ...                     # inventory_environment: staging
    test/      ...                     # inventory_environment: test
    dev/       ...                     # inventory_environment: dev
  .platform/                         # this repository at compliant-infra.ref (git-ignored)
```

Playbooks and tools run from the `.platform/` checkout, where `ansible.cfg` applies, against
`../inventories/<env>`. CI does the same: it clones the pinned release into `.platform/` and runs
from there ([ci.md](ci.md)). Moving to a new release is a reviewed change to `compliant-infra.ref`.

Start each one from `inventories/example`. Production should be deployed only from CI on the protected
branch (CHG-02; pipelines to copy in [ci.md](ci.md)); test and dev may be deployed from feature branches.

## Rules (tools/inventory_lint.py)

1. Every inventory sets `inventory_environment` to `production`, `staging`, `test` or `dev`, for all hosts
   (`group_vars/all/` or `all.vars` in hosts.yml). The variable is not called `environment` because that
   is a reserved Ansible keyword.
2. A host belongs to one inventory only: no inventory hostname and no `ansible_host` address appears in two
   inventories. A service deliberately shared with lower environments (for example the IdP) belongs to the
   production inventory and is reached from the others through its service name (`sso.example.com`), never
   by a production inventory hostname; backup and object-storage targets are never shared (rule 3).
3. A non-production inventory never names a production backup or object-storage target or restore source,
   nor a production host: values of variables about backup, restic, pgBackRest, S3, buckets, repositories,
   endpoints, snapshots or restores in the production inventory must not appear anywhere in a
   non-production one. Test data comes from synthetic or anonymised data, never from a production
   restore.

Run it in the deployment repository's CI and before every change, from `.platform/`:

```bash
python3 tools/inventory_lint.py ../inventories/production ../inventories/staging \
  ../inventories/test ../inventories/dev
python3 tools/vault_check.py ../inventories
```

`inventories/example` is a template, not an environment: it is checked for rule 1 only (it declares
`production` because it shows the production layout).

## What the lint cannot see

- Credentials: use different vault passwords and OpenBao mounts per environment, so a test operator
  cannot decrypt production secrets.
- Network: production backup servers and object stores should only accept production source networks
  (firewall rules per zone, NET-01/NET-03), so a misconfigured test host cannot reach them.
- Data copied by hand. Restores should go through `playbooks/restore.yml` run against the production
  inventory only; the lint cannot see a manual copy.
