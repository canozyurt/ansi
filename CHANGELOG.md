# Changelog

Every release of this repository, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); version numbers follow the rules in
[docs/releasing.md](docs/releasing.md#cutting-a-release).

How to write it:

- Every pull request adds its lines under **[Unreleased]**, in these subsections: Added, Changed,
  Deprecated, Removed, Fixed, Security, Inventory changes.
- A change that needs an edit to an organisation's inventory (a variable renamed, removed, newly
  required, or a default that changed behaviour) also gets an entry in
  [inventory-changes.yml](inventory-changes.yml) and a line under **Inventory changes** that names the
  variable. `tools/release.py check` fails until both are there.
- At release time, `tools/release.py changelog vX.Y.Z` turns [Unreleased] into the release section;
  commit that before `tools/release.py cut vX.Y.Z`.

## [Unreleased]

### Added

- First release: every service, playbook and checklist item described in the README.
- Inventory migration between releases: [inventory-changes.yml](inventory-changes.yml) lists every
  variable that is renamed, removed, newly required or changes its default; `tools/inventory_check.py`
  checks an inventory against a release before upgrading, and `site.yml` and `upgrade.yml` refuse runs
  that would silently ignore a variable. Variable deprecation policy in
  [docs/upgrades.md](docs/upgrades.md#variable-deprecation-policy).

### Inventory changes

- None.
