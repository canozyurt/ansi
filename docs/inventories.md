# Inventories per environment (CHG-04)

Dev, test, staging and production each have their own inventory, and production data never reaches
the others. `tools/inventory_lint.py` enforces the parts that can be checked statically.

## Layout

Keep real inventories out of this repository (`.gitignore` ignores everything under `inventories/`
except `example`). Use a private repository with one directory per environment:

```
infra-inventories/
  production/
    hosts.yml
    group_vars/all/environment.yml   # inventory_environment: production
    group_vars/all/vault.yml         # ansible-vault encrypted
    group_vars/<group>/main.yml
    host_vars/
  staging/   ...                     # inventory_environment: staging
  test/      ...                     # inventory_environment: test
  dev/       ...                     # inventory_environment: dev
```

Start each one from `inventories/example` and run playbooks with `-i <repo>/<environment>`. Production
should be deployed only from CI on the protected branch (CHG-02, designed); test and dev may be deployed
from feature branches.

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

Run it against the private repository in its own CI and before every change:

```bash
python3 tools/inventory_lint.py ../infra-inventories/production ../infra-inventories/staging \
  ../infra-inventories/test ../infra-inventories/dev
python3 tools/vault_check.py ../infra-inventories
```

`inventories/example` is a template, not an environment: it is checked for rule 1 only (it declares
`production` because it shows the production layout).

## Before each upgrade

Check every inventory against the release you are moving to; see
[upgrades.md](upgrades.md#inventory-changes-between-releases):

```bash
git fetch --tags
python3 tools/inventory_check.py ../infra-inventories/production ../infra-inventories/staging \
  ../infra-inventories/test ../infra-inventories/dev --release vX.Y.Z
```

## What the lint cannot see

- Credentials: use different vault passwords and OpenBao mounts per environment, so a test operator
  cannot decrypt production secrets.
- Network: production backup servers and object stores should only accept production source networks
  (firewall rules per zone, NET-01/NET-03), so a misconfigured test host cannot reach them.
- Data copied by hand. Restores should go through `playbooks/restore.yml` run against the production
  inventory only; the lint cannot see a manual copy.
