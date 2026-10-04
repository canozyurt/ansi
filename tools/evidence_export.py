#!/usr/bin/env python3
"""Export the compliance checklist with its evidence as JSON or CSV.

One record per item of ``compliance/checklist.yml``: status, progress, framework controls, and the
files and tasks that evidence it (tasks tagged ``chk_<domain>_<nn>`` under roles/ and playbooks/,
and Molecule verify tasks with the same tag). Feed it to the GRC tool (CISO Assistant, see
docs/grc.md) or hand it to an auditor next to the CI run that produced it.

The checklist parsing and tag scanning come from tools/checklist.py, so both tools always agree.

Usage:
    tools/evidence_export.py --format json [--output FILE]
    tools/evidence_export.py --format csv  [--output FILE]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checklist  # noqa: E402  (tools/checklist.py, reused as a library)

ROOT = checklist.ROOT
TASK_KEYS = ("block", "rescue", "always", "tasks", "pre_tasks", "post_tasks", "handlers")
CSV_FIELDS = [
    "id", "domain", "status", "evidence", "requirement", "check", "progress",
    "soc2", "iso27001", "iso27017", "project_rule",
    "implemented_files", "implemented_tasks", "verified_files", "verified_tasks",
]


def _tags(node: dict) -> list[str]:
    tags = node.get("tags") or []
    return [tags] if isinstance(tags, str) else [str(t) for t in tags]


def _walk(nodes, inherited: list[str], found: dict[str, list[str]], prefix: str = "") -> None:
    """Collect task names per checklist ID, with tags inherited from blocks and plays."""
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        tags = inherited + _tags(node)
        name = str(node.get("name") or "(unnamed)")
        nested = [k for k in TASK_KEYS if isinstance(node.get(k), list)]
        if not nested:
            for tag in tags:
                match = checklist.TAG_RE.fullmatch(tag)
                if match:
                    found.setdefault(checklist.tag_to_id(match), []).append(prefix + name)
        for key in nested:
            _walk(node[key], tags, found, prefix + name + " > " if "hosts" not in node else "")


def task_names(paths: set[str]) -> dict[str, dict[str, list[str]]]:
    """Map checklist ID -> file -> task names carrying its tag."""
    result: dict[str, dict[str, list[str]]] = {}
    for rel in sorted(paths):
        try:
            data = yaml.safe_load((ROOT / rel).read_text())
        except (OSError, yaml.YAMLError):
            continue
        found: dict[str, list[str]] = {}
        _walk(data if isinstance(data, list) else [], [], found)
        for rid, names in found.items():
            result.setdefault(rid, {})[rel] = sorted(set(names))
    return result


def git_revision() -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def build_records(data: dict) -> list[dict]:
    implemented, verified = checklist.collect_evidence()
    all_files = set().union(*implemented.values(), *verified.values()) if (implemented or verified) else set()
    names = task_names(all_files)
    domains = data.get("domains") or {}
    records = []
    for item in data.get("requirements") or []:
        rid = item["id"]
        controls = item.get("controls") or {}
        impl_files = sorted(implemented.get(rid, set()))
        ver_files = sorted(verified.get(rid, set()))
        records.append({
            "id": rid,
            "domain": rid.split("-")[0],
            "domain_name": domains.get(rid.split("-")[0], ""),
            "requirement": item.get("requirement", ""),
            "check": item.get("check", ""),
            "status": item.get("status", ""),
            "progress": item.get("progress", ""),
            "evidence": checklist.evidence(rid, implemented, verified),
            "project_rule": bool(item.get("project_rule", False)),
            "controls": {key: [str(c) for c in controls.get(key) or []] for key in checklist.FRAMEWORKS},
            "implemented_by": [{"file": f, "tasks": names.get(rid, {}).get(f, [])} for f in impl_files],
            "verified_by": [{"file": f, "tasks": names.get(rid, {}).get(f, [])} for f in ver_files],
        })
    return records


def to_csv(records: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for rec in records:
        writer.writerow({
            **{k: rec[k] for k in ("id", "domain", "status", "evidence", "requirement", "check", "progress")},
            **{k: "; ".join(rec["controls"][k]) for k in checklist.FRAMEWORKS},
            "project_rule": str(rec["project_rule"]).lower(),
            "implemented_files": "; ".join(e["file"] for e in rec["implemented_by"]),
            "implemented_tasks": "; ".join(f"{e['file']}: {t}" for e in rec["implemented_by"] for t in e["tasks"]),
            "verified_files": "; ".join(e["file"] for e in rec["verified_by"]),
            "verified_tasks": "; ".join(f"{e['file']}: {t}" for e in rec["verified_by"] for t in e["tasks"]),
        })
    return buf.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--output", "-o", help="write to this file instead of stdout")
    args = parser.parse_args()

    data = checklist.load_checklist()
    errors = checklist.validate(data)
    if errors:
        for error in errors:
            print(f"ERROR {error}", file=sys.stderr)
        return 1
    records = build_records(data)
    if args.format == "json":
        text = json.dumps({
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": str(checklist.CHECKLIST.relative_to(ROOT)),
            "revision": git_revision(),
            "frameworks": checklist.FRAMEWORKS,
            "items": records,
        }, indent=2) + "\n"
    else:
        text = to_csv(records)
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
