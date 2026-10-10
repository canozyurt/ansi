#!/usr/bin/env python3
"""Static container policy check for every ``community.docker.docker_container`` task (HRD-02).

Every task under ``roles/*/tasks`` and ``roles/*/handlers`` that creates a container (it sets
``image:``) must:

    pinned_image     take its image from ``versions_images`` (directly, or through a role
                     variable whose default refers to ``versions_images``)
    cap_drop         drop ALL capabilities (``cap_drop: [ALL]``)
    no_new_privs     set ``security_opts: [no-new-privileges:true]``
    user             run as an explicit ``user:``
    read_only        mount the root filesystem read-only (``read_only: true``)
    memory           set a ``memory:`` limit
    pids_limit       set a ``pids_limit:``
    privileged       not set ``privileged: true``
    host_network     not use ``network_mode: host``

Tasks that only stop or remove a container (no ``image:``) and handlers that only restart an
existing container (``restart: true`` with nothing but name, image, state and comparisons) are skipped:
they do not define the container, the task that created it did.

Exceptions, each with a reason:

* inline, inside the task: ``# policy-exception: <rule>[,<rule>]: <reason>``
* central, in ``compliance/container-policy.yml`` (``exceptions:`` entries with ``rule``,
  ``reason`` and optionally ``role`` and ``task``; no role means every role).

Image signatures are not checked here: CHG-08 (roles/registry, registry-check-signature) does that.

Usage:
    tools/container_policy.py            # exit 1 on any unexcepted violation
    tools/container_policy.py --report   # also list the exceptions in use
    tools/container_policy.py --root DIR # check another tree (used for self-tests)
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MODULES = {"community.docker.docker_container", "docker_container"}
RULES = [
    "pinned_image",
    "cap_drop",
    "no_new_privs",
    "user",
    "read_only",
    "memory",
    "pids_limit",
    "privileged",
    "host_network",
]
EXCEPTION_RE = re.compile(r"#\s*policy-exception:\s*([a-z_,\s]+?)\s*:\s*(\S.*)$")
VAR_RE = re.compile(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$")


class LineLoader(yaml.SafeLoader):
    """SafeLoader that records the first and last line of every mapping."""


def _construct_mapping(loader, node, deep=False):
    mapping = yaml.SafeLoader.construct_mapping(loader, node, deep=deep)
    mapping["__lines__"] = (node.start_mark.line, node.end_mark.line)
    return mapping


LineLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


@dataclass
class Finding:
    path: str
    task: str
    rule: str
    message: str
    excepted_by: str = ""


@dataclass
class Task:
    path: Path
    role: str
    name: str
    args: dict
    lines: list[str] = field(default_factory=list)


def iter_tasks(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        for key in ("block", "rescue", "always"):
            yield from iter_tasks(item.get(key))


def truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


def load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None


def role_variables(root: Path, role: str) -> dict:
    """Variable definitions of a role: defaults, vars and set_fact keys in its tasks."""
    found: dict[str, list] = {}
    base = root / "roles" / role
    for sub in ("defaults", "vars"):
        for path in sorted((base / sub).glob("**/*.yml")):
            data = load_yaml(path)
            if isinstance(data, dict):
                for key, value in data.items():
                    found.setdefault(key, []).append(value)
    for path in sorted((base / "tasks").glob("**/*.yml")):
        data = load_yaml(path)
        if not isinstance(data, list):
            continue
        for item in iter_tasks(data):
            facts = item.get("ansible.builtin.set_fact") or item.get("set_fact")
            if isinstance(facts, dict):
                for key, value in facts.items():
                    found.setdefault(key, []).append(value)
    return found


PINNED_SOURCES = ("versions_images", "versions_tools", "versions_postgres", "versions_packages")
IDENT_RE = re.compile(r"\b([a-z][a-z0-9_]*)\b")
# A quoted image reference inside a Jinja expression, e.g. 'nginx:latest' or "docker.io/library/x".
LITERAL_IMAGE_RE = re.compile(r"""['"][\w.-]+[/:][\w./:@-]+['"]""")


def value_is_pinned(value, variables: dict, seen: frozenset = frozenset()) -> bool:
    """True when every image a value can take derives from the pinned versions (roles/versions)."""
    if isinstance(value, dict):
        if "image" in value:
            return value_is_pinned(value["image"], variables, seen)
        nested = [v for v in value.values() if isinstance(v, dict)]
        return bool(nested) and all(value_is_pinned(v, variables, seen) for v in nested)
    text = str(value)
    if any(src in text for src in PINNED_SOURCES):
        return True
    if "{{" not in text:
        return False
    expr = " ".join(re.findall(r"\{\{(.*?)\}\}", text, re.S))
    if LITERAL_IMAGE_RE.search(expr):
        return False
    refs = []
    for name in IDENT_RE.findall(expr):
        if name in variables and name not in seen:
            refs.append(name)
        elif name.endswith("_spec") and name + "s" in variables and name not in seen:
            refs.append(name + "s")
    image_refs = [r for r in refs if "image" in r or r.endswith(("_spec", "_specs"))]
    if not image_refs:
        return False
    return all(
        all(value_is_pinned(v, variables, seen | {r}) for v in variables[r]) for r in image_refs
    )


def image_is_pinned(image, variables: dict, task_vars: dict) -> bool:
    return value_is_pinned(image, {**variables, **{k: [v] for k, v in task_vars.items()}})


