# DR runbook: Garage clusters (S3 object storage)

**Depends on:** backup servers, OpenBao (TLS), the inventory (layout, keys, buckets).
**RPO:** objects are replicated on 3 nodes in 3 zones, and are **not** in the backup. The metadata
snapshot RPO is the cluster's `backup.rpo_hours`. **RTO:** the cluster's `backup.rto_hours` for
metadata, plus the time to re-copy objects from an off-cluster copy, if one exists.
**Backup:** `garage-<name>` declaration: metadata snapshots (buckets, keys, object index, layout) of
the node. Buckets that need an off-cluster copy must be copied by the product to another S3
target (rclone, or the product's own export). The product's runbook says which buckets have one.

## Scenarios

- **One node or one zone lost:** Garage keeps serving from the other copies. Replace the node,
  run `playbooks/garage.yml -e garage_only=<name>`, and let the resync finish
  (`GarageResyncQueueGrowing` clears). Not a disaster.
- **Metadata database corrupted on a node:** stop that node's `<name>-garage`, replace its
  metadata directory with the newest snapshot (restored with `playbooks/restore.yml`), start it.
  Garage repairs the difference from the other nodes (`garage repair --all-nodes tables`).
- **Two or more zones lost:** objects whose copies were all in the lost zones are gone. Rebuild
  the cluster, restore metadata where it helps the inventory, and re-copy buckets from the
  products' off-cluster copies.

## Rebuild the whole cluster

1. Restore the hosts. Run `playbooks/garage.yml -e garage_only=<name>`. The layout, buckets and
   keys are applied from the inventory, so access keys stay the same.
2. Re-copy each bucket from its off-cluster copy (`rclone sync <backup>:<bucket> <garage>:<bucket>`).
3. Run `garage repair --all-nodes --yes blocks` and wait for the resync.

## Verify

- `GarageUnhealthy`, `GaragePartitionsNoQuorum` and `GarageStorageNodesDown` are clear. A product
  lists and reads its bucket with its own key.

## Drill

`dr_drill_garage`: restores the metadata snapshots into scratch and checks the newest one is
present and not empty. Object data is proven by the HA drill (one node stopped, reads and writes
still work).
