# Upgrades

This repository ships upgrades as **release bundles**. A release tag pins every
upstream version that CI has tested together, and the upgrade from the previous
release is tested in CI too. Organisations never pull new versions on their own;
they move to a new tag and run one playbook.

## For organisations: applying a release

1. Read the release notes for the new tag, especially any line marked
   **recreate**. That service has a short outage while all its nodes restart on
   the new version.
2. Check out the tag and run the upgrade in a maintenance window:

   ```sh
   git fetch --tags && git checkout vX.Y.Z
   ansible-galaxy collection install -r requirements.yml
   ansible-playbook -i inventories/<company> playbooks/upgrade.yml
   ```

3. The playbook:
   - checks that every node is healthy before it changes anything;
   - backs up each service's data before a version change (Keycloak: a
     `pg_dump` in `/var/backups/keycloak`, the newest 5 kept);
   - upgrades one node at a time and waits for it to be healthy;
   - stops at the first node that fails, leaving the rest on the old version.
4. Keep the run log as change evidence (`ansible.log`, or the CI job log).

Running `site.yml` against a cluster whose versions differ from the release
fails on purpose; version changes go through `upgrade.yml`.

### Rolling or recreate

Each service decides how it can change version safely. For Keycloak the role runs
Keycloak's own `update-compatibility` check against the running version:

| Result | What the playbook does |
| --- | --- |
| Rolling update possible | Replaces nodes one at a time, with no downtime |
| Recreate required (e.g. a new minor version) | Takes the backup, stops every node, then starts the new version node by node |

### Rolling back

- Before any data migration (rolling updates, or a failure before the first new
  node started): check out the previous tag and run `upgrade.yml` again.
- After a migration (a recreate update that completed on at least one node):
  stop the service, restore the pre-upgrade dump, then run the previous tag.
  Newer versions may have changed the database schema, so the old version cannot
  run on the migrated data.

### Operating-system security updates

Security updates install automatically every night (`common` role). Hosts that
need a reboot are rebooted one at a time, with health checks between them, by:

```sh
ansible-playbook -i inventories/<company> playbooks/rolling_reboot.yml
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
   - in the next pull request, copy the released versions into
     `versions_upgrade_from`, so the next release is tested against this one.
4. A service that is added to the repo must ship its own upgrade path:
   read its version from `roles/versions`, back up before a version change, refuse
   parallel version changes outside `upgrade.yml`, and add an upgrade Molecule
   scenario. This is checklist item CHG-07.

Database major-version upgrades (for example PostgreSQL 17 to 18) move data
between formats. They get dedicated runbook playbooks in the product playbooks
rather than going through `upgrade.yml`.
