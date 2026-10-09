#!/usr/bin/env python3
"""Forward a Wazuh alert to Prometheus Alertmanager (POST /api/v2/alerts).

Arguments (from wazuh-integratord): alert file, api key (unused), hook url, [options file].
Basic-auth credentials and the CA file are read from /etc/compliant-infra/config/alertmanager.json
(0640 root:wazuh), never from ossec.conf or the command line.
"""
import base64
import datetime
import json
import ssl
import sys
import urllib.request

SETTINGS_FILE = "/etc/compliant-infra/config/alertmanager.json"
LOG_FILE = "/var/ossec/logs/integrations.log"


def log(message):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} custom-alertmanager: {message}\n")


def severity(level):
    if level >= 12:
        return "critical"
    if level >= 10:
        return "warning"
    return "info"


def main(argv):
    if len(argv) < 4:
        log("usage: custom-alertmanager <alert file> <api key> <hook url> [options]")
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        alert = json.load(handle)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as handle:
            settings = json.load(handle)
    except FileNotFoundError:
        settings = {}

    rule = alert.get("rule", {})
    agent = alert.get("agent", {})
    level = int(rule.get("level", 0))
    labels = {
        "alertname": "WazuhAlert",
        "severity": severity(level),
        "source": "wazuh",
        "rule_id": str(rule.get("id", "")),
        "rule_level": str(level),
        "agent_name": str(agent.get("name", "")),
        "agent_id": str(agent.get("id", "")),
    }
    for group in rule.get("groups", [])[:1]:
        labels["rule_group"] = str(group)
    annotations = {
        "summary": str(rule.get("description", ""))[:500],
        "description": str(alert.get("full_log", ""))[:2000],
        "alert_id": str(alert.get("id", "")),
        "timestamp": str(alert.get("timestamp", "")),
    }
    body = json.dumps([{"labels": labels, "annotations": annotations}]).encode()
    url = argv[3].rstrip("/") + "/api/v2/alerts"
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if settings.get("username"):
        token = base64.b64encode(f"{settings['username']}:{settings.get('password', '')}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
    context = None
    if url.startswith("https://"):
        context = ssl.create_default_context(cafile=settings.get("ca_file") or None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as response:
            log(f"sent alert {annotations['alert_id']} rule {labels['rule_id']} -> HTTP {response.status}")
    except Exception as error:  # noqa: BLE001 - report every delivery failure in the integration log
        log(f"failed to send alert {annotations['alert_id']}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
