# Upgrades

This repository ships upgrades as **release bundles**. A release tag pins every
upstream version that CI has tested together, and the upgrade from the previous
release is tested in CI too. Organisations never pull new versions on their own;
they move to a new tag and run two playbooks: `upgrade.yml` for the versions, then
`site.yml` for everything else the release changes.

## For organisations: applying a release

1. Read the release notes for the new tag, especially any line marked
   **recreate**. That service has a short outage while all its nodes restart on
   the new version.
2. In your deployment repository ([inventories.md](inventories.md),
   [ci.md](ci.md)), point `compliant-infra.ref` at the new release in a reviewed
   pull request: the release's commit SHA, not the tag name, since tags can move.
3. Once it is merged, apply it in a maintenance window: run the upgrade, then the
   rest of the release, both from the `.platform/` checkout of that commit:

   ```sh
   git -C .platform fetch --tags && git -C .platform checkout "$(cat compliant-infra.ref)"
   cd .platform
   ansible-galaxy collection install -r requirements.yml
   ansible-playbook -i ../inventories/<env> playbooks/upgrade.yml \
     && ansible-playbook -i ../inventories/<env> playbooks/site.yml
   ```

   Production changes run from CI (CHG-02): the `upgrade.yml` pipeline in your
   deployment repository shows the release notes on the pull request, waits for an
   approval in the maintenance window, then runs the same two playbooks as two steps
   of one job, the second only when the first succeeded ([ci.md](ci.md#upgrades)).

4. `upgrade.yml`:
   - checks that every node is healthy before it changes anything;
   - backs up each service's data before a version change (Keycloak: a
     `pg_dump` in `/var/backups/keycloak`, the newest 5 kept; the table in
     [releasing.md](releasing.md#upgrade-coverage) lists the backup of every service);
   - upgrades one node at a time and waits for it to be healthy;
   - stops at the first node that fails, leaving the rest on the old version.

   It applies each upgraded service's whole role, so that service's new
   configuration rolls out with its new version, one node at a time.
5. `site.yml` then applies everything `upgrade.yml` does not touch: the host
   baseline and hardening, the roles that have no upgrade play (access, mail,
   scanning, audits and the rest), roles that are new in this release, and any
   new compliance controls. On the services `upgrade.yml` already handled it finds
   little or nothing to change.
6. Keep the log of both runs as change evidence (`ansible.log`, or the CI job log).

#### Why two playbooks

Every stateful service refuses a version change outside `upgrade.yml` (CHG-07):
running `site.yml` against a cluster whose versions differ from the release fails
on purpose, because a version change has to back up first and go one node at a
time. So the order is fixed:

| Order | Result |
| --- | --- |
| `upgrade.yml`, then `site.yml` | The release is applied. |
| `site.yml` first | Stops at the first stateful service whose version changed, before touching it (plays ahead of it, such as the baseline, have already run). Run `upgrade.yml`, then `site.yml` again. |
| `upgrade.yml` fails | Do not run `site.yml`. Fix the node (or roll back, below) and run `upgrade.yml` again; `site.yml` comes after a successful run. |
| `upgrade.yml` only | Versions are new but the rest of the release is not applied. The next daily drift check (`site.yml --check`, CHG-03) reports the missing changes as drift until `site.yml` runs. |

`upgrade.yml` does not run `site.yml` itself: keeping them apart lets the
version change finish, and be checked, before anything else changes, and leaves a
separate run record for each step.

### Rolling or recreate

Each service decides how it can change version safely. For Keycloak the role runs
Keycloak's own `update-compatibility` check against the running version:

| Result | What the playbook does |
| --- | --- |
| Rolling update possible | Replaces nodes one at a time, with no downtime |
| Recreate required (e.g. a new minor version) | Takes the backup, stops every node, then starts the new version node by node |

### Rolling back

- Before any data migration (rolling updates, or a failure before the first new
  node started): set `compliant-infra.ref` back to the previous release and run
  `upgrade.yml`, then `site.yml`, from that release.
- After a migration (a recreate update that completed on at least one node):
  stop the service, restore the pre-upgrade dump, then set `compliant-infra.ref`
  back and run the previous release (`upgrade.yml`, then `site.yml`).
  Newer versions may have changed the database schema, so the old version cannot
  run on the migrated data.

### Operating-system security updates

Security updates install automatically every night (`common` role). Hosts that
need a reboot are rebooted one at a time, with health checks between them, by:

```sh
ansible-playbook -i ../inventories/<env> playbooks/rolling_reboot.yml
```

Schedule it weekly in the maintenance window. Patch-age evidence is tracked as
checklist item HRD-04.

## For maintainers: preparing a release

Versions are chosen by hand. A maintainer bumps a version only after judging the
upstream release stable.

1. Change the version in `roles/versions/defaults/main.yml` (images) or
   `requirements.yml` (collections) in a pull request. Read the upstream release
   notes and record anything users must know.
2. CI runs a fresh install (`molecule/keycloak`) and the upgrade from the
   previous release (`molecule/keycloak-upgrade`), which installs
   `versions_upgrade_from` and runs `playbooks/upgrade.yml`.
3. When cutting the release:
   - tag it with semantic versioning: patch for upstream patch releases, minor for
     new upstream minors or new features, major for anything that needs manual steps;
   - write the release notes: every version change, and which services need a
     **recreate** update;
   - in the next pull request, run `tools/release.py bump-upgrade-from <tag>` to copy the
     released versions into `versions_upgrade_from`, so the next release is tested against this one.

   The full procedure, with the commands, is in [releasing.md](releasing.md).
4. A service that is added to the repo must ship its own upgrade path:
   read its version from `roles/versions`, back up before a version change, refuse
   parallel version changes outside `upgrade.yml`, and add an upgrade Molecule
   scenario. This is checklist item CHG-07.

Database major-version upgrades (for example PostgreSQL 17 to 18) move data
between formats. They get dedicated runbook playbooks in the product playbooks
rather than going through `upgrade.yml`.
