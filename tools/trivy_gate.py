#!/usr/bin/env python3
"""Gate Trivy image reports against compliance/vuln-waivers.yml (CHG-05, SEC-01).

Reads Trivy JSON reports (``trivy image --format json --scanners vuln,secret``) and fails when:

* a vulnerability at a blocking severity (default CRITICAL) has no valid waiver;
* a secret is found in an image layer and has no valid waiver (SEC-01);
* a waiver is malformed (no id, reason or expiry) or has expired.

A waiver matches on ``id`` (CVE/GHSA id or Trivy secret rule id) and, when given, ``image``
(repository with or without tag; matched as a prefix of the scanned image) and ``package``.

Usage:
    tools/trivy_gate.py reports/*.json [--waivers compliance/vuln-waivers.yml] [--severity CRITICAL]
                                       [--today YYYY-MM-DD] [--summary out.md]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MAX_WAIVER_DAYS = 180


def load_waivers(path: Path, today: dt.date) -> tuple[list[dict], list[str], list[str]]:
    errors, warnings, valid = [], [], []
    if not path.exists():
        return valid, errors, warnings
    data = yaml.safe_load(path.read_text()) or {}
    for i, waiver in enumerate(data.get("waivers") or []):
        label = f"{path.name} waivers[{i}] ({waiver.get('id', '?')})"
        missing = [f for f in ("id", "reason", "expires") if not str(waiver.get(f, "")).strip()]
        if missing:
            errors.append(f"{label}: missing {', '.join(missing)}")
            continue
        expires = waiver["expires"]
        if not isinstance(expires, dt.date):
            try:
                expires = dt.date.fromisoformat(str(expires))
            except ValueError:
                errors.append(f"{label}: expires {waiver['expires']!r} is not a date (YYYY-MM-DD)")
                continue
        if expires < today:
            errors.append(f"{label}: expired on {expires}; fix the finding or renew the waiver with a new reason")
            continue
        if (expires - today).days > MAX_WAIVER_DAYS:
            warnings.append(f"{label}: expires in more than {MAX_WAIVER_DAYS} days ({expires})")
        valid.append({**waiver, "expires": expires})
    return valid, errors, warnings


def waived(waivers: list[dict], finding_id: str, image: str, package: str) -> dict | None:
    for waiver in waivers:
        if str(waiver["id"]) != finding_id:
            continue
        if waiver.get("image") and not image.startswith(str(waiver["image"])):
            continue
        if waiver.get("package") and str(waiver["package"]) != package:
            continue
        return waiver
    return None


def gate(reports: list[Path], waivers: list[dict], severities: set[str]) -> tuple[list[str], list[str], int]:
    failures, accepted, total = [], [], 0
    for path in reports:
        data = json.loads(path.read_text() or "{}")
        image = str(data.get("ArtifactName", path.stem))
        for result in data.get("Results") or []:
            for vuln in result.get("Vulnerabilities") or []:
                if str(vuln.get("Severity", "")).upper() not in severities:
                    continue
                total += 1
                vid, pkg = str(vuln.get("VulnerabilityID")), str(vuln.get("PkgName", ""))
                line = f"{image}: {vid} in {pkg} {vuln.get('InstalledVersion', '')} ({vuln.get('Severity')})"
                waiver = waived(waivers, vid, image, pkg)
                if waiver:
                    accepted.append(f"{line} waived until {waiver['expires']}: {waiver['reason']}")
                else:
                    fixed = vuln.get("FixedVersion")
                    failures.append(line + (f", fixed in {fixed}" if fixed else ", no fix yet"))
            for secret in result.get("Secrets") or []:
                total += 1
                rid = str(secret.get("RuleID"))
                line = f"{image}: secret {rid} ({secret.get('Title', '')}) in {result.get('Target', '?')}"
                waiver = waived(waivers, rid, image, str(result.get("Target", "")))
                if waiver:
                    accepted.append(f"{line} waived until {waiver['expires']}: {waiver['reason']}")
                else:
                    failures.append(line)
    return failures, accepted, total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--waivers", type=Path, default=ROOT / "compliance" / "vuln-waivers.yml")
    parser.add_argument("--severity", default="CRITICAL", help="comma-separated blocking severities")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    parser.add_argument("--summary", type=Path, help="append a Markdown summary to this file")
    args = parser.parse_args()
    waivers, errors, warnings = load_waivers(args.waivers, args.today)
    failures, accepted, total = gate(args.reports, waivers, {s.strip().upper() for s in args.severity.split(",")})
    for line in warnings:
        print(f"WARN  {line}")
    for line in accepted:
        print(f"WAIVED {line}")
    for line in failures:
        print(f"FAIL  {line}")
    for line in errors:
        print(f"ERROR {line}")
    print(f"{len(args.reports)} reports, {total} blocking findings: {len(failures)} open, {len(accepted)} waived; {len(errors)} waiver errors")
    if args.summary:
        with args.summary.open("a") as fh:
            fh.write(f"## Image scan ({args.severity} and secrets)\n\n")
            fh.write(f"{len(args.reports)} images, {len(failures)} open, {len(accepted)} waived, {len(errors)} waiver errors\n\n")
            for line in failures + errors:
                fh.write(f"- {line}\n")
    return 1 if failures or errors else 0


if __name__ == "__main__":
    sys.exit(main())
