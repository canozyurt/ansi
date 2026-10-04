# Incident response

How an alert becomes an incident, who does what, where it is recorded and how long the records are
kept. No ticketing product is added: incidents are Forgejo issues, customer updates go to the status
page ([status-page.md](status-page.md)), and evidence goes to the GRC tool ([grc.md](grc.md)).

Maps to SOC 2 CC7.3 (evaluate security events), CC7.4 (respond), CC7.5 (recover) and ISO/IEC
27001:2022 Annex A 5.24 (planning and preparation), 5.25 (assessment and decision), 5.26 (response)
and 5.27 (learning from incidents). Checklist item **INC-01**.

## Severity levels

| Severity | Definition | Examples | Response | Customer update | Postmortem |
| --- | --- | --- | --- | --- | --- |
| SEV1 | Customer-facing outage or confirmed security breach / data exposure | `PublicEndpointDown` on a product service; confirmed unauthorized access; data loss | Page on-call now (Alertmanager `critical`), incident commander within 15 min, work until mitigated | Status page within 30 min, then at least every 60 min | Required within 5 working days |
| SEV2 | Major degradation, loss of redundancy that threatens availability, or a suspected security incident | One edge or Keycloak node down with no failover margin; WAF failing closed; suspicious admin activity; backup RPO missed | Respond within 1 hour during and outside business hours | Status page when customers notice (degraded) | Required |
| SEV3 | Minor impact or a control failing with no customer impact | Certificate under 14 days; single restore test failed; GRC tool down | Next business day | None | Optional (record cause in the issue) |
| SEV4 | Near miss or informational | Alert that self-resolved; false positive | Triage weekly | None | No |

Alert severities map to incident severities as a starting point: `critical` -> SEV1 or SEV2,
`warning` -> SEV3, informational -> SEV4. The incident commander sets the final severity and may
raise or lower it at any time; every change is noted in the timeline.

A security incident (breach, suspected compromise, data exposure) is at least SEV2 and also follows
the organisation's legal/notification duties (customers, authorities, contracts); the incident
commander involves the security-admin role holder and management immediately.

## Roles

| Role | Who | Responsibilities |
| --- | --- | --- |
| Incident commander (IC) | The on-call engineer who acknowledges the page, until they hand over explicitly | Opens the incident issue, sets severity, coordinates responders, decides on mitigations, keeps the timeline, declares resolution, assigns the postmortem |
| Communications (comms) | Named by the IC (the IC for SEV3/4) | Status page announcements, customer and internal updates at the cadence above, records every update in the issue |
| Responders | Anyone the IC pulls in | Investigate and fix; post findings and actions in the issue |
| Security lead | security-admin role holder | Joins every security incident: evidence preservation, scope, legal/notification decisions |

One person can hold several roles in a small team; the IC role is always explicit (assigned in
the issue).

## From alert to incident

```
Prometheus rule fires --> Alertmanager --> email / webhook (on-call)
                                   |
             on-call acknowledges, triages (5-15 min)
                                   |
      real impact or risk? --no--> silence with a comment, note it on the weekly SEV4 list
                                   |
                                  yes
                                   |
   open a Forgejo issue from the "incident" template (label incident + severity/*)
                                   |
   IC + comms assigned; status page announcement (SEV1/2, customer-visible)
                                   |
      mitigate -> monitor -> resolve -> status page "operational" -> issue closed
                                   |
           postmortem (SEV1/2) from the "postmortem" template, linked to the issue
```

1. **Detect.** Alerts come from Prometheus rules through Alertmanager (routes: default, critical,
   heartbeat). The status page adds customer-view checks (`PublicEndpointDown` fires only when all
   status page nodes agree). Security events come from the SIEM the same way.
2. **Triage.** The on-call acknowledges, checks the dashboards and decides within 15 minutes whether
   this is an incident. Silences in Alertmanager always carry a comment with the issue link or the
   reason.
3. **Declare.** Open an issue in the organisation's incident repository from
   `.forgejo/issue_template/incident.md` (copied from [templates/incident.md](templates/incident.md)).
   Labels: `incident`, `severity/sev1`..`severity/sev4`, and `security` when relevant. The issue is
   the single record: timeline, decisions, links to alerts, dashboards and changes.
4. **Communicate.** Comms posts a Gatus announcement (`status_page_announcements`, types `outage`,
   `warning`, `information`, then `operational`) and updates it at the cadence above. Every update
   is copied into the issue timeline with its time.
5. **Mitigate and recover.** Changes made during an incident still go through the normal pipeline
   (CHG-01/CHG-02); an emergency change is allowed with a pull request opened afterwards and linked
   in the issue. Restores use `playbooks/restore.yml` and are recorded.
6. **Resolve.** The IC declares resolution when the alert has cleared and customer impact has
   ended, posts the final status page update and closes the issue with the summary filled in.
7. **Learn.** SEV1 and SEV2 get a blameless postmortem from
   [templates/postmortem.md](templates/postmortem.md) within 5 working days, reviewed by someone not
   involved. Each action item becomes its own issue linked to the incident and is tracked to closure.

## Where incidents are tracked

- Forgejo, in one repository per organisation (for example `org/incidents`), private, with the
  `incident` and `severity/*` labels and the two templates copied to `.forgejo/issue_template/`.
  Access: operators, security-admin and platform-admin write; auditors read (through the IdP
  groups, IAM-03).
- Postmortems are a second issue (label `postmortem`) or a Markdown file in the same repository,
  linked both ways.
- The monthly evidence export (`tools/evidence_export.py`) and the list of closed incident issues
  are attached to the GRC tool as evidence for INC-01 / CC7.3-CC7.5.

## Evidence retention

| Record | Where | Kept |
| --- | --- | --- |
| Incident issues, timelines, postmortems | Forgejo (backed up by the Forgejo role) | At least 12 months after closure; never deleted inside that period |
| Alert history | Alertmanager notifications (email/webhook archive), Prometheus 30 days | Notifications 12 months in the mail/chat archive |
| Logs referenced by the incident | Central logging | 12 months (LOG-02) |
| Status page announcements | Inventory (`status_page_announcements`, archived entries) in git | Git history |
| Evidence package | GRC tool (CISO Assistant evidence, backed up) | 12 months minimum |

Deleting an incident record inside its retention period is not allowed for any role (the same rule
as for logs and backups in the role model).

## Test it

Run a tabletop exercise at least once a year (with the DR drill, HA-02): fire a test alert, open an
incident issue, post a status page announcement, close it, write a short postmortem. Keep the issue
as evidence that the process works.