def load_tasks(root: Path) -> list[Task]:
    tasks = []
    files = sorted(root.glob("roles/*/tasks/**/*.yml")) + sorted(root.glob("roles/*/handlers/**/*.yml"))
    for path in files:
        text = path.read_text()
        try:
            data = yaml.load(text, Loader=LineLoader)  # noqa: S506 (SafeLoader subclass)
        except yaml.YAMLError as exc:
            print(f"WARN {path.relative_to(root)}: cannot parse ({exc})", file=sys.stderr)
            continue
        if not isinstance(data, list):
            continue
        raw = text.splitlines()
        role = path.relative_to(root).parts[1]
        for item in iter_tasks(data):
            module = next((m for m in MODULES if m in item), None)
            if not module or not isinstance(item[module], dict):
                continue
            first, last = item["__lines__"]
            tasks.append(
                Task(
                    path=path,
                    role=role,
                    name=str(item.get("name", "<unnamed>")),
                    args={**item[module], "__vars__": item.get("vars") or {}},
                    lines=raw[max(first - 1, 0) : last],
                )
            )
    return tasks


RESTART_ONLY_KEYS = {"name", "image", "state", "restart", "comparisons", "container_default_behavior", "__vars__", "__lines__"}


def is_restart_only(args: dict) -> bool:
    """A handler that restarts an existing container without redefining it (all comparisons ignored)."""
    return truthy(args.get("restart", False)) and set(args) <= RESTART_ONLY_KEYS


def check_task(task: Task, defaults: dict) -> list[tuple[str, str]]:
    args = task.args
    problems = []
    if not image_is_pinned(args.get("image"), defaults, args["__vars__"]):
        problems.append(("pinned_image", f"image {args.get('image')!r} does not come from versions_images"))
    cap_drop = args.get("cap_drop") or []
    if not isinstance(cap_drop, list) or "ALL" not in [str(c).upper() for c in cap_drop]:
        problems.append(("cap_drop", "cap_drop: [ALL] missing"))
    opts = [str(o).replace(" ", "") for o in (args.get("security_opts") or [])]
    if not any(o in ("no-new-privileges", "no-new-privileges:true") for o in opts):
        problems.append(("no_new_privs", "security_opts: [no-new-privileges:true] missing"))
    if not args.get("user"):
        problems.append(("user", "no explicit user"))
    if not truthy(args.get("read_only", False)):
        problems.append(("read_only", "read_only: true missing"))
    if not args.get("memory"):
        problems.append(("memory", "no memory limit"))
    if not args.get("pids_limit"):
        problems.append(("pids_limit", "no pids_limit"))
    if truthy(args.get("privileged", False)):
        problems.append(("privileged", "privileged: true"))
    if str(args.get("network_mode", "")).strip() == "host":
        problems.append(("host_network", "network_mode: host"))
    return problems


def inline_exceptions(task: Task) -> dict[str, str]:
    found = {}
    for line in task.lines:
        match = EXCEPTION_RE.search(line)
        if match:
            for rule in match.group(1).split(","):
                found[rule.strip()] = match.group(2).strip()
    return found


def load_central(root: Path) -> tuple[list[dict], list[str]]:
    path = root / "compliance" / "container-policy.yml"
    if not path.exists():
        return [], []
    data = yaml.safe_load(path.read_text()) or {}
    errors = []
    entries = data.get("exceptions") or []
    for i, entry in enumerate(entries):
        if entry.get("rule") not in RULES:
            errors.append(f"compliance/container-policy.yml exceptions[{i}]: unknown rule {entry.get('rule')!r}")
        if not str(entry.get("reason", "")).strip():
            errors.append(f"compliance/container-policy.yml exceptions[{i}]: reason missing")
    return entries, errors


def central_exception(entries: list[dict], task: Task, rule: str) -> str:
    for entry in entries:
        if entry.get("rule") != rule:
            continue
        if entry.get("role") and entry["role"] != task.role:
            continue
        if entry.get("task") and entry["task"] != task.name:
            continue
        return str(entry["reason"]).strip()
    return ""


def run(root: Path) -> tuple[list[Finding], list[str], int]:
    entries, errors = load_central(root)
    findings = []
    tasks = [t for t in load_tasks(root) if t.args.get("image") and not is_restart_only(t.args)]
    defaults_cache: dict[str, dict] = {}
    for task in tasks:
        defaults = defaults_cache.setdefault(task.role, role_variables(root, task.role))
        inline = inline_exceptions(task)
        for rule in inline:
            if rule not in RULES:
                errors.append(f"{task.path.relative_to(root)} ({task.name}): unknown rule {rule!r} in policy-exception")
        for rule, message in check_task(task, defaults):
            rel = str(task.path.relative_to(root))
            finding = Finding(rel, task.name, rule, message)
            if rule in inline:
                finding.excepted_by = f"inline: {inline[rule]}"
            else:
                reason = central_exception(entries, task, rule)
                if reason:
                    finding.excepted_by = f"compliance/container-policy.yml: {reason[:100]}"
            findings.append(finding)
    return findings, errors, len(tasks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--report", action="store_true", help="also list excepted findings")
    args = parser.parse_args()
    findings, errors, count = run(args.root)
    open_findings = [f for f in findings if not f.excepted_by]
    for f in open_findings:
        print(f"FAIL {f.path}: {f.task}: [{f.rule}] {f.message}")
    if args.report:
        for f in findings:
            if f.excepted_by:
                print(f"EXCEPTED {f.path}: {f.task}: [{f.rule}] {f.excepted_by}")
    for error in errors:
        print(f"ERROR {error}")
    excepted = len(findings) - len(open_findings)
    print(f"{count} container tasks checked: {len(open_findings)} violations, {excepted} excepted, {len(errors)} errors")
    return 1 if open_findings or errors else 0


if __name__ == "__main__":
    sys.exit(main())
