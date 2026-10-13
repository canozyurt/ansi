# Deploy, drift and image pipelines

This repository's own CI only tests the project: lint and the compliance gates, secret scan, Molecule
scenarios, benchmark scans and HA drills (`.github/workflows/`). It deploys nothing, because the
playbooks run against your hosts, with your inventories and your credentials.

The pipelines that do touch your infrastructure are maintained here as examples that you copy into
**your deployment repository**: the private repository that holds your inventories
([inventories.md](inventories.md)). Each pipeline exists for GitHub Actions and for Forgejo Actions
(the Forgejo runners this project deploys, `roles/forgejo_runner`).

| Example | What it does | Checklist | When |
| --- | --- | --- | --- |
| `deploy.yml` | Applies `playbooks/site.yml` to production with a run record | CHG-02, EVD-02 | push to `main` touching the production inventory or the pinned release; manual |
| `drift.yml` | Check-mode run of `site.yml`; fails and alerts when a host drifted | CHG-03 | daily; manual |
| `images.yml` | Mirrors every pinned image into your Zot registry, attaches an SBOM and signs it | CHG-08 | when the pinned release changes; daily; manual |

Files: [`examples/ci/github/`](../examples/ci/github), [`examples/ci/forgejo/`](../examples/ci/forgejo)
and [`examples/ci/compliant-infra.ref`](../examples/ci/compliant-infra.ref).

## Deployment repository layout

```
infra-deploy/                       # your private repository
  compliant-infra.ref               # the compliant-infra-ansible release to deploy
  inventories/
    production/                     # see docs/inventories.md
    staging/
  .github/workflows/                # or .forgejo/workflows/
    deploy.yml
    drift.yml
    images.yml                      # only with image_registry_mirror (CHG-08)
```

Each job checks out your repository, then clones compliant-infra-ansible at the release named in
`compliant-infra.ref` into `.platform/` and runs the playbooks and tools from there against
`inventories/production` of your repository. The run record (`tools/ansible-run`) names your
repository's commit, and that commit pins the release, so every run traces back to both the
configuration and the code that applied it.

Pin a commit SHA rather than a tag in `compliant-infra.ref` (tags can be moved), and bump it in a
reviewed pull request after reading [upgrades.md](upgrades.md). To deploy from a fork, set the
repository variable `COMPLIANT_INFRA_REPO` to its clone URL.

## Setup

1. Copy the three workflow files and `compliant-infra.ref` into your deployment repository
   (`.github/workflows/` or `.forgejo/workflows/`).
2. Protect `main`: pull requests with at least one approval, no direct pushes (CHG-01; on Forgejo,
   `roles/forgejo` org_setup does this).
