# DR runbook: SIEM (Wazuh indexer, manager, dashboard)

**Depends on:** backup servers, the S3 snapshot repository of the indexer, OpenBao, Keycloak
(dashboard SSO).
**RPO:** 24 h for configuration and agent keys. Events: the indexer's S3 snapshot schedule
(`wazuh_indexer_snapshot_cron`, daily). Events from the last day also still sit in agent buffers.
**RTO:** 8 h.
**Backup:**
- `wazuh-manager`: `ossec/etc` (configuration, `client.keys` with every agent's key),
  API configuration, the agent database and certificates.
- `wazuh-indexer`: configuration, security settings, hashes and, on the CA host, the SIEM CA.
- Index data: OpenSearch snapshots to S3, kept as long as logs must be kept.

## Rebuild

1. Restore the hosts. Restore `wazuh-indexer` (CA host first) and `wazuh-manager` on the SIEM
   nodes in place, before deploying:

   ```sh
   ansible-playbook -i inventories/<env> playbooks/restore.yml -e restore_host=<siem-1> \
     -e restore_name=wazuh-indexer -e restore_confirm=wazuh-indexer -e restore_target=/ -e restore_in_place_confirm=wazuh-indexer
   ansible-playbook -i inventories/<env> playbooks/restore.yml -e restore_host=<siem-1> \
     -e restore_name=wazuh-manager -e restore_confirm=wazuh-manager -e restore_target=/ -e restore_in_place_confirm=wazuh-manager
   ansible-playbook -i inventories/<env> playbooks/siem.yml
   ```

   With the same CA and `client.keys`, agents reconnect without being enrolled again.
2. Restore the index data from the S3 snapshot repository:
   `POST _snapshot/<repo>/<snapshot>/_restore` for the `wazuh-alerts-*` indices (dashboard Dev Tools
   or `curl` with the admin certificate).
3. If `client.keys` could not be restored, re-enrol the agents by running `playbooks/siem.yml` on
   all hosts (the wazuh_agent role).

## Verify

- `WazuhManagerDown` and `WazuhAgentsDisconnected` are clear. Recent alerts show in the
  dashboard, and the restored indices are searchable for the retention period.

## Drill

`dr_drill_siem`: restores both declarations into scratch and checks that the manager configuration
and `client.keys` are present. The yearly game day restores one index snapshot into the lab
indexer.
