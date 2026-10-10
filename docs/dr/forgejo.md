# DR runbook: Forgejo and Actions runners

**Depends on:** backup servers, OpenBao, Keycloak (OIDC login), the edge, and the database Forgejo
uses (a PostgreSQL cluster, or the database inside the dump for single-node setups).
**RPO:** `forgejo_backup_rpo_hours`. **RTO:** `forgejo_backup_rto_hours`.
**Backup:** `forgejo` declaration: `forgejo dump` (`/var/backups/forgejo/forgejo-dump.tar.gz`) with
the database as SQL (`forgejo-db.sql`), every repository (`repos/`), `app.ini`, attachments, LFS
and avatars. Its `verify_command` checks the SQL is in the archive.

Runners hold no state. Rebuild them with `playbooks/delivery.yml` and they register again.

## Rebuild

1. Restore the hosts, and the PostgreSQL cluster if Forgejo's database lives there
   ([postgres.md](postgres.md)). Deploy once with `playbooks/delivery.yml`, then stop Forgejo
   (`docker stop forgejo`, and keepalived on the standby).
2. Restore the dump on the active node into a scratch directory with `playbooks/restore.yml`, and
   unpack it.
3. Put the data back, following the upstream "restore from dump" procedure for the release in
   roles/versions:
   - repositories: `repos/*` into `forgejo_repo_dir` (`/srv/forgejo/repositories`), owned by the
     container user;
   - `data/` (attachments, LFS, avatars) into `forgejo_data_dir`;
   - database: load `forgejo-db.sql` into the empty database (`psql -f` for PostgreSQL), as the
     Forgejo database owner;
   - keep the `app.ini` the role writes. Only compare it with the dump's copy.
4. Start Forgejo and run `forgejo admin regenerate hooks` inside the container. Then run
   `playbooks/delivery.yml` again: organisations, branch protection and the automation account are
   reconciled, and the protection evidence export runs.
5. Pushes made after the backup are missing. Developers push their local branches again. CI
   artifacts and caches are rebuilt by running the pipelines.

## Verify

- `ForgejoUnavailable` is clear. Sign in through Keycloak. Clone a repository and run
  `git fsck`. Push to a feature branch, open a pull request, and check that a direct push to
  `main` is rejected (CHG-01).
- A runner picks up a job (`ForgejoNoActionsRunner` is clear).

## Drill

`dr_drill_forgejo`: restores the dump into scratch, checks that the SQL is in it, and runs
`git fsck --connectivity-only` on every repository in the dump, in a container without network.
