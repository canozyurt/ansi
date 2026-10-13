# Compliance audits, change control and the evidence store

What runs on a schedule to prove the controls work, where its evidence goes, and how production
changes are restricted to CI. Deployed by `playbooks/audits.yml` (part of `site.yml`); alert rules in
`inventories/example/group_vars/monitoring/audits.yml`. **None of this has run yet (not tested).**

## Scheduled jobs

| Item | Job | Where | When | Evidence (`/var/lib/compliance-evidence/…`) | Alerts |
| --- | --- | --- | --- | --- | --- |
| HRD-01 | `cis-audit` (OpenSCAP, CIS Level 1; `roles/cis_audit`) | every host | weekly, Sun | `cis/<time>/` results.xml, arf.xml, report.html, summary.json | CISScoreDropped, CISRuleRegressed, CISScoreBelowTarget, CISAuditFailed, CISAuditNotRun |
| HRD-03 | `docker-bench` (docker-bench-security; `roles/docker_bench`) | container_hosts | weekly, Sun | `docker-bench/<time>/` log, JSON, summary.json | DockerBenchWarnings, DockerBenchWarningsIncreased, DockerBenchNotRun |
| SEC-03 | `tls-scan` (testssl.sh container; `roles/tls_scan`) | monitoring hosts | weekly, Sat | `tls/<time>/<target>.json` | TLSWeakProtocolOffered, TLSWeakCipherOffered, TLSObsoleteCipherOffered, TLSCertificateProblem, TLSScanFailed, TLSScanNotRun |
| IAM-05 | `access-review` (`roles/access_review`) | every host; Keycloak and VPN part on the first keycloak node | quarterly, 1 Jan/Apr/Jul/Oct | `access-review/<YYYY-Qn>/<time>/` CSV + JSON | AccessReviewOverdue (report older than 100 days) |
| EVD-02 | `compliant-evidence-upload` (`roles/evidence_store`) | every host | weekly, Sun | uploads the whole tree | EvidenceUploadStale |
| EVD-02 | `compliant-evidence-check` (`roles/backup_server`) | backup servers | weekly, Mon | `evidence-retention/<time>.json` | EvidenceRetentionViolated, EvidenceRetentionCheckNotRun |
| CHG-03 | drift workflow in your deployment repository (from `examples/ci/*/drift.yml`, [ci.md](ci.md)) | deploy runner | daily | run record + log (workflow artifact), metric on each host | DriftDetected, DriftCheckFailed, DriftCheckNotRun |

Every job logs a one-line summary to journald under its own identifier (the Wazuh agent ships it to
the SIEM) and writes metrics through node_exporter's textfile collector. Local copies are pruned
after 120 days; the evidence store keeps them 12 months. Each role also registers its directory with
`backup_register`, so the regular append-only backup holds them too.

CIS and Docker benchmark exceptions are documented in [cis-exceptions.md](cis-exceptions.md).

### TLS scan targets

Built from the Prometheus scrape catalog (every `https` entry, one target per host of its group, plus
every `prometheus_scrape_extra_*`), every edge service hostname on 443 and `tls_scan_extra_targets`
(e.g. `{ name: smtp, host: relay1, port: 25, starttls: smtp }` or the PostgreSQL port with
`starttls: postgres`). Exclude with `tls_scan_exclude_jobs` / `tls_scan_exclude_services`. The scan
trusts the internal CA through the host's CA bundle.

### Access review

The Keycloak part uses the `access-review` client (client credentials, read-only realm-management
roles: view/query users, groups, clients, realm and events), created by `roles/keycloak` when
`keycloak_access_review_client_secret` is set (vault). The report lists users with groups, realm
roles, realm-management roles, admin flag, last login, disabled, stale (no login for
`access_review_stale_days`) and service accounts; groups with members; every role assignment; VPN
peers with owner and expiry (`wireguard_peers`). Every host adds its local accounts (shell, password
and lock state, groups, sudo rules, SSH principals and keys). `playbooks/access_review.yml` runs it
now and collects one bundle per quarter on the controller (`access-review/`, git-ignored): attach it
to the review record in the GRC tool, with the reviewer's decisions.

Keycloak keeps login events `keycloak_events_expiration` (90 days by default); keep it at least
`access_review_stale_days`, or users who logged in earlier show as never logged in.

