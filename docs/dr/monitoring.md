# DR runbook: monitoring and alerting

**Depends on:** OpenBao (TLS), Keycloak (Grafana SSO), the mail relay (alert delivery).
**RPO:** metrics history is not backed up. Alert rules, dashboards, scrape targets and routes come
from the repository and the inventory. **RTO:** 2 h.
**Backup:** none on purpose. Two or more Prometheus replicas scrape the same targets and three
Alertmanagers gossip, so losing one node loses nothing. Compliance evidence does not live in
Prometheus: reports are files under `/var/lib/compliance-evidence/` on the hosts that wrote them,
and those are backed up.

## Rebuild

1. Restore the hosts. Run `ansible-playbook -i inventories/<env> playbooks/monitoring.yml`.
2. Run `playbooks/baseline.yml` on all hosts if node_exporter certificates or scrape credentials
   changed.
3. Metrics history before the rebuild is gone. Note the gap in the incident record. The audit
   evidence for the period is in the evidence files and the SIEM.

## Verify

- Every target in Prometheus is `up`. Grafana signs in through Keycloak.
- Fire a test alert (`amtool alert add DRTest severity=warning`) and check that it reaches the
  receivers.

## Drill

No data to restore. The nightly and pull-request Molecule runs rebuild monitoring from scratch.
The yearly game day rebuilds it in the lab and times it.
