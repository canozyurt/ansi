#!/usr/bin/env python3
"""Check an inventory against the variable changes of a release, before upgrading to it.

Reads inventory-changes.yml (docs/upgrades.md#inventory-changes-between-releases) from the working
tree or from a release tag, and reports for the inventory:

- ERROR a variable that was removed, or renamed and then removed, is still set (its value would be
  ignored);
- ERROR both the old and the new name of a renamed variable are set;
- ERROR a required variable is not set for a host in its groups;
- WARN  a deprecated (renamed) name is still set: it keeps working until its removal release;
- WARN  a required variable cannot be verified because the host's vars file is vault-encrypted;
- NOTE  a default changed and the inventory does not set the variable, so the new default applies.

Each line names the release that made the change. The checks look at variable names only, so they
work on vault-encrypted values (``!vault``); whole-file encrypted vars files hide their names.

Usage:
    tools/inventory_check.py ../infra-inventories/production                    # this checkout
    tools/inventory_check.py ../infra-inventories/production --release v1.4.0   # a tag, before checkout
    tools/inventory_check.py inv/prod inv/staging --strict                      # warnings fail too

Exit status: 0 no errors (warnings and notes allowed unless --strict), 1 errors, 2 usage problems.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "inventory-changes.yml"
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VAR_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
KINDS = {
    "renamed": {"var", "new", "note", "removal_planned"},
    "removed": {"var", "note"},
    "required": {"var", "note", "groups"},
    "default": {"var", "note", "old", "new"},
}
REQUIRED_FIELDS = {"renamed": {"new"}, "default": {"old", "new"}}


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!vault", lambda loader, node: None)
Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


# ---------------------------------------------------------------- manifest

def git(root: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, check=check)


def release_tags(root: Path) -> list[str]:
    out = git(root, "tag", "--list", "v*", check=False).stdout.split()
    return sorted((t for t in out if SEMVER_RE.match(t)), key=version_key)


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in SEMVER_RE.match(tag).groups())


def load_changes(root: Path, ref: str | None = None) -> list[dict]:
    """Entries of inventory-changes.yml in the working tree, or at `ref` (empty before the file existed)."""
    if ref is None:
        path = root / MANIFEST
        text = path.read_text() if path.is_file() else ""
    else:
        shown = git(root, "show", f"{ref}:{MANIFEST}", check=False)
        if shown.returncode != 0:
            if git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).returncode != 0:
                raise LookupError(f"{ref}: no such tag or commit (git fetch --tags?)")
            return []
        text = shown.stdout
    data = yaml.safe_load(text) or {}
    return list(data.get("changes") or [])


def entry_key(entry: dict) -> tuple[str, str]:
    return str(entry.get("kind")), str(entry.get("var"))


def validate(changes: list[dict]) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for i, entry in enumerate(changes):
        where = f"{MANIFEST} changes[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: not a mapping")
            continue
        kind = entry.get("kind")
        if kind not in KINDS:
            errors.append(f"{where}: kind must be one of {', '.join(sorted(KINDS))}")
            continue
        unknown = set(entry) - KINDS[kind] - {"kind"}
        if unknown:
            errors.append(f"{where}: unknown field(s) for {kind}: {', '.join(sorted(unknown))}")
        missing = ({"var", "note"} | REQUIRED_FIELDS.get(kind, set())) - set(entry)
        if missing:
            errors.append(f"{where}: {kind} needs {', '.join(sorted(missing))}")
        for name_field in ("var",) + (("new",) if kind == "renamed" else ()):
            if name_field in entry and not VAR_RE.match(str(entry[name_field])):
                errors.append(f"{where}: {name_field} {entry[name_field]!r} is not a variable name")
        if kind == "renamed" and "removal_planned" in entry and not SEMVER_RE.match(str(entry["removal_planned"])):
            errors.append(f"{where}: removal_planned must be vMAJOR.MINOR.PATCH")
        if kind == "required" and "groups" in entry and not (
                isinstance(entry["groups"], list) and all(isinstance(g, str) for g in entry["groups"])):
            errors.append(f"{where}: groups must be a list of group names")
        if entry_key(entry) in seen:
            errors.append(f"{where}: second {kind} entry for {entry.get('var')}")
        seen.add(entry_key(entry))
    return errors


def introduced_in(root: Path, target: str | None) -> dict[tuple[str, str], str]:
    """(kind, var) -> first release tag (up to `target`) whose manifest holds the entry."""
    found: dict[tuple[str, str], str] = {}
    for tag in release_tags(root):
        if target and SEMVER_RE.match(target) and version_key(tag) > version_key(target):
            break
        for entry in load_changes(root, tag):
            found.setdefault(entry_key(entry), tag)
    return found


# ---------------------------------------------------------------- inventory

@dataclass
class Scope:
    source: str
    names: set[str] = field(default_factory=set)
    encrypted: bool = False


@dataclass
class Inventory:
    path: Path
    host_groups: dict[str, set[str]] = field(default_factory=dict)
    group_scopes: dict[str, list[Scope]] = field(default_factory=dict)
    host_scopes: dict[str, list[Scope]] = field(default_factory=dict)

    def scopes(self, host: str) -> list[Scope]:
        out = list(self.group_scopes.get("all", []))
        for group in sorted(self.host_groups[host] - {"all"}):
            out += self.group_scopes.get(group, [])
        return out + self.host_scopes.get(host, [])

    def defined(self, host: str, var: str) -> list[str]:
        return [s.source for s in self.scopes(host) if var in s.names]

    def encrypted(self, host: str) -> list[str]:
        return [s.source for s in self.scopes(host) if s.encrypted]


def read_vars(path: Path, rel: str) -> Scope:
    text = path.read_text(errors="replace")
    if text.lstrip().startswith("$ANSIBLE_VAULT;"):
        return Scope(rel, encrypted=True)
    try:
        data = yaml.load(text, Loader=Loader)  # noqa: S506 (SafeLoader subclass)
    except yaml.YAMLError as exc:
        raise SystemExit(f"ERROR {path}: {exc}") from exc
    return Scope(rel, set(data) if isinstance(data, dict) else set())


def vars_dir_scopes(base: Path, inv_root: Path, name: str) -> list[Scope]:
    scopes = []
    for candidate in (base / f"{name}.yml", base / f"{name}.yaml", base / name):
        if candidate.is_file():
            scopes.append(read_vars(candidate, str(candidate.relative_to(inv_root))))
        elif candidate.is_dir():
            for path in sorted(candidate.rglob("*")):
                if path.is_file() and path.suffix in (".yml", ".yaml", ""):
                    scopes.append(read_vars(path, str(path.relative_to(inv_root))))
    return scopes


def load_inventory(path: Path) -> Inventory:
    hosts_file = path if path.is_file() else next(
        (path / n for n in ("hosts.yml", "hosts.yaml") if (path / n).is_file()), None)
    if hosts_file is None:
        raise SystemExit(f"{path}: no hosts.yml (only YAML inventories are supported)")
    inv_root = hosts_file.parent
    inv = Inventory(inv_root)
    data = yaml.load(hosts_file.read_text(), Loader=Loader) or {}  # noqa: S506
    rel = hosts_file.name

    def walk(name: str, group: dict, parents: tuple[str, ...]) -> None:
        group = group or {}
        chain = parents + (name,)
        if group.get("vars"):
            inv.group_scopes.setdefault(name, []).append(Scope(f"{rel} ({name}.vars)", set(group["vars"])))
        for host, hvars in (group.get("hosts") or {}).items():
            inv.host_groups.setdefault(host, set()).update(chain)
            if hvars:
                inv.host_scopes.setdefault(host, []).append(Scope(f"{rel} ({host})", set(hvars)))
        for child, body in (group.get("children") or {}).items():
            walk(child, body, chain)

    for name, body in data.items():
        walk(name, body, ("all",) if name != "all" else ())
    groups = {g for gs in inv.host_groups.values() for g in gs} | {"all"}
    for group in groups:
        inv.group_scopes.setdefault(group, []).extend(vars_dir_scopes(inv_root / "group_vars", inv_root, group))
    for host in inv.host_groups:
        inv.host_scopes.setdefault(host, []).extend(vars_dir_scopes(inv_root / "host_vars", inv_root, host))
    return inv


# ---------------------------------------------------------------- checks

@dataclass
class Finding:
    level: str  # ERROR, WARN, NOTE
    var: str
    message: str
    entry: dict


def hosts_with(inv: Inventory, var: str) -> dict[str, list[str]]:
    return {h: src for h in sorted(inv.host_groups) if (src := inv.defined(h, var))}


def sources(found: dict[str, list[str]]) -> str:
    return ", ".join(sorted({s for srcs in found.values() for s in srcs}))


def evaluate(changes: list[dict], inv: Inventory) -> list[Finding]:
    findings: list[Finding] = []
    removed = {e["var"]: e for e in changes if e.get("kind") == "removed"}
    renamed_to = {e["new"]: e for e in changes if e.get("kind") == "renamed" and e["var"] not in removed}
    for entry in changes:
        kind, var, note = entry["kind"], entry["var"], entry.get("note", "").strip()
        if kind == "renamed":
            found = hosts_with(inv, var)
            if not found or var in removed:
                continue  # the removed entry reports it
            both = {h: s for h, s in found.items() if inv.defined(h, entry["new"])}
            if both:
                findings.append(Finding("ERROR", var, (
                    f"renamed to {entry['new']}, and both names are set ({sources(both)}); the old one is "
                    f"ignored. Delete {var}. {note}"), entry))
            else:
                until = entry.get("removal_planned", "a later major release")
                findings.append(Finding("WARN", var, (
                    f"deprecated, renamed to {entry['new']} ({sources(found)}). It still works until "
                    f"{until}. {note}"), entry))
        elif kind == "removed":
            found = hosts_with(inv, var)
            if found:
                findings.append(Finding("ERROR", var, f"removed; its value is ignored ({sources(found)}). {note}", entry))
        elif kind == "required":
            groups = set(entry.get("groups") or ["all"])
            alias = renamed_to.get(var)
            missing, unknown = [], []
            for host in sorted(h for h, gs in inv.host_groups.items() if gs & groups):
                if inv.defined(host, var) or (alias and inv.defined(host, alias["var"])):
                    continue
                (unknown if inv.encrypted(host) else missing).append(host)
            if missing:
                findings.append(Finding("ERROR", var, (
                    f"required for {', '.join(sorted(groups))} but not set for {len(missing)} host(s): "
                    f"{', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}. {note}"), entry))
            if unknown:
                findings.append(Finding("WARN", var, (
                    f"required for {', '.join(sorted(groups))}; cannot tell whether {len(unknown)} host(s) "
                    f"set it in a vault-encrypted vars file ({', '.join(unknown[:5])}). {note}"), entry))
        elif kind == "default":
            if not hosts_with(inv, var):
                findings.append(Finding("NOTE", var, (
                    f"default changed from {entry['old']!r} to {entry['new']!r}; this inventory does not set "
                    f"it, so the new default applies. Set {var}: {entry['old']!r} to keep the old behaviour. "
                    f"{note}"), entry))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("inventories", nargs="+", type=Path, help="inventory directories (or hosts.yml)")
    parser.add_argument("--release", help="release tag to check against (default: this checkout)")
    parser.add_argument("--strict", action="store_true", help="warnings fail too")
    parser.add_argument("--root", default=ROOT, type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.release and not SEMVER_RE.match(args.release):
        print(f"{args.release} is not vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    try:
        changes = load_changes(args.root, args.release)
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2
    problems = validate(changes)
    if problems:
        for problem in problems:
            print(f"ERROR {problem}", file=sys.stderr)
        return 2
    target = args.release or "this checkout"
    since = introduced_in(args.root, args.release)
    failed = False
    for path in args.inventories:
        inv = load_inventory(path)
        findings = evaluate(changes, inv)
        for f in findings:
            print(f"{f.level:<5} {path}: [{since.get(entry_key(f.entry), 'unreleased')}] {f.var}: {f.message}".rstrip())
        errors = sum(f.level == "ERROR" for f in findings)
        warnings = sum(f.level == "WARN" for f in findings)
        print(f"{path}: {errors} error(s), {warnings} warning(s) against {target}")
        failed |= errors > 0 or (args.strict and warnings > 0)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
