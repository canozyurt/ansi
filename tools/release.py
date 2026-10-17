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
        upgrade scenarios that test it, and the release checklist. With no earlier tag this is the
        first release (v0.1.0): every component is new and nothing upgrades from an earlier release.
    tools/release.py cut VERSION [--notes-out FILE] [--allow-dirty]
        Pre-flight for tagging VERSION: a clean tree, a version newer than the last tag (the first
        release must be v0.1.0), HEAD not tagged yet, the document and upgrade-coverage checks. Writes
        the notes and prints the signed-tag and follow-up commands. It never creates or pushes a tag.
    tools/release.py changelog VERSION [--date YYYY-MM-DD]
        Release commit: turn the Unreleased section of CHANGELOG.md into the VERSION section.
    tools/release.py bump-upgrade-from TAG [--dry-run]
        After tagging TAG: copy the versions TAG deployed into versions_upgrade_from and
        versions_postgres_upgrade_from, so the next release is tested against TAG.
    tools/release.py check
        Per-release document checks: every service with a backup or HA drill has a DR runbook
        listed in docs/dr/README.md (HA-02), docs/shared-responsibility.md covers every ISO 27017
        CLD control and every checklist item that maps to one (DAT-03), every *-upgrade
        scenario runs in CI, and every role that deploys a pinned service has a play in
        playbooks/upgrade.yml and an upgrade scenario, or is listed as exempt with a reason (CHG-07).
        Inventory changes: inventory-changes.yml is valid and keeps every entry the last tag holds,
        every role default removed since the last tag has a renamed or removed entry, no role reads
        a removed variable, inventories/example passes tools/inventory_check.py, and CHANGELOG.md
        names every new entry under "Inventory changes".

Options: --root DIR checks another tree (self-tests).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inventory_check  # noqa: E402  (tools/inventory_check.py)

VERSIONS = Path("roles/versions/defaults/main.yml")
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
KEYS_RE = re.compile(r"check_upgrade_from_keys:\s*\[([^\]]*)\]")
VARS_RE = re.compile(r"check_upgrade_from_vars:\s*\[([^\]]*)\]")
REF_RE = re.compile(r"versions_upgrade_from\.([a-z0-9_]+)")

FIRST_RELEASE = "v0.1.0"
CHANGELOG = Path("CHANGELOG.md")
SECTION_RE = re.compile(r"^## \[([^\]]+)\][^\n]*\n", re.M)
INVENTORY_HEADING_RE = re.compile(r"^### Inventory changes\s*$", re.M)

# CHG-07: every role that deploys a pinned, long-running service is upgraded by playbooks/upgrade.yml
# (one node at a time, backup first) and has a Molecule scenario that installs the previous release
# and runs that playbook. None means the role has a play but no scenario of its own; the reason says why.
UPGRADE_COVERAGE: dict[str, str | None] = {
    "openbao": "openbao-upgrade",
    "keycloak": "keycloak-upgrade",
    "postgres_cluster": "postgres-upgrade",
    "valkey_cluster": "valkey-upgrade",
    "rabbitmq_cluster": "rabbitmq-upgrade",
    "garage_cluster": "garage-upgrade",
    "wazuh_indexer": "siem-upgrade",
    "wazuh_manager": "siem-upgrade",
    "wazuh_dashboard": "siem-upgrade",
    "wazuh_agent": "siem-upgrade",
    "registry": "registry-upgrade",
    "backup_server": "backup-upgrade",
    "forgejo": "forgejo-upgrade",
    "forgejo_runner": "forgejo-upgrade",
    "edge": "edge-upgrade",
    "status_page": "governance-upgrade",
    "grc": "governance-upgrade",
    "prometheus": "monitoring-upgrade",
    "alertmanager": "monitoring-upgrade",
    "grafana": "monitoring-upgrade",
    "blackbox_exporter": "monitoring-upgrade",
    "node_exporter": "monitoring-upgrade",
    # Stateless SSO sidecar, upgraded in the monitoring play; the monitoring lab has no Keycloak.
    "oauth2_proxy": None,
}

