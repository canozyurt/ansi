# DR runbook: container registry (Zot)

**Depends on:** backup servers, OpenBao (TLS), Keycloak (UI login), the edge.
**RPO:** `registry_backup_rpo_hours` (24 h). **RTO:** `registry_backup_rto_hours`.
**Backup:** `registry` declaration: the OCI image layout in `/opt/registry/data`. The CVE database,
caches and metadata are excluded, because Zot rebuilds them. The declaration's `verify_command`
(`registry-verify-restore`) checks the layout and the blob digests of the restored copy.

## Rebuild

1. Restore the hosts. Restore the data on the first registry node, then deploy it:

   ```sh
   ansible-playbook -i inventories/<env> playbooks/restore.yml -e restore_host=<registry-1> \
     -e restore_name=registry -e restore_confirm=registry -e restore_target=/ -e restore_in_place_confirm=registry
   ansible-playbook -i inventories/<env> playbooks/registry.yml
   ```

   (Restore in place only while the registry is not running, on a freshly built node.)
2. Images pushed after the backup are missing. Re-run the CI pipelines that build them, or pull
   them again from upstream (mirrored images).
3. Signatures are stored as OCI referrers next to the images, so they come back with the data.
   Check them with `registry-check-signature`.

## Verify

- `RegistryDown` is clear. Pulling a known image by digest works. `registry-check-signature`
  accepts a signed image under the enforce policy.

## Drill

`dr_drill_registry`: restores the newest snapshot into scratch and runs `registry-verify-restore`
on it.
