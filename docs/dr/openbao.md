# DR runbook: OpenBao (secrets, internal PKI, SSH CA)

**Depends on:** backup servers, admin access, the unseal (Shamir) or recovery keys and the
procedure for a new root token (break-glass envelope).
**RPO:** `openbao_backup_rpo_hours` (24 h). **RTO:** `openbao_backup_rto_hours` (4 h).
**Backup:** `openbao` declaration: Raft snapshots (`bao operator raft snapshot save`) in
`/opt/openbao/snapshots`, taken before every backup. The newest 5 are kept on the node. The
snapshot is encrypted by OpenBao's barrier and is useless without the unseal keys.

## Scenarios

- **One or two nodes lost** (Raft keeps quorum): replace them and run `playbooks/secrets.yml`.
  Raft re-joins them. Not a disaster.
- **Quorum lost, or data corrupted on every node:** restore the newest good snapshot (below).

## Rebuild

1. Restore the hosts (access runbook). Run `playbooks/secrets.yml --limit openbao` with
   `openbao_init_host` set to the first node. On a fresh, empty cluster the role initialises a
   **new** cluster. Its keys are not the ones you need, but you need a running, unsealed node to
   restore into.
2. Fetch the snapshot on that node:

   ```sh
   ansible-playbook -i ../inventories/<env> playbooks/restore.yml -e restore_host=<openbao-1> \
     -e restore_name=openbao -e restore_confirm=openbao
   ```

3. Force-restore it, with the new cluster's root token:

   ```sh
   docker cp /var/restore/openbao-<ts>/opt/openbao/snapshots/openbao-<time>.snap openbao:/tmp/r.snap
   docker exec -it openbao bao operator raft snapshot restore -force /tmp/r.snap
   ```

   The cluster now holds the old data and is sealed with the **old** keys. Unseal each node with
   the old unseal keys from the envelope (`bao operator unseal`, 3 of 5). With a static seal, the
   static key is used.
4. Run `playbooks/secrets.yml` again to join the other nodes and reconcile policies, auth methods
   and the PKI configuration from the inventory.
5. Secrets written after the snapshot are lost. Rotate anything issued since then: dynamic
   database credentials expire by themselves. Re-issue TLS certificates by running the service
   playbooks (tls_cert renews certificates the cluster no longer knows).

## Verify

- `bao status` on each node shows unsealed and one active node. `/v1/sys/health` returns 200 on
  the active node.
- A KV read of a known path works. `pki/cert/ca` returns the same CA as before the disaster
  (compare the fingerprint with the one in the inventory documentation).
- `ssh-ca` signs a test key.

## Drill

`dr_drill_openbao`: restores the newest snapshot into scratch and checks that it holds
`state.bin` and `meta.json` with matching SHA256SUMS. The yearly game day does steps 1 to 4 in the
lab with the lab's keys.
