#!/usr/bin/env python3
"""Check compliance/tools.yml against roles/versions (EVD-04).

Fails when:
* a roles/versions entry (versions_images, versions_tools, versions_packages, versions_postgres
  packages and patroni) is not listed under any tool's ``versions``;
* tools.yml names a versions entry that does not exist;
* an entry lacks a field, uses a status other than yes/no/n.a., or has a "no" (or a non-OSI licence)
  without a documented exception.

Usage:
    tools/tool_registry.py            # check
    tools/tool_registry.py --md       # also print a Markdown table of the tools
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"yes", "no", "n.a."}
CAPABILITIES = ("sso", "audit_log", "ha")


def versions_entries(root: Path) -> set[str]:
    data = yaml.safe_load((root / "roles" / "versions" / "defaults" / "main.yml").read_text()) or {}
    entries = {f"images/{k}" for k in data.get("versions_images") or {}}
    entries |= {f"tools/{k}" for k in data.get("versions_tools") or {}}
    entries |= {f"packages/{k}" for k in data.get("versions_packages") or {}}
    pg = data.get("versions_postgres") or {}
    entries |= {f"postgres/{k}" for k in pg.get("packages") or {}}
    if pg.get("patroni"):
        entries.add("postgres/patroni")
    return entries


def check(root: Path) -> tuple[list[str], list[dict]]:
    errors = []
    path = root / "compliance" / "tools.yml"
    if not path.exists():
        return [f"{path.relative_to(root)} is missing"], []
    tools = (yaml.safe_load(path.read_text()) or {}).get("tools") or []
    expected = versions_entries(root)
    listed: dict[str, str] = {}
    for i, tool in enumerate(tools):
        name = tool.get("name") or f"tools[{i}]"
        for field in ("name", "license", "osi_approved"):
            if tool.get(field) in (None, ""):
                errors.append(f"{name}: missing {field}")
        if not tool.get("versions") and not tool.get("packages"):
            errors.append(f"{name}: lists neither versions entries nor packages")
        for entry in tool.get("versions") or []:
            if entry in listed:
                errors.append(f"{name}: {entry} is already listed under {listed[entry]}")
            listed[entry] = name
            if entry not in expected:
                errors.append(f"{name}: {entry} is not in roles/versions")
        needs_exception = []
        for cap in CAPABILITIES:
            value = tool.get(cap)
            if not isinstance(value, dict) or str(value.get("status")) not in STATUSES:
                errors.append(f"{name}: {cap} needs status yes, no or n.a.")
                continue
            if value["status"] != "yes" and not str(value.get("note", "")).strip():
                errors.append(f"{name}: {cap} is {value['status']} without a note")
            if value["status"] == "no":
                needs_exception.append(cap)
        if tool.get("osi_approved") is False:
            needs_exception.append("licence not OSI-approved")
        if needs_exception and not tool.get("exceptions"):
            errors.append(f"{name}: no documented exception for {', '.join(needs_exception)}")
    for entry in sorted(expected - set(listed)):
        errors.append(f"roles/versions {entry} has no entry in compliance/tools.yml")
    return errors, tools


def markdown(tools: list[dict]) -> str:
    rows = ["| Tool | Licence | OSI | SSO | Audit log | HA |", "| --- | --- | --- | --- | --- | --- |"]
    for tool in tools:
        caps = [str((tool.get(c) or {}).get("status", "?")) for c in CAPABILITIES]
        osi = "yes" if tool.get("osi_approved") else "no"
        rows.append(f"| {tool.get('name')} | {tool.get('license')} | {osi} | " + " | ".join(caps) + " |")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--md", action="store_true")
    args = parser.parse_args()
    errors, tools = check(args.root)
    if args.md:
        print(markdown(tools))
    for error in errors:
        print(f"ERROR {error}")
    print(f"{len(tools)} tools in compliance/tools.yml: {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
