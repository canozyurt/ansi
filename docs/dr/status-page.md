# DR runbook: status page

**Depends on:** the edge, DNS.
**RPO:** n.a. (configuration from the inventory; uptime history is kept in each node's local
storage and may be lost). **RTO:** 1 h.
**Backup:** none needed.

## Rebuild

1. Restore the hosts. Run `ansible-playbook -i inventories/<env> playbooks/governance.yml` (the
   status page role).
2. Host the status page outside the failed site or provider when possible, so customers can still
   read it during the disaster. Post the incident there.

## Verify

- The public URL shows every endpoint. `StatusPageNodeDown` is clear.

## Drill

No data to restore.
