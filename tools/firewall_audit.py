#!/usr/bin/env python3
"""Static audit of the firewall rules every host gets from an inventory (NET-02, NET-03).

Renders, for each host, the rules roles/firewall would load: the built-in SSH rule (sources
firewall_mgmt_cidrs), firewall_rules and every firewall_rules_<role> variable, plus published container
ports. Nothing connects to a host; only the inventory and its group_vars are read through Ansible's own
variable manager, so the values are exactly what a playbook run would template.

Checks (each failure is a finding; the exit code is 1 when there is any):
  * every host has a zone (network_zone) and, when its address is known, the address lies in that zone;
  * NET-03: a rule source in another zone than the host must match a flow in compliance/zone-flows.yml
    (from-zone, to-zone, port, protocol); sources outside every zone, or spanning zones, are refused;
  * NET-02: admin and database rules (class admin/database; the SSH rule is admin) allow only
    firewall_mgmt_cidrs plus sources with a declared flow, never "any" and never the whole mgmt zone;
  * every rule declares a known class, so new admin or database ports cannot slip past the audit.

Usage:
    tools/firewall_audit.py                       # audit inventories/example, print findings
    tools/firewall_audit.py --markdown -o docs/network.md   # write the port matrix
    tools/firewall_audit.py --check-doc docs/network.md     # also fail when the committed matrix is stale
    tools/firewall_audit.py --json                # rendered rule set as JSON
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = ROOT / "inventories" / "example" / "hosts.yml"
DEFAULT_FLOWS = ROOT / "compliance" / "zone-flows.yml"
SENSITIVE = {"admin", "database"}
RULE_VAR = re.compile(r"^firewall_rules_.+")


# --------------------------------------------------------------------------- rendering


class Renderer:
    """Templates inventory variables per host with Ansible's variable manager."""

    def __init__(self, inventory: Path):
        os.environ.setdefault("ANSIBLE_CONFIG", str(ROOT / "ansible.cfg"))
        from ansible.inventory.manager import InventoryManager
        from ansible.parsing.dataloader import DataLoader
        from ansible.vars.manager import VariableManager

        self.loader = DataLoader()
        self.inventory = InventoryManager(loader=self.loader, sources=[str(inventory)])
        self.variables = VariableManager(loader=self.loader, inventory=self.inventory)

    def hosts(self) -> list[str]:
        return sorted(h.name for h in self.inventory.get_hosts("all"))

    def groups_of(self, host: str) -> set[str]:
        return {g.name for g in self.inventory.get_host(host).get_groups()}

    def host_vars(self, host: str):
        from ansible.template import Templar

        hv = self.variables.get_vars(host=self.inventory.get_host(host))
        return hv, Templar(loader=self.loader, variables=hv)

    @staticmethod
    def render(templar, value):
        """Render a value from the inventory, or a Jinja string from the flows file."""
        from ansible.template import trust_as_template

        if isinstance(value, str) and "{{" in value:
            value = trust_as_template(value)
        return templar.template(value)


def plain(value):
    """Convert Ansible's tagged values into plain Python types."""
    return json.loads(json.dumps(value, default=str))


def render_host(renderer: Renderer, host: str, flows_doc: dict) -> dict:
    hv, templar = renderer.host_vars(host)

    def var(name, default=None):
        return plain(renderer.render(templar, hv[name])) if name in hv else default

    mgmt_cidrs = var("firewall_mgmt_cidrs", []) or []
    rules = [{"name": "ssh", "port": var("firewall_ssh_port", 22), "proto": "tcp", "class": "admin",
              "sources": mgmt_cidrs, "origin": "roles/firewall (built-in)"}]
    for rule in var("firewall_rules", []) or []:
        rules.append({**rule, "origin": "firewall_rules"})
    for name in sorted(k for k in hv if RULE_VAR.match(k)):
        for rule in var(name, []) or []:
            rules.append({**rule, "origin": name})
    for pub in var("firewall_published_container_ports", []) or []:
        rules.append({"name": f"published-{pub.get('port')}", "port": pub.get("port"),
                      "proto": pub.get("proto", "tcp"), "class": pub.get("class", "public"),
                      "sources": ["any"], "origin": "firewall_published_container_ports"})
    for rule in rules:
        rule.setdefault("proto", "tcp")
        rule["port"] = None if rule.get("port") is None else str(rule["port"])
        sources = rule.get("sources") or []
        rule["sources"] = [sources] if isinstance(sources, str) else list(sources)

    zones = {name: plain(renderer.render(templar, spec.get("cidrs", [])))
             for name, spec in (flows_doc.get("zones") or {}).items()}
    addresses = var("network_host_addresses", {}) or {}
    address = addresses.get(host) or hv.get("ansible_host")
    return {
        "host": host,
        "zone": var("network_zone"),
        "groups": sorted(renderer.groups_of(host)),
        "address": str(address) if address else None,
        "mgmt_cidrs": mgmt_cidrs,
        "zones": zones,
        "rules": rules,
    }


# --------------------------------------------------------------------------- zone logic


