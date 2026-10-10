# Supply chain and CI gates

What CI checks before anything merges, and the rules behind it (SEC-01, CHG-04, CHG-05, CHG-06,
EVD-01, EVD-04, HRD-02). Every gate is a small script under `tools/` that also runs locally and from
pre-commit; `python3 -m unittest discover -s tools/tests` exercises them on sample trees.

| Gate | Script / tool | Fails when | Item |
| --- | --- | --- | --- |
| Secrets in git history | gitleaks v8.28.0 with `.gitleaks.toml` (CI and pre-commit) | any finding outside the placeholder allowlist | SEC-01 |
| Vault values | `tools/vault_check.py` | a `vault_*` value outside `inventories/example` is plain text, or a `*vault*.yml` vars file is not encrypted | SEC-01 |
| Secrets in images | Trivy `--scanners vuln,secret` + `tools/trivy_gate.py` | a secret is found in an image layer and not waived | SEC-01 |
| Pins | `tools/pins.py` | an image, collection, Python tool, package or download is not pinned | CHG-05 |
| SBOM | syft (pinned image) | — (artifact `sbom-and-image-scan-<sha>`: one SPDX JSON per image plus the repository) | CHG-05 |
| Image vulnerabilities | Trivy (image and digest from `roles/versions`) + `tools/trivy_gate.py` | a CRITICAL vulnerability has no valid waiver in `compliance/vuln-waivers.yml` | CHG-05 |
| Container policy | `tools/container_policy.py` | a container task is not non-root/read-only/capability-free/limited, without a reason | HRD-02 |
| Inventories | `tools/inventory_lint.py` | see [inventories.md](inventories.md) | CHG-04 |
| Tool registry | `tools/tool_registry.py` | a `roles/versions` entry has no `compliance/tools.yml` entry, or a "no" has no exception | EVD-04 |
| Checklist | `tools/checklist.py check` | a status claims more than the code backs; a Core item marked verified has no test | EVD-01 |
| Test coverage | `tools/checklist.py coverage --md` (job summary) | report only: roles and checklist items without a test | CHG-06 |

## Pinning rule (CHG-05)

- **Images: `registry/name:tag@sha256:<digest>` is the rule.** The tag says what it is, the digest makes
  it immutable (a re-pushed tag cannot change what runs). Add the digest when you add or bump an image:
  `docker buildx imagetools inspect <image:tag>` (or `crane digest`) prints the index digest; copy it from
  that output, never from memory. Entries without a digest are reported as warnings by `tools/pins.py`;
  once every entry has one, CI switches to `tools/pins.py --require-digest`. Today only
  `versions_images.trivy` carries a digest (it runs as root with the Docker socket).
- Tags must be exact releases: never `latest`/`stable`, never a bare major such as `:3`.
- Every deployed image comes from `roles/versions` (`versions_images`); roles never write a literal image.
- Collections (`requirements.yml`) and Python tools (`requirements-dev.txt`) use exact versions.
- Binaries downloaded on hosts are in `versions_tools` with a version, a URL naming it and a SHA-256 per
  architecture; every `get_url` sets `checksum:`. A download that cannot be checksummed (a repository
  signing key) needs a `# pin-exception: <reason>` comment and should be checked another way (fingerprint).
- CI: tool images are pinned by tag (gitleaks, syft) or come from `roles/versions` (Trivy); CI downloads
  must be checked with `sha256sum` in the same step. GitHub Actions are pinned to major tags today
  (`tools/pins.py` warns); pin them to commit SHAs from the action's release page when bumping.
- Molecule platform images (`geerlingguy/docker-*-ansible:latest`) are test fixtures and only warned about.

Image **signatures** are CHG-08's job: `roles/registry` (Zot trust stores and `registry-check-signature`)
verifies cosign/notation signatures before deployment; the container policy here does not check them.

## Vulnerability waivers

`compliance/vuln-waivers.yml` lists accepted findings, each with `id`, an optional `image`/`package`
scope, a `reason`, an `expires` date (at most 180 days ahead; expired waivers fail CI) and `approved_by`.
A waiver is a risk acceptance: link the tracking issue in the reason. Weekly scheduled CI runs re-scan,
so new CVEs in unchanged images surface without a code change.

## SBOM

The `images` job generates SPDX JSON SBOMs with syft for every image in `versions_images` and for the
repository itself (collections, Python requirements, Dockerfiles), and uploads them with the Trivy reports
as the artifact `sbom-and-image-scan-<commit>` (kept 90 days by GitHub; long-term retention of CI
evidence belongs to EVD-02).

## Container policy (HRD-02)

`tools/container_policy.py` reads every `community.docker.docker_container` task that defines a
container and requires: image from `versions_images`, `cap_drop: [ALL]`, `no-new-privileges`, an explicit
`user`, `read_only: true`, `memory` and `pids_limit`, no `privileged`, no host network. Exceptions carry a
reason, inline (`# policy-exception: <rule>[,<rule>]: <reason>` inside the task) or, for project-wide
decisions, in `compliance/container-policy.yml`. Host networking is such a project-wide exception: the host
nftables firewall is the only packet filter (docs/role-contract.md). Current inline exceptions:
Keycloak, Wazuh indexer and dashboard (`read_only`: they write into the image at start) and the Wazuh
manager (`user`, `read_only`: s6 init starts as root and drops privileges per daemon).
Run `tools/container_policy.py --report` to list every exception in use.

## Secrets (SEC-01)

- Secrets live in `vault_*` variables encrypted with Ansible Vault (whole file or `!vault` strings) or are
  looked up from OpenBao at run time; inventories reference them as `{{ vault_x }}`. Real inventories are
  not in this repository (`.gitignore`); run `tools/vault_check.py <inventory repo>` there too.
- `.gitleaks.toml` extends the default rules and allows only placeholders: Molecule values named
  `test-only-*`, Jinja references and obvious placeholders (`changeme`, `<...>`).
- A leaked secret is rotated first, then removed from history; never add it to the allowlist.
