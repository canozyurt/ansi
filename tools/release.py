#!/usr/bin/env python3
"""Release bundles (CHG-07): upgrade paths, release notes and the per-release document checks.

A release is a git tag ``vX.Y.Z`` of this repository. Every version it deploys is pinned in
roles/versions/defaults/main.yml; ``versions_upgrade_from`` (images) and
``versions_postgres_upgrade_from`` (the PostgreSQL image recipe) hold what the *previous* release
deployed, so the Molecule ``*-upgrade`` scenarios install that first and upgrade it. See
docs/releasing.md.

Commands:
    tools/release.py has-upgrade-path SCENARIO
        Exit 0 when the scenario needs nothing or its previous release is pinned, 3 when the
        previous release is not pinned yet (no earlier release shipped the service), 2 on errors.
        CI skips the scenario on 3 with a notice.
    tools/release.py status
        One line per *-upgrade scenario: ready or waiting for a release.
    tools/release.py notes VERSION [--since TAG]
        Release notes (Markdown) for VERSION: every version change since the previous tag, the
        upgrade scenarios that test it, and the release checklist.
    tools/release.py bump-upgrade-from TAG [--dry-run]
        After tagging TAG: copy the versions TAG deployed into versions_upgrade_from and
        versions_postgres_upgrade_from, so the next release is tested against TAG.
    tools/release.py check
        Per-release document checks: every service with a backup or HA drill has a DR runbook
        listed in docs/dr/README.md (HA-02), docs/shared-responsibility.md covers every ISO 27017
        CLD control and every checklist item that maps to one (DAT-03), and every *-upgrade
        scenario runs in CI (CHG-07).

Options: --root DIR checks another tree (self-tests).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

VERSIONS = Path("roles/versions/defaults/main.yml")
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
KEYS_RE = re.compile(r"check_upgrade_from_keys:\s*\[([^\]]*)\]")
VARS_RE = re.compile(r"check_upgrade_from_vars:\s*\[([^\]]*)\]")
REF_RE = re.compile(r"versions_upgrade_from\.([a-z0-9_]+)")

# ISO/IEC 27017:2015 cloud-specific controls (the CLD extended control set).
CLD_CONTROLS = [
    "CLD.6.3.1",
    "CLD.8.1.5",
    "CLD.9.5.1",
    "CLD.9.5.2",
    "CLD.12.1.5",
    "CLD.12.4.5",
    "CLD.13.1.4",
]


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


def split_list(text: str) -> list[str]:
    return [x.strip().strip("'\"") for x in text.split(",") if x.strip()]


def upgrade_requirements(root: Path, scenario: str) -> tuple[list[str], list[str]] | None:
    """Image keys and variables a scenario takes from the previous release; None if unknown."""
    sdir = root / "molecule" / scenario
    if not (sdir / "molecule.yml").is_file():
        return None
    keys: list[str] = []
    variables: list[str] = []
    for path in sorted(sdir.glob("*.yml")):
        text = path.read_text()
        for m in KEYS_RE.finditer(text):
            keys += split_list(m.group(1))
        for m in VARS_RE.finditer(text):
            variables += split_list(m.group(1))
        keys += REF_RE.findall(text)
    return sorted(set(keys)), sorted(set(variables))


def missing_for(root: Path, scenario: str) -> list[str] | None:
    req = upgrade_requirements(root, scenario)
    if req is None:
        return None
    keys, variables = req
    data = load_yaml(root / VERSIONS)
    have = data.get("versions_upgrade_from") or {}
    missing = [f"versions_upgrade_from.{k}" for k in keys if not have.get(k)]
    missing += [v for v in variables if not data.get(v)]
    return missing


def cmd_has_upgrade_path(root: Path, args) -> int:
    missing = missing_for(root, args.scenario)
    if missing is None:
        print(f"unknown scenario {args.scenario}", file=sys.stderr)
        return 2
    if missing:
        print(f"{args.scenario}: previous release not pinned ({', '.join(missing)})")
        return 3
    print(f"{args.scenario}: upgrade path pinned")
    return 0


def upgrade_scenarios(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "molecule").glob("*-upgrade") if (p / "molecule.yml").is_file())


def cmd_status(root: Path, _args) -> int:
    for scenario in upgrade_scenarios(root):
        missing = missing_for(root, scenario) or []
        print(f"{scenario:24} {'ready' if not missing else 'waiting for a release: ' + ', '.join(missing)}")
    return 0


def git(root: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True, text=True).stdout


def tags(root: Path) -> list[str]:
    found = [t for t in git(root, "tag", "--list", "v*").split() if SEMVER_RE.match(t)]
    return sorted(found, key=lambda t: tuple(int(x) for x in SEMVER_RE.match(t).groups()))


def versions_at(root: Path, ref: str | None) -> dict:
    if ref is None:
        return load_yaml(root / VERSIONS)
    return yaml.safe_load(git(root, "show", f"{ref}:{VERSIONS}")) or {}


def flatten(data: dict) -> dict[str, str]:
    flat = {f"images/{k}": str(v) for k, v in (data.get("versions_images") or {}).items()}
    flat |= {f"packages/{k}": str(v) for k, v in (data.get("versions_packages") or {}).items()}
    flat |= {f"tools/{k}": str(v.get("version")) for k, v in (data.get("versions_tools") or {}).items()}
    pg = data.get("versions_postgres") or {}
    flat |= {f"postgres/{k}": str(v) for k, v in (pg.get("packages") or {}).items()}
    if pg.get("patroni"):
        flat["postgres/patroni"] = str(pg["patroni"].get("version"))
    return flat


def cmd_notes(root: Path, args) -> int:
    if not SEMVER_RE.match(args.version):
        print(f"{args.version} is not vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    known = tags(root)
    since = args.since or (known[-1] if known else None)
    if since and tuple(int(x) for x in SEMVER_RE.match(args.version).groups()) <= tuple(
            int(x) for x in SEMVER_RE.match(since).groups()):
        print(f"{args.version} is not newer than {since}", file=sys.stderr)
        return 2
    old = flatten(versions_at(root, since)) if since else {}
    new = flatten(versions_at(root, None))
    out = [f"# Release {args.version}", ""]
    out.append(f"Previous release: {since}." if since else "First release.")
    out += ["", "## Version changes", "", "| Component | Before | After |", "| --- | --- | --- |"]
    changed = 0
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changed += 1
            out.append(f"| {key} | {old.get(key, '(new)')} | {new.get(key, '(removed)')} |")
    if not changed:
        out.append("| (none) | | |")
    out += ["", "## Upgrade paths tested in CI", ""]
    for scenario in upgrade_scenarios(root):
        missing = missing_for(root, scenario) or []
        out.append(f"- {scenario}: {'from ' + (since or 'the previous release') if not missing else 'not yet (new service)'}")
    out += [
        "",
        "## Services that need a recreate update",
        "",
        "(Fill in from the upstream release notes and the upgrade scenario logs: services whose nodes",
        "all restart on the new version, with a short outage. Write \"none\" if every change is rolling.)",
        "",
        "## Release checklist (docs/releasing.md)",
        "",
        "- [ ] CI green on the release commit, including every *-upgrade scenario and the nightly HA drills",
        "- [ ] `tools/release.py check` passes; docs/shared-responsibility.md reviewed for this release (DAT-03)",
        "- [ ] DR runbooks in docs/dr/ still match the roles (HA-02)",
        "- [ ] Notes above completed (recreate updates, manual steps)",
        f"- [ ] Tag signed: `git tag -s {args.version}`",
        f"- [ ] Follow-up pull request: `tools/release.py bump-upgrade-from {args.version}`",
        "",
    ]
    print("\n".join(out))
    return 0


def replace_block(text: str, name: str, body: dict) -> str:
    """Replace the top-level YAML key `name` (up to the next top-level key or comment block)."""
    rendered = yaml.safe_dump({name: body}, default_flow_style=False, sort_keys=False, width=200).rstrip("\n")
    if not body:
        rendered = f"{name}: {{}}"
    pattern = re.compile(rf"^{re.escape(name)}:.*?(?=^\S|\Z)", re.M | re.S)
    if not pattern.search(text):
        raise SystemExit(f"{VERSIONS}: no top-level {name}")
    def sub(match: re.Match) -> str:
        trailing = match.group(0)[len(match.group(0).rstrip("\n")):]
        return rendered + (trailing if len(trailing) > 1 or match.end() == len(text) else "\n\n")

    return pattern.sub(sub, text, count=1)


def cmd_bump_upgrade_from(root: Path, args) -> int:
    if not SEMVER_RE.match(args.tag):
        print(f"{args.tag} is not vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    if not args.from_file and args.tag not in tags(root):
        print(f"tag {args.tag} does not exist; tag the release first", file=sys.stderr)
        return 2
    released = load_yaml(Path(args.from_file)) if args.from_file else versions_at(root, args.tag)
    path = root / VERSIONS
    text = path.read_text()
    text = replace_block(text, "versions_upgrade_from", dict(released.get("versions_images") or {}))
    text = replace_block(text, "versions_postgres_upgrade_from", dict(released.get("versions_postgres") or {}))
    if args.dry_run:
        print(text)
        return 0
    path.write_text(text)
    yaml.safe_load(text)  # still valid YAML
    print(f"{VERSIONS}: versions_upgrade_from and versions_postgres_upgrade_from now hold {args.tag}")
    return 0


def check_docs(root: Path) -> list[str]:
    errors: list[str] = []
    # HA-02: a DR runbook per service the DR drill knows, each listed in the rebuild order.
    dr_dir = root / "docs" / "dr"
    readme = (dr_dir / "README.md").read_text() if (dr_dir / "README.md").is_file() else ""
    if not readme:
        errors.append("docs/dr/README.md is missing")
    defaults = root / "roles" / "dr_drill" / "defaults" / "main.yml"
    services = (load_yaml(defaults).get("dr_drill_all_services") or []) if defaults.is_file() else []
    if not services:
        errors.append("roles/dr_drill/defaults/main.yml has no dr_drill_all_services")
    for service in services:
        runbook = f"{service.replace('_', '-')}.md"
        if not (dr_dir / runbook).is_file():
            errors.append(f"docs/dr/{runbook} is missing (DR runbook for {service})")
        elif f"({runbook})" not in readme:
            errors.append(f"docs/dr/README.md does not link {runbook} in the rebuild order")
    for runbook in sorted(dr_dir.glob("*.md")) if dr_dir.is_dir() else []:
        if runbook.name != "README.md" and f"({runbook.name})" not in readme:
            errors.append(f"docs/dr/README.md does not link {runbook.name}")
    # DAT-03: every CLD control, and every checklist item mapped to one, is in the matrix.
    matrix_path = root / "docs" / "shared-responsibility.md"
    matrix = matrix_path.read_text() if matrix_path.is_file() else ""
    if not matrix:
        errors.append("docs/shared-responsibility.md is missing")
    checklist = load_yaml(root / "compliance" / "checklist.yml").get("requirements") or []
    wanted = set(CLD_CONTROLS)
    for item in checklist:
        wanted |= set((item.get("controls") or {}).get("iso27017") or [])
    for control in sorted(wanted):
        if matrix and control not in matrix:
            errors.append(f"docs/shared-responsibility.md does not cover {control}")
    # CHG-07: every upgrade scenario runs in CI.
    ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    for scenario in upgrade_scenarios(root):
        if not re.search(rf"^\s*-\s*{re.escape(scenario)}\s*$", ci, re.M):
            errors.append(f".github/workflows/ci.yml does not run molecule/{scenario}")
    return errors


def cmd_check(root: Path, _args) -> int:
    errors = check_docs(root)
    for error in errors:
        print(f"ERROR {error}")
    print(f"release document checks: {len(errors)} error(s)")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=Path(__file__).resolve().parent.parent, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("has-upgrade-path")
    p.add_argument("scenario")
    sub.add_parser("status")
    p = sub.add_parser("notes")
    p.add_argument("version")
    p.add_argument("--since")
    p = sub.add_parser("bump-upgrade-from")
    p.add_argument("tag")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-file", help=argparse.SUPPRESS)  # self-tests: a versions file instead of the tag
    sub.add_parser("check")
    args = parser.parse_args()
    handler = {
        "has-upgrade-path": cmd_has_upgrade_path,
        "status": cmd_status,
        "notes": cmd_notes,
        "bump-upgrade-from": cmd_bump_upgrade_from,
        "check": cmd_check,
    }[args.command]
    return handler(args.root, args)


if __name__ == "__main__":
    sys.exit(main())
