#!/usr/bin/env python3
"""Lint inventories so dev, test and production stay apart (CHG-04).

Layout (docs/inventories.md): one directory per environment under ``inventories/`` of the private
deployment repository, each with ``hosts.yml``, ``group_vars/`` and ``host_vars/``. Checks:

1. every inventory sets ``inventory_environment`` to one of production, staging, test or dev
   (in ``group_vars/all``, or ``all.vars`` in hosts.yml);
2. no host (inventory name or ``ansible_host``) appears in more than one inventory;
3. no non-production inventory refers to a production backup or object-storage target or restore
   source (values of variables about backup, restic, pgBackRest, S3, buckets, repositories,
   endpoints or restore), nor to a production host, so production data cannot be copied to test.

``inventories/example`` is a template, not an environment: it is checked for rule 1 only, unless
``--include-example`` is given.

Usage:
    tools/inventory_lint.py                         # every inventory under inventories/ (local try-out)
    tools/inventory_lint.py ../inventories/production ../inventories/test   # the deployment repository
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENTS = {"production", "staging", "test", "dev"}
ENV_VAR = "inventory_environment"
TARGET_KEY_RE = re.compile(r"(backup|restic|pgbackrest|s3|bucket|restore|repo|repository|snapshot|endpoint|rest_server)", re.I)
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)
URL_HOST_RE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:?#]+)", re.I)
IGNORED_VALUES = {"", "true", "false", "none", "null", "localhost", "127.0.0.1", "::1"}


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!vault", lambda loader, node: None)
Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


@dataclass
class Inventory:
    path: Path
    name: str
    hosts: dict[str, str] = field(default_factory=dict)  # name or address -> how it was found
    variables: list[tuple[str, str, str]] = field(default_factory=list)  # (file, key path, value)
    environment: str = ""
    env_sources: list[str] = field(default_factory=list)


def load_yaml(path: Path):
    text = path.read_text(errors="replace")
    if text.lstrip().startswith("$ANSIBLE_VAULT;"):
        return None
    try:
        return yaml.load(text, Loader=Loader)  # noqa: S506 (SafeLoader subclass)
    except yaml.YAMLError as exc:
        raise SystemExit(f"ERROR {path}: {exc}") from exc


def flatten(data, prefix=""):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from flatten(value, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            yield from flatten(value, f"{prefix}[{i}]")
    elif data is not None:
        yield prefix, str(data)


def walk_groups(group: dict, inv: Inventory, rel: str, group_name: str = "all") -> None:
    if not isinstance(group, dict):
        return
    for host, hvars in (group.get("hosts") or {}).items():
        inv.hosts.setdefault(str(host).lower(), f"{rel} ({group_name})")
        if isinstance(hvars, dict):
            if hvars.get("ansible_host"):
                inv.hosts.setdefault(str(hvars["ansible_host"]).lower(), f"{rel} (ansible_host of {host})")
            for key, value in flatten(hvars):
                inv.variables.append((rel, f"{host}.{key}", value))
            if ENV_VAR in hvars:
                inv.env_sources.append(f"{rel} host {host}")
    gvars = group.get("vars") or {}
    for key, value in flatten(gvars):
        inv.variables.append((rel, f"{group_name}.vars.{key}", value))
    if group_name == "all" and ENV_VAR in gvars:
        inv.environment = str(gvars[ENV_VAR])
        inv.env_sources.append(f"{rel} all.vars")
    for child, cdata in (group.get("children") or {}).items():
        walk_groups(cdata or {}, inv, rel, str(child))


def parse_ini(path: Path, inv: Inventory, rel: str) -> None:
    section = "ungrouped"
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]")
            continue
        if section.endswith(":children"):
            continue
        if section.endswith(":vars"):
            key, _, value = line.partition("=")
            inv.variables.append((rel, f"{section}.{key.strip()}", value.strip()))
            if section == "all:vars" and key.strip() == ENV_VAR:
                inv.environment = value.strip()
                inv.env_sources.append(f"{rel} [all:vars]")
            continue
        parts = line.split()
        inv.hosts.setdefault(parts[0].lower(), f"{rel} ({section})")
        for part in parts[1:]:
            key, _, value = part.partition("=")
            inv.variables.append((rel, f"{parts[0]}.{key}", value))
            if key == "ansible_host":
                inv.hosts.setdefault(value.lower(), f"{rel} (ansible_host of {parts[0]})")


def load_inventory(path: Path) -> Inventory:
    inv = Inventory(path=path, name=path.name)
    for hosts_file in sorted(path.glob("hosts*")) + sorted(path.glob("inventory*")):
        rel = str(hosts_file.relative_to(path))
        if hosts_file.suffix in (".yml", ".yaml"):
            data = load_yaml(hosts_file) or {}
            walk_groups(data.get("all") or {}, inv, rel)
        elif hosts_file.is_file():
            parse_ini(hosts_file, inv, rel)
    for sub in ("group_vars", "host_vars"):
        for var_file in sorted((path / sub).rglob("*")):
            if not var_file.is_file() or var_file.suffix not in (".yml", ".yaml", ""):
                continue
            rel = str(var_file.relative_to(path))
            data = load_yaml(var_file)
            if not isinstance(data, dict):
                continue
            for key, value in flatten(data):
                inv.variables.append((rel, key, value))
            parts = var_file.relative_to(path / sub).parts
            if sub == "group_vars" and parts[0] in ("all", "all.yml", "all.yaml") and ENV_VAR in data:
                inv.environment = str(data[ENV_VAR])
                inv.env_sources.append(rel)
            elif ENV_VAR in data:
                inv.env_sources.append(rel)
    return inv


def target_values(inv: Inventory) -> dict[str, str]:
    """Backup/object-storage targets and restore sources a production inventory declares."""
    found = {}
    for rel, key, value in inv.variables:
        leaf = key.rsplit(".", 1)[-1]
        if not TARGET_KEY_RE.search(leaf) or "{{" in value or "{%" in value:
            continue
        for candidate in candidates(value):
            found.setdefault(candidate, f"{rel}: {key}")
    return found


def candidates(value: str) -> set[str]:
    """Hostnames, URLs and bucket-like names inside a variable value."""
    value = value.strip().strip("'\"")
    out = set()
    if value.lower() in IGNORED_VALUES or value.isdigit():
        return out
    url = URL_HOST_RE.match(value)
    if url:
        out.add(value.rstrip("/").lower())
        out.add(url.group(1).lower())
        return out
    host = value.rsplit(":", 1)[0] if re.search(r":\d+$", value) else value
    if HOST_RE.match(host):
        out.add(host.lower())
    elif re.match(r"^[a-z0-9][a-z0-9.-]{2,62}$", value) and "-" in value:
        out.add(value.lower())  # bucket or repository names such as prod-backups
    return out


def references(value: str, needle: str) -> bool:
    return re.search(rf"(?<![a-z0-9.-]){re.escape(needle)}(?![a-z0-9-])", value.lower()) is not None


def lint(inventories: list[Inventory], cross: list[Inventory]) -> list[str]:
    errors = []
    for inv in inventories:
        if not inv.environment:
            errors.append(f"{inv.path}: {ENV_VAR} is not set in group_vars/all (one of {', '.join(sorted(ENVIRONMENTS))})")
        elif inv.environment not in ENVIRONMENTS:
            errors.append(f"{inv.path}: {ENV_VAR} is {inv.environment!r}; use one of {', '.join(sorted(ENVIRONMENTS))}")
        extra = [s for s in inv.env_sources if not (s.startswith("group_vars/all") or s.endswith("all.vars") or "[all:vars]" in s)]
        if extra:
            errors.append(f"{inv.path}: {ENV_VAR} must only be set for all hosts, not in {', '.join(extra)}")
    seen: dict[str, tuple[str, str]] = {}
    for inv in cross:
        for host, where in inv.hosts.items():
            if host in seen and seen[host][0] != inv.name:
                errors.append(f"host {host} is in inventory {seen[host][0]} ({seen[host][1]}) and {inv.name} ({where})")
            else:
                seen.setdefault(host, (inv.name, where))
    production = [i for i in cross if i.environment == "production"]
    for prod in production:
        targets = target_values(prod)
        prod_hosts = set(prod.hosts)
        for other in cross:
            if other.environment in ("production", ""):
                continue
            for rel, key, value in other.variables:
                for target, source in targets.items():
                    if references(value, target):
                        errors.append(
                            f"{other.name} ({other.environment}) {rel}: {key} uses production target {target!r} "
                            f"from {prod.name} {source}"
                        )
                for host in prod_hosts:
                    if references(value, host):
                        errors.append(f"{other.name} ({other.environment}) {rel}: {key} refers to production host {host}")
    return sorted(set(errors))


def discover(root: Path) -> list[Path]:
    base = root / "inventories"
    return sorted(p for p in base.iterdir() if p.is_dir() and (any(p.glob("hosts*")) or (p / "group_vars").is_dir()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inventories", nargs="*", type=Path)
    parser.add_argument("--include-example", action="store_true", help="treat inventories/example as an environment")
    args = parser.parse_args()
    paths = [p.resolve() for p in args.inventories] or discover(ROOT)
    inventories = [load_inventory(p) for p in paths]
    example = (ROOT / "inventories" / "example").resolve()
    cross = [i for i in inventories if args.include_example or i.path.resolve() != example]
    errors = lint(inventories, cross)
    for error in errors:
        print(f"ERROR {error}")
    summary = ", ".join(f"{i.name}={i.environment or '?'}" for i in inventories)
    print(f"{len(inventories)} inventories ({summary}): {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
