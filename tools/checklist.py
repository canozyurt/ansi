#!/usr/bin/env python3
"""Validate compliance/checklist.yml against the code and print coverage reports.

Evidence is read from Ansible tags of the form ``chk_<domain>_<nn>`` (for
example ``chk_iam_06`` for IAM-06). A tag means the task *contributes* to the
requirement; whether the requirement is fully met is the status a person sets
in the checklist. The tags make that status checkable:

* ``implemented`` needs at least one tagged task under ``roles/*/tasks`` or
  ``playbooks``, or a tagged CI step in ``.github/workflows`` (for controls that live in CI,
  such as secret scanning);
* ``verified`` additionally needs a tagged task in a Molecule ``verify*.yml``.

Core items (``core: true``: identity, secrets, logging and monitoring, hardening, network, backup
and change management) must have a test (EVD-01). ``check`` fails for a Core item marked
``verified`` without a tagged verify task and warns for a Core item marked ``implemented``
without one; ``--strict-core`` turns that warning into an error (switch it on in CI once Molecule
runs again; see the EVD-01 progress note in compliance/checklist.yml).

Usage:
    tools/checklist.py check [--strict-core]  # exit 1 if a status claims more than the code backs
    tools/checklist.py report [--md]          # coverage table (plain text or Markdown)
    tools/checklist.py coverage [--md]        # roles and checklist items without a test (CHG-06)
    tools/checklist.py matrix --md            # control -> checklist item matrix per framework
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
        if "core" in item and not isinstance(item["core"], bool):
            errors.append(f"{rid}: core must be true or false")
    return errors


def collect_evidence() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    implemented: dict[str, set[str]] = defaultdict(set)
    verified: dict[str, set[str]] = defaultdict(set)
    candidates = (
        list((ROOT / "roles").glob("*/tasks/**/*.yml"))
        + list((ROOT / "playbooks").glob("**/*.yml"))
        + list((ROOT / ".github" / "workflows").glob("*.yml"))
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


def cmd_check(data: dict, strict_core: bool = False) -> int:
    errors = validate(data)
    warnings = []
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
        if item.get("core") and claimed in ("implemented", "verified") and rid not in verified:
            message = f"{rid}: Core item is '{claimed}' but no Molecule verify task carries chk_{rid.lower().replace('-', '_')}"
            if claimed == "verified" or strict_core:
                errors.append(message)
            else:
                warnings.append(message)
    for warning in warnings:
        print(f"WARN  {warning}")
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


def scenario_files(scenario: Path) -> list[Path]:
    """Playbooks a Molecule scenario runs, plus the repository playbooks they import."""
    files = [p for p in scenario.glob("*.yml") if p.name != "molecule.yml"]
    queue, seen = list(files), set(files)
    while queue:
        text = queue.pop().read_text()
        for ref in re.findall(r"import_playbook:\s*['\"]?([^'\"\s]+)", text):
            target = (scenario / ref).resolve()
            if not target.exists():
                target = (ROOT / "playbooks" / Path(ref).name).resolve()
            if target.exists() and target not in seen:
                seen.add(target)
                files.append(target)
                queue.append(target)
    return files


ROLE_REF_RE = r"(?:role:\s*|name:\s*|^\s*-\s+)['\"]?{name}['\"]?\s*$"


def roles_used(texts: list[str], roles: list[str]) -> set[str]:
    """Roles referenced from the given texts, following role dependencies and include_role."""
    found: set[str] = set()
    pending = list(texts)
    while pending:
        text = pending.pop()
        for role in roles:
            if role in found:
                continue
            if re.search(ROLE_REF_RE.format(name=re.escape(role)), text, re.M):
                found.add(role)
                for sub in ("meta", "tasks", "handlers"):
                    pending.extend(p.read_text() for p in (ROOT / "roles" / role / sub).glob("**/*.yml"))
    return found


def molecule_platforms(scenario: Path) -> str:
    data = yaml.safe_load((scenario / "molecule.yml").read_text()) or {}
    images = [str(p.get("image", "")) for p in data.get("platforms") or []]
    if any("MOLECULE_DISTRO" in i for i in images):
        return "debian12, ubuntu2204, ubuntu2404 (MOLECULE_DISTRO matrix)"
    distros = sorted({m for i in images for m in re.findall(r"(debian\d+|ubuntu\d+)", i)})
    return ", ".join(distros) or "?"


def cmd_coverage(data: dict, markdown: bool) -> int:
    roles = sorted(p.name for p in (ROOT / "roles").iterdir() if (p / "tasks").is_dir())
    scenarios = sorted(p for p in (ROOT / "molecule").iterdir() if (p / "molecule.yml").exists())
    covered: dict[str, list[str]] = defaultdict(list)
    platforms = {}
    for scenario in scenarios:
        texts = [p.read_text() for p in scenario_files(scenario)]
        for role in roles_used(texts, roles):
            covered[role].append(scenario.name)
        platforms[scenario.name] = molecule_platforms(scenario)
    implemented, verified = collect_evidence()
    untested_roles = [r for r in roles if r not in covered]
    untested_items = [i for i in data["requirements"] if i["id"] not in verified]
    core_gaps = [i["id"] for i in untested_items if i.get("core") and i["status"] in ("implemented", "verified")]
    if markdown:
        print("### Molecule scenarios\n\n| Scenario | Platforms |\n| --- | --- |")
        for name, plats in platforms.items():
            print(f"| {name} | {plats} |")
        print(f"\n### Roles without a Molecule scenario ({len(untested_roles)} of {len(roles)})\n")
        print(", ".join(untested_roles) or "none")
        print(f"\n### Checklist items without a tagged verify task ({len(untested_items)} of {len(data['requirements'])})\n")
        print("| ID | Core | Status | Code evidence |\n| --- | --- | --- | --- |")
        for item in untested_items:
            core = "yes" if item.get("core") else ""
            print(f"| {item['id']} | {core} | {item['status']} | {'yes' if item['id'] in implemented else 'no'} |")
        print(f"\nCore items claimed implemented/verified without a test: {', '.join(core_gaps) or 'none'}")
    else:
        print("Molecule scenarios:")
        for name, plats in platforms.items():
            print(f"  {name:20} {plats}")
        print(f"Roles without a Molecule scenario ({len(untested_roles)} of {len(roles)}):")
        for role in untested_roles:
            print(f"  {role}")
        print(f"Checklist items without a tagged verify task ({len(untested_items)} of {len(data['requirements'])}):")
        for item in untested_items:
            core = "core" if item.get("core") else ""
            print(f"  {item['id']:7} {core:4} {item['status']:12} {item['requirement'][:80]}")
        print(f"Core items claimed implemented/verified without a test: {', '.join(core_gaps) or 'none'}")
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
    parser.add_argument("command", choices=["check", "report", "coverage", "matrix"])
    parser.add_argument("--md", action="store_true", help="Markdown output")
    parser.add_argument("--strict-core", action="store_true", help="Core items marked implemented also need a test")
    args = parser.parse_args()
    data = load_checklist()
    if args.command == "check":
        return cmd_check(data, args.strict_core)
    if args.command == "report":
        return cmd_report(data, args.md)
    if args.command == "coverage":
        return cmd_coverage(data, args.md)
    return cmd_matrix(data, args.md)


if __name__ == "__main__":
    sys.exit(main())
