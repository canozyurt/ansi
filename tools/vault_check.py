#!/usr/bin/env python3
"""Fail on unencrypted Ansible Vault variables (SEC-01).

Convention: secrets live in variables named ``vault_*`` (inventories reference them as
``{{ vault_x }}``). Outside ``inventories/example`` every ``vault_*`` value must be either

* inside a file encrypted as a whole (``$ANSIBLE_VAULT;`` header), or
* an inline ``!vault |`` encrypted string, or
* a lookup that fetches it at run time (``{{ lookup(...) }}`` / ``{{ query(...) }}``, e.g. OpenBao),
* or a pure Jinja reference to another variable (checked where that one is defined), or empty.

Files named ``*vault*.yml`` (e.g. ``group_vars/all/vault.yml``) must be encrypted as a whole.

Scanned by default: every YAML file in the repository except ``inventories/example``, ``.git``,
collections and caches. Pass paths to scan a private inventory repository instead:

    tools/vault_check.py                       # this repository
    tools/vault_check.py ../infra-inventories  # an operator's inventory checkout
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", "collections", ".cache", ".ansible", ".venv", "node_modules"}
EXAMPLE = Path("inventories") / "example"
VAULT_HEADER = "$ANSIBLE_VAULT;"
LOOKUP_RE = re.compile(r"^\s*\{\{\s*(lookup|query|q)\s*\(", re.S)
# A value that is only a Jinja expression refers to another variable; that variable is checked where defined.
TEMPLATE_RE = re.compile(r"^\s*\{\{.*\}\}\s*$", re.S)


class VaultTagged(str):
    """A value that carried the !vault tag."""


class Loader(yaml.SafeLoader):
    pass


def _vault(loader, node):
    return VaultTagged(loader.construct_scalar(node))


Loader.add_constructor("!vault", _vault)
Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def is_protected(value) -> bool:
    if isinstance(value, VaultTagged):
        return True
    if isinstance(value, str):
        return (
            not value.strip()
            or bool(LOOKUP_RE.match(value) or TEMPLATE_RE.match(value))
            or value.lstrip().startswith(VAULT_HEADER)
        )
    if value is None or isinstance(value, bool):
        return True  # nothing secret in an empty or boolean value
    if isinstance(value, dict):
        return all(is_protected(v) for v in value.values())
    if isinstance(value, list):
        return all(is_protected(v) for v in value)
    return False  # numbers and other scalars in plain text


def walk(data, path=""):
    if isinstance(data, dict):
        for key, value in data.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).startswith("vault_"):
                yield here, value
            else:
                yield from walk(value, here)
    elif isinstance(data, list):
        for i, value in enumerate(data):
            yield from walk(value, f"{path}[{i}]")


def candidate_files(paths: list[Path], root: Path) -> list[Path]:
    files = []
    for base in paths:
        if base.is_file():
            files.append(base)
            continue
        for path in base.rglob("*"):
            if path.suffix not in (".yml", ".yaml") or not path.is_file():
                continue
            rel = path.relative_to(root) if path.is_relative_to(root) else path
            if SKIP_PARTS & set(rel.parts):
                continue
            if path.is_relative_to(root) and rel.is_relative_to(EXAMPLE):
                continue
            files.append(path)
    return sorted(files)


def check_file(path: Path) -> list[str]:
    text = path.read_text(errors="replace")
    if text.lstrip().startswith(VAULT_HEADER):
        return []
    problems = []
    if "vault" in path.stem.lower() and path.parent.name not in ("tasks", "handlers", "defaults", "templates"):
        if path.parent.parent.name in ("group_vars", "host_vars") or path.parent.name in ("group_vars", "host_vars"):
            problems.append("vault file is not encrypted (ansible-vault encrypt)")
    if "vault_" not in text:
        return problems
    try:
        documents = list(yaml.load_all(text, Loader=Loader))  # noqa: S506 (SafeLoader subclass)
    except yaml.YAMLError:
        return problems
    for data in documents:
        for key, value in walk(data):
            if not is_protected(value):
                problems.append(f"{key} is not encrypted (use ansible-vault encrypt_string or an OpenBao lookup)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    paths = [p.resolve() for p in args.paths] or [ROOT]
    failures = 0
    files = candidate_files(paths, ROOT)
    for path in files:
        for problem in check_file(path):
            failures += 1
            shown = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
            print(f"FAIL {shown}: {problem}")
    print(f"{len(files)} YAML files checked: {failures} unencrypted vault values")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
