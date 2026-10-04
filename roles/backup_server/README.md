# backup_server

restic [rest-server](https://github.com/restic/rest-server) (BSD-2) on the `backup` group: the target
for the encrypted backups that `roles/backup` takes on every host.

- Runs as a pinned container (`versions_images.rest_server`) with host networking, as the unprivileged
  `rest-server` user (uid 10800), read-only root, no capabilities, `no-new-privileges`.
- `--append-only`: clients can create data, index and snapshot files but cannot delete or overwrite
  them (only their own lock files). A stolen client credential cannot destroy backups (BCK-02).
- `--private-repos`: each login reaches only `/<login>/`. One login per inventory host, bcrypt hash in
  `/opt/rest-server/auth/htpasswd` (0600, owned by the container user). Logins of hosts that leave the
  inventory are removed; their repositories stay until an operator deletes them.
- TLS from `roles/tls_cert` (internal PKI, organisation files, or self-signed in labs). Go's server
  default refuses anything below TLS 1.2.
- Prometheus metrics at `https://<server>:8000/metrics` for the login `metrics`.
- Access log (combined format) on stdout, so in journald through the container log driver.

Run at least two servers in different failure domains; every client writes an independent copy to each.

## Retention runs on the server

Clients cannot delete, so retention cannot run on them. A daily systemd timer
(`compliant-backup-prune.timer`) runs `restic forget --prune` directly on each repository directory with
`backup_server_keep_*` (default: 14 daily, 8 weekly, 12 monthly), grouped per host and declaration.

**This needs each repository's password.** The server keeps them in
`/etc/compliant-infra/backup-server/repo-passwords.json` (0600 root), rendered from the same Vault /
OpenBao secret the clients use (`backup_password_seed`, or a host's explicit `backup_repo_password`).
restic runs as the `rest-server` user with the password in its environment, never on a command line.
Consequence: root on a backup server can decrypt the backups stored there. Protect the backup servers
like the secrets manager (management zone access only, no other workloads).

Results: `backup_server_prune_last_success_timestamp_seconds{repo}`, `backup_server_prune_last_status`,
`backup_server_snapshots{repo}` in `/var/lib/node_exporter/textfile_collector/backup_server.prom`.

## Optional off-site copy to S3

Set `backup_s3_endpoint`, `backup_s3_bucket` and the Vault-backed `backup_s3_access_key` /
`backup_s3_secret_key` (optionally `backup_s3_region`, `backup_s3_prefix`, default the server's name).
A nightly timer (`compliant-backup-s3.timer`) runs `restic copy` from every local repository to
`s3:<endpoint>/<bucket>/<prefix>/<client>` (initialised with the same chunker parameters, same
password), then applies the retention policy there too (`backup_s3_forget`, default true). Any
S3-compatible store works.

Recommended bucket settings: **versioning plus Object Lock** (compliance or governance mode) with a
default retention at least as long as your longest restore need, and a lifecycle rule that expires
non-current versions after that period. With versioning, restic's deletes only add delete markers, so
prune keeps working, while the locked object versions cannot be removed by anyone until their retention
expires: a compromised backup server cannot destroy the off-site copy. Give the access key only
`s3:PutObject`, `s3:GetObject`, `s3:ListBucket` and `s3:DeleteObject` (no `s3:DeleteObjectVersion`,
no bucket policy or lock changes).

Results: `backup_server_s3_copy_last_success_timestamp_seconds{repo}` and `backup_server_s3_copy_last_status`.
