---
name: Incident
about: Record an incident (docs/incident-response.md)
title: "[SEVx] <short description>"
labels: [incident]
---
<!-- Provisioned to .forgejo/ISSUE_TEMPLATE/incident.md in the incident tracker by roles/forgejo (org_setup).
     Add a sev1..sev4 label, postmortem-needed for SEV1/SEV2, and "security" for security incidents. -->

## Summary

- **Severity:** SEV1 / SEV2 / SEV3 / SEV4
- **Status:** investigating / identified / mitigated / monitoring / resolved
- **Incident commander:** @
- **Communications:** @
- **Security lead (security incidents):** @
- **Started (UTC):**
- **Detected (UTC) and how:** <!-- alert name and link, customer report, ... -->
- **Resolved (UTC):**
- **Customer impact:** <!-- who, what, how long -->
- **Status page announcement:** <!-- link / text -->

## Timeline (UTC)

<!-- One line per event: detection, decisions, changes, status page updates, severity changes. -->

| Time | Who | Event |
| --- | --- | --- |
| | | |

## Investigation notes

<!-- Hypotheses, findings, links to dashboards, logs, alerts. Never paste secrets or personal data. -->

## Changes made during the incident

<!-- Link every pull request, playbook run or manual action (emergency changes get a PR afterwards). -->

## Closing checklist

- [ ] Customer impact ended and the alert has cleared
- [ ] Final status page update posted ("operational")
- [ ] Summary and timeline complete
- [ ] Emergency changes have pull requests
- [ ] Postmortem issue opened (required for SEV1/SEV2) and linked:
- [ ] Notification duties checked (security incidents: customers, authorities, contracts)