3. Set up the deploy identity in OpenBao and on the hosts (CHG-02):
   - declare the `ci-deploy` signing role in `openbao_ssh_extra_roles`; `roles/openbao` then creates
     the `ci-deploy` AppRole, which can only sign that certificate and read the vault password
     (`openbao_ci_deploy_*`);
   - store the Ansible vault password at `secret/data/ci/ansible-vault`, field `password`;
   - set `access_automation_cidrs` to the runner addresses (binds the AppRole secret_id and the
     deploy account's SSH source addresses) and `ci_only_deploys: true` (`roles/admin_login`).
4. Provide a runner that reaches the hosts (see below).
5. Add the variables and secrets in the table below.
6. Run `drift.yml` manually first: it changes nothing and proves the login, SSH and inventory work.

### Runners

| | GitHub Actions | Forgejo Actions |
| --- | --- | --- |
| deploy and drift | self-hosted runner with the labels `self-hosted, deploy`, on a host in `access_automation_cidrs`; Python 3.12, git, curl, jq, openssh-client | runner with a `deploy` label (add it to `forgejo_runner_labels`, e.g. `deploy:docker://<ansible tool image>`); jobs run in `ANSIBLE_RUNNER_IMAGE` |
| images | any runner that reaches your registry (`IMAGE_MIRROR_RUNNER`, default `ubuntu-24.04`) | `IMAGE_MIRROR_RUNNER`, default `docker` |

The Forgejo examples expect an Ansible tool image with ansible-core, the collections from
`requirements.yml` of the pinned release, git, curl, jq and openssh-client, pinned by digest
(`registry/.../ansible-runner:<tag>@sha256:<digest>`). Runner hosts hold deploy credentials: keep
them dedicated to this job ([forgejo_runner README](../roles/forgejo_runner/README.md)).

### Variables and secrets

| Name | Kind | Used by | Value |
| --- | --- | --- | --- |
| `BAO_ADDR` | variable | deploy, drift | OpenBao URL, e.g. `https://bao.example.internal:8200` |
| `BAO_CACERT` | variable | deploy, drift | path to the internal CA bundle on the runner (or in the tool image); empty for a public CA |
| `BAO_DEPLOY_ROLE_ID` | variable | deploy, drift | role_id of the `ci-deploy` AppRole |
| `BAO_DEPLOY_SECRET_ID` | secret | deploy, drift | a secret_id of that AppRole; expires after `openbao_ci_deploy_secret_id_ttl` (quarterly) |
| `ANSIBLE_INVENTORY` | variable, optional | deploy, drift (GitHub) | inventory path in your repository, default `inventories/production` |
| `ANSIBLE_RUNNER_IMAGE` | variable | Forgejo deploy, drift | the Ansible tool image, by digest |
| `COMPLIANT_INFRA_REPO` | variable, optional | all | clone URL of compliant-infra-ansible or your fork |
| `IMAGE_MIRROR` | variable | images | registry host, e.g. `registry.example.internal` (same as `image_registry_mirror`) |
| `IMAGE_MIRROR_RUNNER` | variable, optional | images | runner label |
| `REGISTRY_USER`, `REGISTRY_PASSWORD` | secrets | images | the `ci-mirror` robot (`registry_robots`) |
| `COSIGN_KEY` | variable | images | `hashivault://ci-image-signing` for an OpenBao transit key (preferred); empty to use the key pair below |
| `VAULT_ADDR`, `VAULT_TOKEN` | secrets | images, with transit | a token limited to `transit/sign/ci-image-signing` |
| `COSIGN_PRIVATE_KEY`, `COSIGN_PASSWORD` | secrets | images, without transit | cosign key pair |

Put the public signing key into `registry_trust_cosign_keys.ci` (`cosign public-key --key <same reference>`).

On GitHub, create an environment named `production` with required reviewers and restrict it to the
`main` branch: the deploy job then waits for a second approval, and the deploy secrets are only
available to jobs on `main`. Forgejo (v15) has no deployment environments, so the merge into the
protected branch is the approval and any workflow in the repository could read the secret: keep write
access to the deployment repository to the platform team and review changes to `.forgejo/` with care.

## What the pipelines prove

- **CHG-02**: production changes come only from `deploy.yml`; the hosts accept the deploy account
  only with the one-hour certificate the job gets from OpenBao (`ci_only_deploys`). Each run is logged
  by `tools/ansible-run` on the controller and every host (journald, identifier `ansible-run`, shipped
  to the SIEM), and the record is kept as a workflow artifact for 90 days.
- **CHG-03**: `drift.yml` fails when any host would change, lists the hosts in the job summary and
  writes `ansible_drift_changed_tasks` on each host (alerts DriftDetected, DriftCheckFailed,
  DriftCheckNotRun). See [audits.md](audits.md#drift-chg-03).
- **CHG-08**: `images.yml` mirrors, attests and signs every image of the pinned release; hosts with
  `image_signature_policy: enforce` refuse unsigned images.

Keep the workflow artifacts (or copy them into the evidence store) as long as your audit period needs.

## How the examples are maintained

The examples live next to the tools they call and change in the same pull request when a tool's
interface changes. CI in this repository checks them on every pull request without running them:

- `tools/tests/test_ci_examples.py`: both CI systems have every pipeline, the files parse, every
  `tools/` and `playbooks/` path they call exists, and this repository's own workflows contain no
  deployment settings (production inventory, deploy credentials, image mirror);
- `tools/pins.py`: actions, images and downloads in the examples are pinned (CHG-05);
- `tools/checklist.py`: the checklist tags in the examples count as evidence for CHG-02, CHG-03 and
  CHG-08;
- `yamllint`.

Release notes ([releasing.md](releasing.md)) call out changes to the examples, so compare your copies
with the examples of the release you bump to. The examples have not run against real infrastructure
yet (not tested).
