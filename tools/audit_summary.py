#!/usr/bin/env python3
"""Markdown summary of CIS (cis-audit) and Docker benchmark (docker-bench) summary.json files for CI.

    tools/audit_summary.py cis|docker-bench SUMMARY.json [...]
"""

import json
import sys


def main() -> int:
    kind, paths = sys.argv[1], sys.argv[2:]
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            s = json.load(fh)
        if kind == "cis":
            counts = s["counts"]
            print(f"- profile `{s['profile']}`: score {s['score_percent']}% (pass {counts['pass']}, fail {counts['fail']}, "
                  f"excepted {counts['excepted']}, not applicable {counts['notapplicable']})")
        else:
            warnings = s.get("warnings") or []
            print(f"- {s['counts']}; WARN outside the documented exceptions: {len(warnings)}")
            for warning in warnings:
                print(f"  - {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
