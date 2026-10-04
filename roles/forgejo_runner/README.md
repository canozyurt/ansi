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

## Deploy pipeline example (CHG-02)

Production changes run only from this workflow after the pull request is approved and merged into
the protected `main` branch. Store it as `.forgejo/workflows/deploy.yml` in the infrastructure
repository. The vault password comes from OpenBao with an AppRole whose `secret_id` is a Forgejo
Actions secret of the repository; bind the AppRole to the runner hosts' addresses
(`secret_id_bound_cidrs`, `token_bound_cidrs`) and give it read access to that one KV path only.

```yaml
name: deploy
on:
  push:
    branches: [main]
  schedule:
    - cron: "0 5 * * 1"          # weekly check-mode drift run (CHG-03)
jobs:
  deploy:
    runs-on: docker
    # Pin the tool image by digest; it needs ansible-core, the pinned collections, curl and jq.
    container:
      image: registry.example.internal/platform/ansible-runner:2026.10@sha256:<digest>
    steps:
      - uses: actions/checkout@v4
      - name: Read the vault password from OpenBao
        env:
          BAO_ADDR: https://bao.example.internal
          ROLE_ID: ${{ vars.BAO_DEPLOY_ROLE_ID }}
          SECRET_ID: ${{ secrets.BAO_DEPLOY_SECRET_ID }}
        run: |
          set -euo pipefail
          token=$(jq -n --arg r "$ROLE_ID" --arg s "$SECRET_ID" '{role_id:$r, secret_id:$s}' |
            curl -sSf --data @- "$BAO_ADDR/v1/auth/approle/login" | jq -r .auth.client_token)
          umask 077
          curl -sSf -H "X-Vault-Token: $token" "$BAO_ADDR/v1/kv/data/ansible/vault" |
            jq -r .data.data.password > "$RUNNER_TEMP/vault-pass"
          curl -sSf -X POST -H "X-Vault-Token: $token" "$BAO_ADDR/v1/auth/token/revoke-self"
      - name: Apply (check mode on the weekly schedule)
        run: |
          mode=""; [ "${{ forgejo.event_name }}" = schedule ] && mode="--check"
          ansible-playbook -i inventories/production playbooks/site.yml $mode --diff \
            --vault-password-file "$RUNNER_TEMP/vault-pass" | tee "$RUNNER_TEMP/ansible.log"
      - name: Remove the vault password
        if: always()
        run: rm -f "$RUNNER_TEMP/vault-pass"
```

Every run is logged by Forgejo (who merged, commit, workflow log; Actions logs live in object storage)
and the runner's container log goes to journald and the SIEM. SSH access for Ansible should use
short-lived certificates from the OpenBao SSH CA rather than a stored key.

Known limit: Forgejo (v15) has no deployment environments with protection rules, so a workflow on any
branch of the repository could ask for the same secret. Keep write access to the infrastructure
repository to the platform team, review changes under `.forgejo/` with particular care, and bind the
OpenBao AppRole as above. Denying ad-hoc Ansible runs from other machines (the second half of
CHG-02) is not enforced by this role.
