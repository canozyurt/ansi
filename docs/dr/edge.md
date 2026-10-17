# DR runbook: edge (HAProxy, WAF, VIPs)

**Depends on:** OpenBao (public and internal certificates, or the ACME settings), DNS records at
the provider.
**RPO:** n.a. (configuration from the inventory). **RTO:** 1 h.
**Backup:** none needed. Routes, WAF rules (OWASP CRS in the pinned image) and VIPs are in the
inventory.

## Rebuild

1. Restore the hosts. If the VIPs are provider floating IPs, re-attach them, or update DNS if the
   addresses changed.
2. `ansible-playbook -i ../inventories/<env> playbooks/edge.yml`.

## Verify

- `PublicEndpointDown` clears for every catalog endpoint. The certificate expiry probes pass.
- A request with an attack pattern gets 403 from the WAF.

## Drill

No data to restore. HA (VIP failover) is drilled by `playbooks/ha_drill.yml` and
`molecule/edge`. The yearly game day rebuilds the pair in the lab.