# Roles that read roles/versions but run no long-lived service, so there is nothing to upgrade in
# place: the next site.yml run (or the next scheduled run) uses the new pin.
UPGRADE_EXEMPT: dict[str, str] = {
    "backup": "restic client binary; the repositories are served by backup_server",
    "cis_audit": "scheduled scan job",
    "docker_bench": "scheduled scan job",
    "tls_scan": "scheduled scan job",
    "trivy_scan": "scheduled scan job",
    "data_lifecycle": "on-demand export and deletion playbooks",
    "dr_drill": "on-demand restore drill into scratch space",
    "openbao_secrets": "secret rotation and checks against OpenBao; no service of its own",
    "image_verify": "signature check run by the container roles before they create a container",
}
VERSION_REF_RE = re.compile(r"\bversions_(images|packages|tools|postgres)\b")

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
    if since:
        out.append(f"Previous release: {since}.")
    else:
        out += [
            "First release of this repository. Every component below is new, and there is no earlier",
            "release to upgrade from: install with playbooks/site.yml. The upgrade scenarios marked",
            "\"ready\" below were tested from the predecessor versions pinned in versions_upgrade_from,",
            "which are upstream releases, not releases of this repository.",
        ]
    out += ["", "## Version changes", "", "| Component | Before | After |", "| --- | --- | --- |"]
    changed = 0
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            changed += 1
            out.append(f"| {key} | {old.get(key, '(new)')} | {new.get(key, '(removed)')} |")
    if not changed:
        out.append("| (none) | | |")
    out += inventory_notes(root, args.version, since)
    out += ["", "## Upgrade paths tested in CI", ""]
    for scenario in upgrade_scenarios(root):
        missing = missing_for(root, scenario) or []
        if missing:
            state = "not yet (no earlier release of the service)"
        elif since:
            state = f"from {since}"
        else:
            state = "ready, from the upstream predecessor versions in versions_upgrade_from"
        out.append(f"- {scenario}: {state}")
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
        f"- [ ] CHANGELOG.md has a [{args.version}] section; every inventory change has an entry in inventory-changes.yml",
        f"- [ ] Tag signed: `git tag -s {args.version}`",
        f"- [ ] Follow-up pull request: `tools/release.py bump-upgrade-from {args.version}`",
        "",
    ]
    print("\n".join(out))
    return 0


# ---------------------------------------------------------------- changelog and inventory changes

def changelog_sections(root: Path) -> dict[str, str]:
    """CHANGELOG.md section name (Unreleased, vX.Y.Z) -> body, in file order."""
    path = root / CHANGELOG
    text = path.read_text() if path.is_file() else ""
    matches = list(SECTION_RE.finditer(text))
    return {m.group(1): text[m.end():(matches[i + 1].start() if i + 1 < len(matches) else len(text))]
            for i, m in enumerate(matches)}


def has_entries(body: str) -> bool:
    return bool(re.search(r"^\s*[-*] ", body, re.M))


def inventory_section(body: str) -> str:
    match = INVENTORY_HEADING_RE.search(body)
    if not match:
        return ""
    rest = body[match.end():]
    end = re.search(r"^#{2,3} ", rest, re.M)
    return rest[:end.start()] if end else rest


def new_inventory_changes(root: Path, since: str | None) -> list[dict]:
    old = inventory_check.load_changes(root, since) if since else []
    return [e for e in inventory_check.load_changes(root) if e not in old]


def describe_change(entry: dict) -> str:
    kind, var, note = entry["kind"], entry["var"], str(entry.get("note", "")).strip()
    if kind == "renamed":
        until = entry.get("removal_planned", "a later major release")
        head = f"`{var}` renamed to `{entry['new']}`; the old name works until {until}"
    elif kind == "removed":
        head = f"`{var}` removed; delete it from the inventory"
    elif kind == "required":
        head = f"`{var}` is now required for {', '.join(entry.get('groups') or ['all hosts'])}"
    else:
        head = f"default of `{var}` changed from `{entry['old']}` to `{entry['new']}`"
    return f"- {head}. {note}".rstrip()


def inventory_notes(root: Path, version: str, since: str | None) -> list[str]:
    sections = changelog_sections(root)
    body = sections.get(version) or sections.get("Unreleased") or ""
    out = ["", "## Changes", "", body.strip() or f"(CHANGELOG.md has no [{version}] section yet)"]
    out += ["", "## Inventory changes", ""]
    changes = new_inventory_changes(root, since)
    out += [describe_change(e) for e in changes] or ["None: inventories need no edits for this release."]
    out += ["", "Check each inventory before upgrading; errors must be fixed first:", "", "```sh",
            f"git fetch --tags && python3 tools/inventory_check.py <inventory> --release {version}", "```"]
    return out


def role_defaults(root: Path, ref: str | None = None) -> set[str]:
    """Top-level variables of every roles/*/defaults/main.yml, in the tree or at a git ref."""
    if ref is None:
        texts = [p.read_text() for p in sorted(root.glob("roles/*/defaults/main.yml"))]
    else:
        names = git(root, "ls-tree", "-r", "--name-only", ref, "roles").split()
        texts = [git(root, "show", f"{ref}:{n}") for n in names if re.fullmatch(r"roles/[^/]+/defaults/main\.yml", n)]
    found: set[str] = set()
    for text in texts:
        data = yaml.load(text, Loader=inventory_check.Loader)  # noqa: S506 (SafeLoader subclass)
        if isinstance(data, dict):
            found |= set(data)
    return found


