# DR runbook: RabbitMQ clusters

**Depends on:** backup servers, OpenBao (TLS), Keycloak (management UI login).
**RPO:** the cluster's `backup.rpo_hours` for definitions. Messages are not backed up: they are
transient by design, and producers must be able to send again. Quorum queues replicate messages
across members, which covers node loss. **RTO:** the cluster's `backup.rto_hours`.
**Backup:** `rabbitmq-<name>` declaration: the definitions export (`definitions.json`: vhosts,
users with password hashes, permissions, policies, queues, exchanges, bindings).

## Rebuild

1. Restore the hosts. Run `playbooks/rabbitmq.yml -e rabbitmq_only=<name>`. The cluster forms
   again, and the role re-creates the vhosts, users and permissions declared in the inventory.
2. Restore the declaration into scratch with `playbooks/restore.yml` and import the definitions,
   to bring back objects that applications created at runtime (queues, exchanges, bindings,
   policies):

   ```sh
   docker cp <scratch>/opt/rabbitmq/<name>/backup/definitions.json <name>-rabbitmq:/tmp/defs.json
   docker exec <name>-rabbitmq rabbitmqctl import_definitions /tmp/defs.json
   ```

3. Run the playbook once more so that passwords rotated after the backup win over the imported
   hashes.
4. Tell the product teams which messages may have been lost (from the incident timeline), so they
   can replay from their own sources.

## Verify

- `RabbitMQNodeDown` and `RabbitMQClusterPartitioned` are clear. `list_queues` shows the expected
  quorum queues. A product publishes and consumes a test message.

## Drill

`dr_drill_rabbitmq`: restores the definitions into scratch, imports them into a throwaway
single-node broker without network, and counts the vhosts, users and queues that came back.
