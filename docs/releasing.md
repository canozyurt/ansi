# Releasing

A release of this repository is a **release bundle**: a signed git tag `vX.Y.Z` whose
`roles/versions/defaults/main.yml` pins every upstream version that CI tested together, plus the
tested upgrade from the previous tag (CHG-07). Organisations apply a release as described in
[upgrades.md](upgrades.md); this page is for maintainers.

## What CI proves for every release

| Check | Where |
| --- | --- |
| Fresh install of every service on every supported OS | `molecule-services` matrix in `.github/workflows/ci.yml` |
| Upgrade from the previous release: the `*-upgrade` scenarios install `versions_upgrade_from` (and `versions_postgres_upgrade_from`) and run `playbooks/upgrade.yml` | `keycloak-upgrade`, `openbao-upgrade`, `monitoring-upgrade`, `edge-upgrade`, `postgres-upgrade`, `forgejo-upgrade` |
| Failover with one node stopped (HA-01) | `.github/workflows/nightly-ha.yml`: `keycloak-ha`, `openbao-ha`, `postgres-ha` |
| Pins, container policy, tool registry, vault, inventories, secrets | lint job |
| Per-release documents: DR runbooks (HA-02), shared-responsibility matrix (DAT-03), every upgrade scenario in CI | `tools/release.py check` |

An upgrade scenario whose service was not in the previous release has nothing to upgrade from. CI
asks `tools/release.py has-upgrade-path <scenario>`; exit code 3 skips the scenario with a notice.
The skip ends by itself once a release that includes the service has been tagged and
`versions_upgrade_from` has been bumped. `tools/release.py status` lists which scenarios are
ready and which are waiting.

Every upgrade play backs up the service before it changes a version and replaces one node at a
time behind health gates (`playbooks/upgrade.yml`). A release must not merge a service whose upgrade
play lacks either of those.

## Cutting a release

1. **Freeze.** Merge the version bumps you want (one pull request per upstream change, with the
   upstream release notes read and summarised in the PR). CI must be green on `main`, including the
   latest nightly HA run.
2. **Write the notes.**

   ```sh
   python3 tools/release.py notes v1.4.0 > /tmp/notes-v1.4.0.md
   ```

   The command lists every version change since the last tag and which upgrade paths CI tested.
   Complete the "recreate" section: services whose upgrade play stops all nodes (for example a
   Keycloak minor version, or an OpenBao release that changes the storage format). Note any
   manual step. Manual steps make it a **major** release.
3. **Review the documents** that must be current for each release:

   ```sh
   python3 tools/release.py check
   ```

   Then read [shared-responsibility.md](shared-responsibility.md) and [dr/README.md](dr/README.md)
   against what changed in the release (new services, new provider assumptions). Tick the
   checklist at the end of the notes.
4. **Tag** with semantic versioning, signed:

   | Change | Version part |
   | --- | --- |
   | Upstream patch releases, fixes | patch |
   | Upstream minor releases, new features or services | minor |
   | Anything an organisation must do by hand, or a data migration with no rolling path back | major |

   ```sh
   git tag -s v1.4.0 -F /tmp/notes-v1.4.0.md
   git push origin v1.4.0
   ```

   Publish the notes as the Forgejo/GitHub release text.
5. **Move the upgrade baseline** in the next pull request, so the following release is tested
   against this one:

   ```sh
   python3 tools/release.py bump-upgrade-from v1.4.0
   git diff roles/versions/defaults/main.yml   # versions_upgrade_from now holds v1.4.0's images
   ```

   This is the step that turns on the upgrade scenario of every service new in v1.4.0.

## Hotfix releases

Branch `release/vX.Y` from the tag, cherry-pick the fix, run CI, tag `vX.Y.(Z+1)`. Do not run
`bump-upgrade-from` for a hotfix of an older line; the baseline on `main` follows the newest release.

## Rolling back a release

See [upgrades.md](upgrades.md#rolling-back). Every upgrade play takes a backup first; the release
notes say which services migrate data and therefore need that backup to go back.
