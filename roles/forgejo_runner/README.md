# forgejo_runner

Forgejo Actions runner (`data.forgejo.org/forgejo/runner`, pinned in `roles/versions`) on the
`forgejo_runner` group, two or more hosts. CI jobs, and the deployments that make production
changes (CHG-02), run here.

## Registration

The vault holds one secret, `forgejo_runner_registration_secret` (40 hex characters,
`openssl rand -hex 20`). Each runner derives its own token as `sha1(secret + ":" + hostname)`,
registers it on the active Forgejo node with `forgejo forgejo-cli actions register --secret-stdin`
(idempotent; the secret goes through stdin, not the command line) and reads it from a 0600 file
(`token_url`). `forgejo_runner_scope` limits a runner to an organisation or a repository; deploy
runners should be limited to the infrastructure organisation.

## Isolation choice

Jobs run in containers on the host's Docker Engine, started by the runner, not in Docker-in-Docker:

- Rootless Docker-in-Docker still needs a privileged (or unconfined seccomp/AppArmor) container for
  its user namespaces and nested overlay mounts; a privileged container is a weaker boundary than
  a normal unprivileged job container on the host engine.
- Each job container is unprivileged (`container.privileged: false`), runs with
  `no-new-privileges`, without `NET_RAW` and with a PID limit, gets no Docker socket
  (`docker_host: "-"`) and no host paths except the CA bundle (`valid_volumes`).
- Jobs share a dedicated bridge network (`forgejo_runner_job_network`) with inter-container traffic
  disabled, so concurrent jobs cannot reach each other; they reach Forgejo and the managed hosts
  like any other client of the app zone (restrict further with the firewall and OpenBao policies).
- The runner container itself is hardened (non-root, no capabilities, read-only root) but holds the
  Docker socket, which is root-equivalent on that host. Runner hosts must therefore be dedicated:
  nothing else runs there, and nothing on them is trusted by other hosts beyond what the deploy
  credentials grant.

Jobs that build container images need a builder that works without the socket (for example
buildah or kaniko in a job container), or a separate runner label on a host set aside for it.

## Deploy pipeline (CHG-02)

Production changes run only from a deploy workflow after the pull request is approved and merged into
the protected `main` branch of your deployment repository. The maintained workflows are
`examples/ci/forgejo/deploy.yml` and `drift.yml`; [docs/ci.md](../../docs/ci.md) covers the setup.
They run on a runner with a `deploy` label, so add one to `forgejo_runner_labels` on the runner hosts
in `access_automation_cidrs`, for example `deploy:docker://<ansible tool image by digest>`. The vault
password and a one-hour SSH certificate come from the OpenBao `ci-deploy` AppRole
(`tools/ci-openbao-login`), whose `secret_id` is a Forgejo Actions secret of the repository.

Every run is logged by Forgejo (who merged, commit, workflow log; Actions logs live in object storage)
and the runner's container log goes to journald and the SIEM. SSH access for Ansible uses
short-lived certificates from the OpenBao SSH CA rather than a stored key.

Known limit: Forgejo (v15) has no deployment environments with protection rules, so a workflow on any
branch of the repository could ask for the same secret. Keep write access to the infrastructure
repository to the platform team, review changes under `.forgejo/` with particular care, and bind the
OpenBao AppRole as above. Denying ad-hoc Ansible runs from other machines (the second half of
CHG-02) is not enforced by this role.