def code_text(root: Path) -> str:
    """Everything roles and playbooks can read a variable from (not the inventory check itself)."""
    paths = [p for pattern in ("roles/**/*", "playbooks/**/*") for p in root.glob(pattern)
             if p.is_file() and p.suffix in (".yml", ".yaml", ".j2", ".py") and p.name != "inventory_check.yml"]
    return "\n".join(p.read_text(errors="replace") for p in paths)


def check_inventory_changes(root: Path) -> list[str]:
    if not (root / inventory_check.MANIFEST).is_file():
        return [f"{inventory_check.MANIFEST} is missing (docs/upgrades.md#inventory-changes-between-releases)"]
    changes = inventory_check.load_changes(root)
    errors = inventory_check.validate(changes)
    if errors:
        return errors
    known = tags(root)
    previous = known[-1] if known else None
    if previous:
        for entry in inventory_check.load_changes(root, previous):
            if entry not in changes:
                errors.append(f"{inventory_check.MANIFEST}: the {entry.get('kind')} entry for {entry.get('var')} is "
                              f"part of {previous}; entries a release holds are never edited or deleted")
        covered = {e["var"] for e in changes if e["kind"] in ("renamed", "removed")}
        for var in sorted(role_defaults(root, previous) - role_defaults(root) - covered):
            errors.append(f"{var} was a role default in {previous} and is gone: add a renamed or removed entry to "
                          f"{inventory_check.MANIFEST} (docs/upgrades.md#variable-deprecation-policy)")
    code = code_text(root)
    for entry in changes:
        if entry["kind"] == "removed" and re.search(rf"\b{entry['var']}\b", code):
            errors.append(f"{entry['var']} is listed as removed in {inventory_check.MANIFEST} but roles or playbooks "
                          "still read it")
        if entry["kind"] == "renamed" and not re.search(rf"\b{entry['new']}\b", code):
            errors.append(f"{entry['var']} is renamed to {entry['new']}, which no role or playbook reads")
    example = root / "inventories" / "example"
    if (example / "hosts.yml").is_file():
        for f in inventory_check.evaluate(changes, inventory_check.load_inventory(example)):
            if f.level in ("ERROR", "WARN"):
                errors.append(f"inventories/example: {f.var}: {f.message}")
    sections = changelog_sections(root)
    if not sections:
        errors.append(f"{CHANGELOG} is missing or has no '## [Unreleased]' section")
        return errors
    unreleased = "".join(inventory_section(body) for name, body in sections.items()
                         if name == "Unreleased" or (SEMVER_RE.match(name) and (
                             previous is None or version_key(name) > version_key(previous))))
    for entry in new_inventory_changes(root, previous):
        if not re.search(rf"\b{entry['var']}\b", unreleased):
            errors.append(f"{CHANGELOG} does not name {entry['var']} under '### Inventory changes' "
                          f"(new {entry['kind']} entry in {inventory_check.MANIFEST})")
    return errors


def check_changelog_release(root: Path, version: str) -> list[str]:
    sections = changelog_sections(root)
    errors = []
    if not has_entries(sections.get(version, "")):
        errors.append(f"{CHANGELOG} has no [{version}] section with entries: run tools/release.py changelog {version} "
                      "and commit it")
    if has_entries(sections.get("Unreleased", "")):
        errors.append(f"{CHANGELOG}: [Unreleased] still has entries that {version} would leave out")
    return errors


