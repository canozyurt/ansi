#!/usr/bin/env python3
"""Summarize the run record of a check-mode run of site.yml as drift (CHG-03).

    tools/drift_report.py RECORD.json [--summary FILE]

Prints one line per host with the tasks the run would change (drift) or that failed, appends a
Markdown table to FILE (e.g. $GITHUB_STEP_SUMMARY) and exits 1 when any host drifted, failed or was
unreachable, 2 when the record is not a check-mode run of site.yml. The per-host drift metric and
the DriftDetected alert come from playbooks/run_record.yml; this is the CI side of the same record.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def analyse(record: dict) -> tuple[list[tuple[str, dict]], bool]:
    hosts = sorted((record.get("hosts") or {}).items())
    bad = [(h, s) for h, s in hosts if s.get("changed") or s.get("failures") or s.get("unreachable")]
    return bad, bool(bad)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("record")
    parser.add_argument("--summary")
    args = parser.parse_args()
    record = json.loads(Path(args.record).read_text())
    what = record.get("what") or {}
    if not what.get("check_mode") or Path(str(what.get("playbook", ""))).name != "site.yml":
        print("not a check-mode run of site.yml", file=sys.stderr)
        return 2
    bad, drift = analyse(record)
    total = len(record.get("hosts") or {})
    lines = [f"Drift check of {total} hosts at commit {str(what.get('commit', '?'))[:12]}: "
             f"{len(bad)} with drift, failures or unreachable"]
    for host, stats in bad:
        lines.append(f"  {host}: changed={stats.get('changed', 0)} failed={stats.get('failures', 0)} "
                     f"unreachable={stats.get('unreachable', 0)}")
    print("\n".join(lines))
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write("## Drift (check-mode run of site.yml)\n\n" + lines[0] + "\n\n")
            if bad:
                fh.write("| Host | Would change | Failed | Unreachable |\n| --- | --- | --- | --- |\n")
                for host, stats in bad:
                    fh.write(f"| {host} | {stats.get('changed', 0)} | {stats.get('failures', 0)} | "
                             f"{stats.get('unreachable', 0)} |\n")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