def net(value: str):
    return ipaddress.ip_network(str(value), strict=False)


def contains(outer, inner) -> bool:
    return outer.version == inner.version and inner.subnet_of(outer)


def zone_of(source: str, zones: dict[str, list[str]]) -> str | None:
    """Most specific zone containing source; "multi" when it spans zones; None when outside all."""
    if source == "any":
        return "internet"
    try:
        src = net(source)
    except ValueError:
        return None
    best, best_len = None, -1
    for name, cidrs in zones.items():
        for cidr in cidrs:
            if cidr == "any":
                continue
            zone_net = net(cidr)
            if contains(zone_net, src) and zone_net.prefixlen > best_len:
                best, best_len = name, zone_net.prefixlen
    if best:
        return best
    for name, cidrs in zones.items():
        if any(c != "any" and contains(src, net(c)) for c in cidrs):
            return "multi"
    return None


def as_list(value) -> list[str]:
    return [value] if isinstance(value, str) else list(value or [])


def flow_matches(flow: dict, port, proto: str, src_zone: str, dst_zone: str) -> bool:
    if str(flow.get("proto", "tcp")) != proto:
        return False
    if proto in ("tcp", "udp") and str(flow.get("port")) != str(port):
        return False
    return src_zone in as_list(flow.get("from")) and dst_zone in as_list(flow.get("to"))


def find_flow(flows: list[dict], port, proto, src_zone, dst_zone) -> dict | None:
    return next((f for f in flows if flow_matches(f, port, proto, src_zone, dst_zone)), None)


# --------------------------------------------------------------------------- audit


def audit(hosts: list[dict], flows_doc: dict) -> list[str]:
    findings: list[str] = []
    flows = flows_doc.get("flows") or []
    classes = set((flows_doc.get("classes") or {}).keys())
    zone_names = set((flows_doc.get("zones") or {}).keys())
    for flow in flows:
        for zone in as_list(flow.get("from")) + as_list(flow.get("to")):
            if zone not in zone_names:
                findings.append(f"zone-flows: flow {flow.get('service')} names unknown zone {zone}")
    for h in hosts:
        host, zone, zones = h["host"], h["zone"], h["zones"]
        if zone not in zones or zone in ("access", "internet"):
            findings.append(f"{host}: network_zone is {zone!r}; set it through group_vars/<zone>/zone.yml")
            continue
        for other in ("mgmt", "app", "data"):
            if other in h["groups"] and other != zone:
                findings.append(f"{host}: in inventory group {other} but network_zone is {zone}")
        if h["address"]:
            if zone_of(f"{h['address']}/32", {zone: zones[zone]}) != zone:
                findings.append(f"{host}: address {h['address']} is outside its zone {zone} {zones[zone]}")
        else:
            findings.append(f"{host}: no address (add it to network_host_addresses)")
        mgmt_zone_nets = [net(c) for c in zones.get("mgmt", []) if c != "any"]
        mgmt_cidrs = [net(c) for c in h["mgmt_cidrs"]]
        for rule in h["rules"]:
            label = f"{host}: rule {rule.get('name')} ({rule['proto']}/{rule['port']}, {rule['origin']})"
            cls = rule.get("class")
            if cls not in classes:
                findings.append(f"{label}: class {cls!r} is not one of {', '.join(sorted(classes))}")
            if not rule["sources"]:
                findings.append(f"{label}: no sources")
            for src in rule["sources"]:
                src_zone = zone_of(src, zones)
                if src_zone is None:
                    findings.append(f"{label}: source {src} is outside every zone")
                    continue
                if src_zone == "multi":
                    findings.append(f"{label}: source {src} spans several zones")
                    continue
                flow = find_flow(flows, rule["port"], rule["proto"], src_zone, zone)
                if src_zone != zone and not flow:
                    findings.append(f"{label}: {src} ({src_zone} -> {zone}) matches no flow in zone-flows.yml (NET-03)")
                if cls in SENSITIVE:
                    if src == "any":
                        findings.append(f"{label}: {cls} port open to any (NET-02)")
                        continue
                    src_net = net(src)
                    if any(contains(src_net, m) for m in mgmt_zone_nets):
                        findings.append(f"{label}: {cls} port open to the whole mgmt zone ({src}) (NET-02)")
                        continue
                    if any(contains(m, src_net) for m in mgmt_cidrs):
                        continue
                    if not flow:
                        findings.append(f"{label}: {cls} port allows {src} ({src_zone}) without a declared flow (NET-02)")
    return findings


# --------------------------------------------------------------------------- output


def source_label(src: str, zones: dict) -> str:
    zone = zone_of(src, zones) or "?"
    return f"{src} ({zone})"


