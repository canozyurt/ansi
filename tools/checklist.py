#!/usr/bin/env python3
"""Validate compliance/checklist.yml against the code and print coverage reports.

Evidence is read from Ansible tags of the form ``chk_<domain>_<nn>`` (for
example ``chk_iam_06`` for IAM-06). A tag means the task *contributes* to the
requirement; whether the requirement is fully met is the status a person sets
in the checklist. The tags make that status checkable:

* ``implemented`` needs at least one tagged task under ``roles/*/tasks`` or
  ``playbooks``;
* ``verified`` additionally needs a tagged task in a Molecule ``verify*.yml``.

Usage:
    tools/checklist.py check            # exit 1 if a status claims more than the code backs
    tools/checklist.py report [--md]    # coverage table (plain text or Markdown)
    tools/checklist.py matrix --md      # control -> checklist item matrix per framework
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CHECKLIST = ROOT / "compliance" / "checklist.yml"
STATUSES = ["not_started", "designed", "implemented", "verified"]
ID_RE = re.compile(r"^[A-Z]{2,3}-\d{2}$")
TAG_RE = re.compile(r"\bchk_([a-z]{2,3})_(\d{2})\b")
FRAMEWORKS = {"soc2": "SOC 2", "iso27001": "ISO 27001:2022", "iso27017": "ISO 27017"}


def tag_to_id(match: re.Match) -> str:
    return f"{match.group(1).upper()}-{match.group(2)}"


def load_checklist() -> dict:
    with CHECKLIST.open() as fh:
        return yaml.safe_load(fh)


def validate(data: dict) -> list[str]:
    errors = []
    domains = data.get("domains") or {}
    seen = set()
    for item in data.get("requirements") or []:
        rid = item.get("id", "<missing id>")
        if not ID_RE.match(str(rid)):
            errors.append(f"{rid}: id must look like ABC-01")
        elif rid.split("-")[0] not in domains:
            errors.append(f"{rid}: unknown domain prefix")
        if rid in seen:
            errors.append(f"{rid}: duplicate id")
        seen.add(rid)
        for field in ("requirement", "check", "status", "controls"):
            if not item.get(field):
                errors.append(f"{rid}: missing {field}")
        if item.get("status") not in STATUSES:
            errors.append(f"{rid}: status must be one of {', '.join(STATUSES)}")
        controls = item.get("controls") or {}
        if not item.get("project_rule") and not any(controls.get(f) for f in FRAMEWORKS):
            errors.append(f"{rid}: maps to no framework control")
    return errors


def collect_evidence() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    implemented: dict[str, set[str]] = defaultdict(set)
    verified: dict[str, set[str]] = defaultdict(set)
    candidates = list((ROOT / "roles").glob("*/tasks/**/*.yml")) + list(
        (ROOT / "playbooks").glob("**/*.yml")
    )
    for path in candidates:
        for match in TAG_RE.finditer(path.read_text()):
            implemented[tag_to_id(match)].add(str(path.relative_to(ROOT)))
    tests = list((ROOT / "roles").glob("*/molecule/**/verify*.yml")) + list((ROOT / "molecule").glob("**/verify*.yml"))
    for path in tests:
        for match in TAG_RE.finditer(path.read_text()):
            verified[tag_to_id(match)].add(str(path.relative_to(ROOT)))
    return implemented, verified


def evidence(rid: str, implemented, verified) -> str:
    if rid in implemented and rid in verified:
        return "code+tests"
    if rid in implemented:
        return "code"
    return "none"


REQUIRED_EVIDENCE = {"implemented": {"code", "code+tests"}, "verified": {"code+tests"}}


def cmd_check(data: dict) -> int:
    errors = validate(data)
    implemented, verified = collect_evidence()
    known = {item["id"] for item in data["requirements"]}
    for rid in sorted(set(implemented) | set(verified)):
        if rid not in known:
            where = sorted(implemented.get(rid, set()) | verified.get(rid, set()))
            errors.append(f"tag for unknown checklist item {rid} in {', '.join(where)}")
    for item in data["requirements"]:
        rid, claimed = item["id"], item["status"]
        found = evidence(rid, implemented, verified)
        if claimed in REQUIRED_EVIDENCE and found not in REQUIRED_EVIDENCE[claimed]:
            errors.append(f"{rid}: status '{claimed}' but evidence in code is '{found}'")
    for error in errors:
        print(f"ERROR {error}")
    counts = defaultdict(int)
    for item in data["requirements"]:
        counts[item["status"]] += 1
    summary = ", ".join(f"{counts[s]} {s}" for s in STATUSES)
    print(f"{len(data['requirements'])} requirements: {summary}; {len(errors)} errors")
    return 1 if errors else 0


def cmd_report(data: dict, markdown: bool) -> int:
    implemented, verified = collect_evidence()
    rows = []
    for item in data["requirements"]:
        rid = item["id"]
        rows.append((rid, item["status"], evidence(rid, implemented, verified), item["requirement"]))
    if markdown:
        print("| ID | Status | Evidence | Requirement |")
        print("| --- | --- | --- | --- |")
        for row in rows:
            print("| " + " | ".join(row) + " |")
    else:
        for rid, claimed, actual, req in rows:
            print(f"{rid:7} {claimed:12} {actual:12} {req[:90]}")
    return 0


def cmd_matrix(data: dict, markdown: bool) -> int:
    for key, name in FRAMEWORKS.items():
        mapping: dict[str, list[str]] = defaultdict(list)
        for item in data["requirements"]:
            for control in (item.get("controls") or {}).get(key) or []:
                mapping[str(control)].append(item["id"])
        print(f"## {name}\n" if markdown else f"{name}")
        if markdown:
            print("| Control | Checklist items |\n| --- | --- |")
        for control in sorted(mapping, key=natural_key):
            items = ", ".join(mapping[control])
            print(f"| {control} | {items} |" if markdown else f"  {control:12} {items}")
        print()
    return 0


def natural_key(value: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", value)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "report", "matrix"])
    parser.add_argument("--md", action="store_true", help="Markdown output")
    args = parser.parse_args()
    data = load_checklist()
    if args.command == "check":
        return cmd_check(data)
    if args.command == "report":
        return cmd_report(data, args.md)
    return cmd_matrix(data, args.md)


if __name__ == "__main__":
    sys.exit(main())