def cmd_changelog(root: Path, args) -> int:
    if not SEMVER_RE.match(args.version):
        print(f"{args.version} is not vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    path = root / CHANGELOG
    text = path.read_text() if path.is_file() else ""
    sections = changelog_sections(root)
    if args.version in sections:
        print(f"{CHANGELOG} already has a [{args.version}] section", file=sys.stderr)
        return 2
    if not has_entries(sections.get("Unreleased", "")):
        print(f"{CHANGELOG}: [Unreleased] has no entries to release", file=sys.stderr)
        return 2
    date = args.date or __import__("datetime").date.today().isoformat()
    text = re.sub(r"^## \[Unreleased\][^\n]*\n", f"## [Unreleased]\n\n## [{args.version}] - {date}\n",
                  text, count=1, flags=re.M)
    path.write_text(text)
    print(f"{CHANGELOG}: [Unreleased] is now [{args.version}] - {date}; commit it, then run cut {args.version}")
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
    errors += check_upgrade_coverage(root)
    errors += check_inventory_changes(root)
    return errors


def roles_with_pins(root: Path) -> list[str]:
    """Roles whose defaults, vars or tasks read a pinned version from roles/versions."""
    found = []
    for role in sorted(p for p in (root / "roles").glob("*") if p.is_dir() and p.name != "versions"):
        files = [f for sub in ("defaults", "vars", "tasks") for f in (role / sub).glob("*.yml")]
        if any(VERSION_REF_RE.search(f.read_text()) for f in files):
            found.append(role.name)
    return found


def check_upgrade_coverage(root: Path) -> list[str]:
    errors: list[str] = []
    upgrade_path = root / "playbooks" / "upgrade.yml"
    upgrade = upgrade_path.read_text() if upgrade_path.is_file() else ""
    if not upgrade:
        return ["playbooks/upgrade.yml is missing"]
    scenarios = set(upgrade_scenarios(root))
    for role in roles_with_pins(root):
        if role in UPGRADE_EXEMPT:
            continue
        if role not in UPGRADE_COVERAGE:
            errors.append(f"roles/{role} reads roles/versions but has no upgrade coverage: add a play to "
                          "playbooks/upgrade.yml and an upgrade scenario (tools/release.py UPGRADE_COVERAGE), "
                          "or list it in UPGRADE_EXEMPT with a reason")
            continue
        if not re.search(rf"(role|name):\s*{re.escape(role)}\s*$", upgrade, re.M):
            errors.append(f"playbooks/upgrade.yml does not upgrade roles/{role}")
        scenario = UPGRADE_COVERAGE[role]
        if scenario is None:
            continue
        if scenario not in scenarios:
            errors.append(f"molecule/{scenario} (upgrade scenario of roles/{role}) is missing")
            continue
        converge = root / "molecule" / scenario / "converge.yml"
        if not converge.is_file() or "playbooks/upgrade.yml" not in converge.read_text():
            errors.append(f"molecule/{scenario}/converge.yml does not run playbooks/upgrade.yml")
    return errors


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in SEMVER_RE.match(tag).groups())


def cmd_cut(root: Path, args) -> int:
    """Pre-flight and notes for a release; prints the tag commands, never runs them."""
    problems: list[str] = []
    if not SEMVER_RE.match(args.version):
        print(f"{args.version} is not vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    known = tags(root)
    previous = known[-1] if known else None
    if previous is None and args.version != FIRST_RELEASE:
        problems.append(f"there is no release tag yet; the first release is {FIRST_RELEASE}")
    if previous is not None and version_key(args.version) <= version_key(previous):
        problems.append(f"{args.version} is not newer than the last release {previous}")
    if args.version in known:
        problems.append(f"tag {args.version} already exists")
    head_tags = [t for t in git(root, "tag", "--points-at", "HEAD").split() if SEMVER_RE.match(t)]
    if head_tags:
        problems.append(f"HEAD is already released as {', '.join(head_tags)}")
    if not args.allow_dirty and git(root, "status", "--porcelain").strip():
        problems.append("the working tree has uncommitted changes")
    problems += check_docs(root)
    problems += check_changelog_release(root, args.version)
    for problem in problems:
        print(f"ERROR {problem}")
    if problems:
        print(f"{args.version}: not ready to tag ({len(problems)} problem(s))")
        return 1
    notes_out = Path(args.notes_out or f"release-notes-{args.version}.md")
    notes_args = argparse.Namespace(version=args.version, since=previous)
    from contextlib import redirect_stdout
    from io import StringIO
    buf = StringIO()
    with redirect_stdout(buf):
        cmd_notes(root, notes_args)
    notes_out.write_text(buf.getvalue())
    head = git(root, "rev-parse", "--short", "HEAD").strip()
    print(f"{args.version}: ready to tag {head} ({'first release' if previous is None else 'after ' + previous}).")
    print(f"Notes written to {notes_out}; complete the recreate section, then run:")
    print(f"  git tag -s --cleanup=verbatim {args.version} -F {notes_out} {head}")
    print(f"  git push origin {args.version}")
    print("Then, in the next pull request:")
    print(f"  python3 tools/release.py bump-upgrade-from {args.version}")
    print("This command did not create a tag.")
    return 0


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
    p = sub.add_parser("cut")
    p.add_argument("version")
    p.add_argument("--notes-out")
    p.add_argument("--allow-dirty", action="store_true")
    sub.add_parser("check")
    p = sub.add_parser("changelog")
    p.add_argument("version")
    p.add_argument("--date")
    args = parser.parse_args()
    handler = {
        "has-upgrade-path": cmd_has_upgrade_path,
        "status": cmd_status,
        "notes": cmd_notes,
        "bump-upgrade-from": cmd_bump_upgrade_from,
        "check": cmd_check,
        "cut": cmd_cut,
        "changelog": cmd_changelog,
    }[args.command]
    return handler(args.root, args)


if __name__ == "__main__":
    sys.exit(main())
