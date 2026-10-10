# DR runbook: Valkey instances

**Depends on:** backup servers, OpenBao (TLS).
**RPO:** the instance's `backup.rpo_hours` for the RDB copy. AOF on every member covers node
loss. **RTO:** the instance's `backup.rto_hours`.
**Backup:** `valkey-<name>` declaration: an RDB snapshot (`BGSAVE`, then a copy of `dump.rdb` into
`/opt/valkey/<name>/backup`).

Many Valkey instances hold caches or sessions that applications rebuild. Each product decides
whether to restore at all or to start empty. Record that decision in the product's own runbook.

## Scenarios

- **One member lost:** Sentinel promotes a replica. Replace the node and run
  `playbooks/valkey.yml -e valkey_only=<name>`. Not a disaster.
- **All members lost, or bad data replicated everywhere:** restore the RDB (below).

## Rebuild

1. Restore the hosts. Run `playbooks/valkey.yml -e valkey_only=<name>`, then stop all three
   `<name>-valkey` and `<name>-sentinel` containers.
2. On the first node (the initial primary), restore the declaration with `playbooks/restore.yml`
   and copy `backup/dump.rdb` over `/opt/valkey/<name>/data/dump.rdb`. Remove the AOF directory
   (`appendonlydir`), because Valkey prefers the AOF over the RDB when both exist. Keep the owner
   at the container user (999:1000).
3. Start the first node's `<name>-valkey`. It loads the RDB and writes a new AOF. Start the
   replicas and the Sentinels. The replicas resync from the primary.

## Verify

- `ValkeyNoPrimary` and `ValkeyReplicationBroken` are clear. `DBSIZE` is plausible. The product's
  health check passes.

## Drill

`dr_drill_valkey`: restores the newest RDB into scratch and runs `valkey-check-rdb` on it in a
container without network.
