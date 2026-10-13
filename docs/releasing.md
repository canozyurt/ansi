# Releasing

A release of this repository is a **release bundle**: a signed git tag `vX.Y.Z` whose
`roles/versions/defaults/main.yml` pins every upstream version that CI tested together, plus the
tested upgrade from the previous tag (CHG-07). Organisations apply a release as described in
[upgrades.md](upgrades.md); this page is for maintainers.

## What CI proves for every release

| Check | Where |
| --- | --- |
| Fresh install of every service on every supported OS | `molecule-services` matrix in `.github/workflows/ci.yml` |
| Upgrade from the previous release: the `*-upgrade` scenarios install `versions_upgrade_from` (and `versions_postgres_upgrade_from`) and run `playbooks/upgrade.yml` | `keycloak-upgrade`, `openbao-upgrade`, `monitoring-upgrade`, `edge-upgrade`, `postgres-upgrade`, `forgejo-upgrade`, `valkey-upgrade`, `rabbitmq-upgrade`, `garage-upgrade`, `siem-upgrade`, `registry-upgrade`, `backup-upgrade`, `governance-upgrade` |
| Failover with one node stopped (HA-01) | `.github/workflows/nightly-ha.yml`: `keycloak-ha`, `openbao-ha`, `postgres-ha` |
| Pins, container policy, tool registry, vault, inventories, secrets | lint job |
| Per-release documents: DR runbooks (HA-02), shared-responsibility matrix (DAT-03), every upgrade scenario in CI, upgrade coverage of every pinned service (below) | `tools/release.py check` |

An upgrade scenario whose service was not in the previous release has nothing to upgrade from. CI
asks `tools/release.py has-upgrade-path <scenario>`; exit code 3 skips the scenario with a notice.
The skip ends by itself once a release that includes the service has been tagged and
`versions_upgrade_from` has been bumped. `tools/release.py status` lists which scenarios are
ready and which are waiting.

Every upgrade play backs up the service before it changes a version and replaces one node at a
time behind health gates (`playbooks/upgrade.yml`). A release must not merge a service whose upgrade
play lacks either of those.

### Upgrade coverage

`tools/release.py check` fails when a role reads a pinned version from `roles/versions` but is
neither in `UPGRADE_COVERAGE` (a play in `playbooks/upgrade.yml` plus an upgrade scenario whose
converge runs that playbook) nor in `UPGRADE_EXEMPT` (with the reason: scheduled jobs, client tools,
on-demand playbooks). Adding a service therefore means adding its upgrade play and scenario in the
same pull request.

| Service | Backup before the version change | One node at a time | Scenario |
| --- | --- | --- | --- |
| OpenBao | Raft snapshot | standbys first, active last, unseal after each restart | `openbao-upgrade` |
| Keycloak | `pg_dump` | rolling, or recreate when the compatibility check says so | `keycloak-upgrade` |
| PostgreSQL | pgBackRest differential backup | replicas, switchover, former leader | `postgres-upgrade` |
| Valkey | BGSAVE and copy of the RDB | replicas, Sentinel failover, former primary | `valkey-upgrade` |
| RabbitMQ | definitions export | one node at a time, back in the cluster before the next | `rabbitmq-upgrade` |
| Garage | metadata snapshot | one node at a time, healthy cluster in between | `garage-upgrade` |
| SIEM (Wazuh) | indexer snapshot to S3 (required unless waived), manager state archive | indexer, manager, dashboard `serial: 1`; agents in 20 % batches | `siem-upgrade` |
| Registry (Zot) | index and metadata archive; full backup when the client is installed (local storage) | `serial: 1` behind the edge | `registry-upgrade` |
| Backup server (rest-server) | off-site S3 copy when configured; every other server keeps a full copy | `serial: 1` | `backup-upgrade` |
| Forgejo and runners | `forgejo dump` | `serial: 1` | `forgejo-upgrade` |
| GRC (CISO Assistant) | database dump before migrations | `serial: 1` | `governance-upgrade` |
| Status page (Gatus) | none: check history only, metrics are in Prometheus | `serial: 1` behind the edge | `governance-upgrade` |
| Edge (HAProxy, WAF) | none: stateless, configuration is in git | `serial: 1`, the VIP moves | `edge-upgrade` |
| Monitoring | none: Prometheus replicas keep scraping; TSDB stays on disk | `serial: 1` | `monitoring-upgrade` |

The mail relay uses the distribution's Postfix packages, so it follows the nightly security updates
and `playbooks/rolling_reboot.yml`, not the release bundle.

## The first release (v0.1.0)

There is no tag yet, so the first release is special:

- `tools/release.py cut v0.1.0` accepts only `v0.1.0` while no tag exists, and lists every component as
  new in the notes.
- Nothing upgrades from an earlier release of this repository: organisations install v0.1.0 with
  `playbooks/site.yml`. The upgrade scenarios that are already "ready" (`tools/release.py status`)
  test the upgrade from upstream predecessor versions pinned in `versions_upgrade_from`, which the
  notes say plainly.
- After the tag, `tools/release.py bump-upgrade-from v0.1.0` fills `versions_upgrade_from` and
  `versions_postgres_upgrade_from` with what v0.1.0 deployed. That turns on every upgrade scenario
  that is still waiting, so v0.2.0 is the first release tested against a previous release for every
  service.

The maintainer decides when to tag; the tooling never creates or pushes a tag.

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
   manual step. Manual steps make it a **major** release. List changes to the pipeline examples
   (`git diff <last tag> -- examples/ci`): users keep copies of them ([ci.md](ci.md)).
3. **Review the documents** that must be current for each release, and run the pre-flight:

   ```sh
   python3 tools/release.py check
   python3 tools/release.py cut v1.4.0 --notes-out /tmp/notes-v1.4.0.md
   ```

   `cut` refuses a dirty tree, a version that is not newer than the last tag, a commit that is
   already tagged and any failing document or upgrade-coverage check. When everything passes it
   writes the notes (step 2) and prints the exact `git tag -s` command for the current commit;
   it does not tag.

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
