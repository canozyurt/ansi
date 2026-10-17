# Contributing

## Rules every change follows

1. **Secure by default.** Hardened values are the defaults. Loosening one needs an explicit variable
   and a comment saying why.
2. **Tag the checklist.** A task that contributes to a checklist item carries its tag
   (`chk_<domain>_<nn>`, e.g. `chk_net_01`). A Molecule verify task that proves it carries the same tag.
3. **Keep the checklist honest.** Update `status` (and `progress`) in `compliance/checklist.yml` in the
   same pull request. `tools/checklist.py check` fails if a status claims more than the code backs.
4. **Test on every supported OS.** New roles get Molecule coverage for Debian 12, Ubuntu 22.04 and
   Ubuntu 24.04, including idempotence.
5. **Integration contract.** A service role is not done until it covers all five hooks in
   [docs/architecture.md](docs/architecture.md#integration-contract): SSO with the default roles, log
   shipping, metrics and alerts, backup, and secrets/certificates from the central stores.
6. **High availability by default.** Service roles support at least three nodes (or two plus a witness).
   Single-node is a lab mode only.
7. **No SSO tax.** A tool qualifies as a default only if its free self-hosted edition has SSO (OIDC at
   least), audit logs and HA, under an OSI licence. Note the check in the pull request that adds it.
8. **Pin everything.** Collections, container images and tools are pinned; bump them in reviewed pull
   requests.

## Variable naming

Role variables are prefixed with the role name (`firewall_mgmt_cidrs`). Variables are part of the
interface organisations' inventories depend on: renaming, removing or newly requiring one follows the
[variable deprecation policy](docs/upgrades.md#variable-deprecation-policy) (an entry in
`inventory-changes.yml` and a line under "Inventory changes" in CHANGELOG.md). Every pull request adds
its lines to the `[Unreleased]` section of [CHANGELOG.md](CHANGELOG.md). Roles that need inbound ports
declare them as `firewall_rules_<role>` (a list in the `firewall_rules` format); the firewall role collects
every variable with that prefix.

## Adding an operating system

1. Add the version to `common_supported_platforms` in `roles/common/defaults/main.yml`.
2. Add `roles/<role>/vars/<Distribution>.yml` where package names or paths differ.
3. Add a Molecule platform and a CI matrix entry.

## Before opening a pull request

```bash
yamllint -s .
ansible-lint
python3 tools/checklist.py check
molecule test --platform-name ubuntu2404
```