## Evidence store (EVD-02)

A dedicated restic repository, `/<backup_server_evidence_user>/` (default `evidence`) on every
backup server, behind the same append-only rest-server as the backups:

- every host uploads `/var/lib/compliance-evidence` weekly with one shared append-only login; a
  host can add snapshots but never delete or rewrite them (the rest-server refuses deletes);
- only the backup servers hold the credentials that can delete; their prune keeps every evidence
  snapshot for `backup_server_evidence_keep_within` (13 months);
- the weekly retention check lists the snapshots and fails (EvidenceRetentionViolated) when the
  oldest kept evidence is younger than the store (up to 365 days, minus 8 days of slack) or a
  calendar month in that period has no snapshot; it also reports uploaders more than 8 days late.
  The verdict itself is written to `evidence-retention/` and so lands in the next upload.

Restic snapshots are content-addressed and encrypted; together with append-only access this makes
the store tamper-evident (a changed file is a new snapshot, the old one stays). Not yet covered:
CI artifacts (scan reports, drift records) stay in the CI system for 90 days; attach them to the
GRC tool, or extend the CI pipelines to upload them to the evidence repository.

## Production changes only from CI (CHG-02)

With `ci_only_deploys: true` (example inventory) and a trusted SSH user CA:

1. The deploy account (`ansible_user`) has no `authorized_keys`; its principals file accepts only
   `ci-deploy` (optionally only from `access_automation_cidrs`).
2. Certificates with `ci-deploy` are signed only through the OpenBao AppRole `ci-deploy`
   (`roles/openbao`), which can sign that one role (1 hour) and read the Ansible vault password,
   nothing else. Its `secret_id` is bound to the runner addresses and lives in the CI's secrets.
   `tools/ci-openbao-login` does the login, signing and token revocation in the pipelines.
3. `sudo` for the deploy account is allowed only in an SSH session that logged in with such a
   certificate (pam_exec check of the certificate sshd exposes with `ExposeAuthInfo`). Refusals are
   logged as `ansible-run-denied` (SIEM rule 100131, level 12).
4. People keep their accounts but lose sudo. A `break-glass` account with sudo accepts the
   `break-glass` principal, signed only under the admin policy; every login is logged at alert
   priority (SIEM rule 100120) and raises BreakGlassLogin. Run the drift check after any break-glass use.

Every run goes through `tools/ansible-run`: the `run_record` callback (`ansible.cfg`) writes a JSON
record (who: CI actor or user, commit, branch, playbook, limit, tags, check mode; per-host result)
to journald on the controller, and `playbooks/run_record.yml` logs it on every host the run touched
(identifier `ansible-run`, SIEM rule 100130). For a full check-mode run of `site.yml` it also writes
the per-host drift metric.

Setup order: deploy OpenBao with the `openbao_ssh_extra_roles` of the example inventory; create the
KV secret `secret/ci/ansible-vault` (field `password`); read the AppRole's role_id and issue a
secret_id into the CI's variables (`BAO_ADDR`, `BAO_CACERT`, `BAO_DEPLOY_ROLE_ID`) and secrets
(`BAO_DEPLOY_SECRET_ID`); trust the SSH CA (`ssh_hardening_trusted_user_ca_keys`); then deploy with
`ci_only_deploys: true`. Until the CA is trusted, the role only reports that CI-only is not enforced.

## Drift (CHG-03)

The drift workflow ([ci.md](ci.md)) runs `tools/ansible-run playbooks/site.yml --check --diff` daily as the CI. Any
task that would change counts as drift: the job fails, `tools/drift_report.py` lists the hosts in the
job summary, and every host gets `ansible_drift_changed_tasks` (DriftDetected). Tasks that always
report a change in check mode would show as permanent drift; fix them (`changed_when`) when they
appear. A host the run did not reach keeps its old timestamp and raises DriftCheckNotRun.

## Known gaps

- Nothing here has run: no Molecule scenario run, no workflow run (tests are on hold).
- The scap-security-guide release (Ubuntu 24.04) and docker-bench-security are not pinned in
  `roles/versions` because their SHA-256 sums were not recorded; those scans stop with that message.
- The Forgejo drift example needs the Ansible tool image (`ANSIBLE_RUNNER_IMAGE`, by digest; [ci.md](ci.md)).