def markdown(hosts: list[dict], flows_doc: dict, findings: list[str], inventory: Path) -> str:
    out = []
    rel = os.path.relpath(inventory, ROOT)
    out.append("# Network zones and port matrix")
    out.append("")
    out.append("<!-- Generated by tools/firewall_audit.py --markdown; do not edit. CI fails when it is stale. -->")
    out.append("")
    out.append(f"Rendered from `{rel}` and `compliance/zone-flows.yml` (NET-02, NET-03). Each host runs "
               "`roles/firewall`: default deny inbound, then the rules below. Sources are followed by the zone "
               "they belong to; `access` is the VPN, VPN gateway, bastion and automation addresses "
               "(`firewall_mgmt_cidrs`). Admin and database ports allow only `access` plus declared flows, "
               "never `any` and never the whole mgmt zone.")
    out.append("")
    out.append("To open a port: add the rule with its `class` to `firewall_rules_<role>` in the inventory, add a flow to "
               "`compliance/zone-flows.yml` when the source is in another zone, then regenerate this file with "
               "`tools/firewall_audit.py --markdown -o docs/network.md`. CI runs the audit "
               "(`.github/workflows/network.yml`).")
    out.append("")
    out.append("## Zones")
    out.append("")
    out.append("| Zone | CIDRs | Hosts | Description |")
    out.append("| --- | --- | --- | --- |")
    zones = hosts[0]["zones"] if hosts else {}
    for name, spec in (flows_doc.get("zones") or {}).items():
        members = ", ".join(h["host"].split(".")[0] for h in hosts if h["zone"] == name) or "-"
        out.append(f"| {name} | {', '.join(zones.get(name, []))} | {members} | {spec.get('description', '')} |")
    out.append("")
    out.append("## Allowed flows between zones")
    out.append("")
    out.append("| Service | Port | Proto | From | To |")
    out.append("| --- | --- | --- | --- | --- |")
    for f in flows_doc.get("flows") or []:
        out.append(f"| {f['service']} | {f.get('port', '-')} | {f.get('proto', 'tcp')} | "
                   f"{', '.join(as_list(f['from']))} | {', '.join(as_list(f['to']))} |")
    out.append("")
    out.append("## Open ports per host")
    out.append("")
    out.append("Hosts with the same rule set are listed together.")
    groups: dict[str, list[dict]] = {}
    for h in hosts:
        key = json.dumps([h["zone"], [(r.get("name"), r["port"], r["proto"], r.get("class"), r["sources"])
                                      for r in h["rules"]]])
        groups.setdefault(key, []).append(h)
    for members in groups.values():
        first = members[0]
        names = ", ".join(m["host"] for m in members)
        out.append("")
        out.append(f"### {names}")
        out.append("")
        out.append(f"Zone `{first['zone']}`.")
        out.append("")
        out.append("| Port | Proto | Rule | Class | Sources |")
        out.append("| --- | --- | --- | --- | --- |")
        for r in sorted(first["rules"], key=lambda r: (port_key(r["port"]), r["proto"], r.get("name") or "")):
            srcs = "<br>".join(source_label(s, first["zones"]) for s in r["sources"])
            out.append(f"| {r['port'] or '-'} | {r['proto']} | {r.get('name')} | {r.get('class')} | {srcs} |")
    out.append("")
    out.append("## Findings")
    out.append("")
    if findings:
        out.extend(f"- {f}" for f in findings)
    else:
        out.append("None: every inter-zone rule matches a declared flow, and admin and database ports allow only "
                   "the access networks plus declared flows.")
    out.append("")
    return "\n".join(out)


def port_key(port):
    if port is None:
        return -1
    return int(str(port).split("-")[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--flows", type=Path, default=DEFAULT_FLOWS)
    parser.add_argument("--markdown", action="store_true", help="print the port matrix as Markdown")
    parser.add_argument("--json", action="store_true", help="print the rendered rules as JSON")
    parser.add_argument("-o", "--output", type=Path, help="write the Markdown or JSON output to this file")
    parser.add_argument("--check-doc", type=Path, help="fail when this generated Markdown file is out of date")
    args = parser.parse_args()

    flows_doc = yaml.safe_load(args.flows.read_text())
    renderer = Renderer(args.inventory)
    hosts = [render_host(renderer, h, flows_doc) for h in renderer.hosts()]
    findings = audit(hosts, flows_doc)

    text = None
    if args.markdown or args.check_doc:
        text = markdown(hosts, flows_doc, findings, args.inventory)
    if args.json:
        text = json.dumps({"hosts": hosts, "findings": findings}, indent=2, sort_keys=True)
    if args.output and text is not None:
        args.output.write_text(text)
    elif (args.markdown or args.json) and text is not None:
        print(text)

    status = 0
    if args.check_doc:
        md = markdown(hosts, flows_doc, findings, args.inventory)
        if not args.check_doc.exists() or args.check_doc.read_text() != md:
            print(f"ERROR {args.check_doc} is out of date: run tools/firewall_audit.py --markdown -o {args.check_doc}",
                  file=sys.stderr)
            status = 1
    for finding in findings:
        print(f"FINDING {finding}", file=sys.stderr)
    rules = sum(len(h["rules"]) for h in hosts)
    print(f"{len(hosts)} hosts, {rules} rules: {len(findings)} findings", file=sys.stderr)
    return 1 if findings else status


if __name__ == "__main__":
    sys.exit(main())
