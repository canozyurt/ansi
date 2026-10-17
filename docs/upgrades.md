# Upgrades

This repository ships upgrades as **release bundles**. A release tag pins every
upstream version that CI has tested together, and the upgrade from the previous
release is tested in CI too. Organisations never pull new versions on their own;
they move to a new tag and run one playbook.

## For organisations: applying a release

1. Read the release notes for the new tag, especially any line marked
   **recreate**. That service has a short outage while all its nodes restart on
   the new version, and the **Inventory changes** section, which lists every
   variable you may need to edit.
2. Check each inventory against the new tag before you check it out, and fix every
   error (see [Inventory changes between releases](#inventory-changes-between-releases)):

   ```sh
   git fetch --tags
   python3 tools/inventory_check.py ../infra-inventories/production --release vX.Y.Z
   ```

3. Check out the tag and run the upgrade in a maintenance window:

   ```sh
   git checkout vX.Y.Z
   ansible-galaxy collection install -r requirements.yml
   ansible-playbook -i inventories/<company> playbooks/upgrade.yml
   ```

4. The playbook:
   - refuses to start when the inventory still sets a removed variable or lacks a
     required one (the same checks as step 2);
   - checks that every node is healthy before it changes anything;
   - backs up each service's data before a version change (Keycloak: a
     `pg_dump` in `/var/backups/keycloak`, the newest 5 kept; the table in
     [releasing.md](releasing.md#upgrade-coverage) lists the backup of every service);
   - upgrades one node at a time and waits for it to be healthy;
   - stops at the first node that fails, leaving the rest on the old version.
5. Keep the run log as change evidence (`ansible.log`, or the CI job log).

When you deploy from CI, bump `compliant-infra.ref` in your deployment repository instead: the
`upgrade.yml` pipeline shows the release notes on the pull request, waits for an approval in the
maintenance window, then runs `upgrade.yml` and `site.yml` ([ci.md](ci.md#upgrades)).

Running `site.yml` against a cluster whose versions differ from the release
fails on purpose; version changes go through `upgrade.yml`.

## Inventory changes between releases

A release can rename a variable, remove one, make one required, or change a default.
Each such change is an entry in [inventory-changes.yml](../inventory-changes.yml),
added in the pull request that makes it, and a line under **Inventory changes** in
[CHANGELOG.md](../CHANGELOG.md) and the release notes. Three things read that file:

| Where | What it does |
| --- | --- |
| `tools/inventory_check.py <inventory> --release vX.Y.Z` | Before an upgrade, without touching hosts: reads the entries of the tag and reports what the inventory must change. Run it in the inventory repository's CI against the next release too. |
| `playbooks/inventory_check.yml` (first play of `site.yml` and `upgrade.yml`) | At run time: fails before any change when a removed variable is set, both names of a renamed variable are set, or a required variable is missing for a host. Copies deprecated names to their new names, so they keep working. |
| `tools/release.py check` (CI) | For maintainers: a role default that disappears without an entry, an entry that was edited after its release, or a missing changelog line fails the build. |

What the checker reports:

| Level | Meaning | What to do |
| --- | --- | --- |
| ERROR | A removed variable is still set; both names of a renamed variable are set; a required variable is missing for a host | Fix before upgrading; the playbooks refuse to run |
| WARN | A deprecated name is still set (it still works); a required variable may sit in a fully encrypted vars file the checker cannot read | Rename it before the release named in the message; check the encrypted file yourself |
| NOTE | A default changed and the inventory does not set the variable | Nothing, unless you want the old behaviour: set the variable to the old value |

The checker reads variable names only, so `!vault` values are fine. Vars files that
are encrypted as a whole hide their names; prefer inline `!vault` values (as
`tools/vault_check.py` expects) so the check sees them. `--strict` turns warnings
into failures, for inventory CI that wants no deprecated names.

## Variable deprecation policy

Variables are part of this repository's interface. Changing one follows these rules:

1. **Renaming** keeps the old name working for at least one minor release, and until
   the next major release at the earliest. The pull request adds a `renamed` entry
   with `removal_planned` set to that major release. The roles read only the new
   name; the run-time check copies the old one over and prints a deprecation notice.
2. **Removing** a variable, or dropping a deprecated name, happens only in a major
   release. The pull request adds a `removed` entry, so an inventory that still sets
   it fails the check instead of having its value silently ignored.
3. **Making a variable required** (no default) happens only in a minor or major
   release and needs a `required` entry naming the groups it applies to.
   `inventories/example` must set it, which `tools/release.py check` verifies.
4. **Changing a default** that changes behaviour on existing hosts gets a `default`
   entry with the old and new values, so organisations can pin the old one.
5. Entries are never edited or deleted once a release contains them. A mistake is
   corrected with a new entry.

A release that removes a variable or adds a required one without a default is a
**major** release if organisations must act before upgrading ([releasing.md](releasing.md#cutting-a-release)).

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
