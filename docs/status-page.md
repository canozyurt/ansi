# Status page (Gatus)

`roles/status_page` deploys [Gatus](https://github.com/TwiN/gatus) (Apache-2.0, image
`docker.io/twinproduction/gatus`, pinned in `roles/versions`) on the `status_page` group. It is the
public status page for customers and the synthetic HTTPS check for every public service
(`playbooks/governance.yml`).

## What it checks

Endpoints are generated from the service catalog the edge proxy uses, so a service published
through the edge is checked without extra configuration:

- every entry of `edge_services` with `public: true` is checked at
  `https://<first hostname><health_path>` (the role reads the edge role's defaults plus
  `edge_extra_services` when `edge_services` is not set in the inventory);
- `status_page_extra_endpoints` adds anything else (Gatus endpoint format);
- `status_page_exclude_services` leaves services out (the status page itself by default).

Every endpoint gets three conditions: HTTP status (`status_page_expected_status`, 200), certificate
expiry (more than `status_page_cert_min_hours`, 14 days, SEC-05) and response time (under
`status_page_max_response_ms`). Checks verify certificates against the host trust store, which
holds the internal CA once the PKI exists.

## Availability (HA-01)

Gatus keeps no shared state: its endpoints come from the configuration and its results are per
node. Two nodes (`status_page_min_nodes: 2`) each run their own Gatus and check everything
independently; the edge proxy balances between them and takes a failed node out. That is a complete
active/active pair, not a degraded one. Each node keeps its history in a local SQLite file, so two
nodes can show slightly different histories; the current state is the same. History is monitoring
data, not evidence: it is not backed up (Prometheus keeps the metrics).

## Public by design: the documented exception to IAM-01

The page has no login. It is read-only (Gatus has no write actions in the UI or API), shows only
public hostnames, and hides error strings (`status_page_hide_errors`) that could reveal internal
addresses. This is the one web UI that does not authenticate through the IdP; IAM-01 lists it as a
documented exception. Everything else about it stays internal:

- the edge serves it as a `public: true` service and should list `/metrics` as an admin path;
- the host firewall opens `status_page_port` (8444) to the edge (app zone) and Prometheus (mgmt zone)
  only (`firewall_rules_status_page`).

Suggested edge catalog entry (`edge_extra_services` or the edge role defaults):

```yaml
- name: status
  hostnames: [status.example.com]
  backend_group: status_page
  backend_port: 8444
  backend_tls: true
  health_path: /health
  public: true
  admin_paths: [/metrics]
  # The Vue front end uses inline styles.
  csp: >-
    default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none';
    base-uri 'self'; frame-ancestors 'none'
```

## TLS, hardening, logs

- HTTPS only on `status_page_port` with a certificate from `tls_cert` (TLS 1.2+, Go defaults).
- Runs as uid 65534 with no capabilities, no privilege escalation, a read-only root, memory and PID
  limits, host networking. Logs go to stdout (journald).

## Alerting

- Main path: Prometheus scrapes `https://<node>:8444/metrics` (`status_page` job in the scrape
  catalog). The rules in `inventories/example/group_vars/monitoring/governance.yml`
  (`prometheus_rules_status_page`) fire `PublicEndpointDown` (critical) only when every status page
  node sees the endpoint failing, plus slow responses, certificates under 14 days and a status page
  node down. Alertmanager deduplicates and routes them like every other alert.
- Optional direct notifications from Gatus: email (`status_page_alert_email_*`) or a webhook
  (`status_page_alert_webhook_url`). Each node sends its own, so expect duplicates; use them as a
  path that works when Prometheus is down.

## Customer communication

During an incident the incident commander's comms role posts a Gatus announcement
(`status_page_announcements`, deployed by re-running `playbooks/governance.yml`) and keeps it
updated until resolution; see [incident-response.md](incident-response.md).
