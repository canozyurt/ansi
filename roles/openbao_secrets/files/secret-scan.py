#!/usr/bin/env python3
"""Look for known secret values in files on disk (SEC-02).

Reads a JSON request on stdin, so the values never appear on a command line:
    {"values": {"<service>/<key>": "<value>", ...}, "paths": ["/etc", "/opt"],
     "exclude": ["/etc/openbao-agent"], "min_length": 12, "max_bytes": 10485760}
Prints a JSON report with the files that contain a value (service/key and path, never the value) and
exits 1 when anything is found. Files on tmpfs mounts are skipped: that is where secrets belong.
"""
import json
import os
import sys


def tmpfs_mounts():
    mounts = []
    try:
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 3 and parts[2] in ("tmpfs", "ramfs"):
                    mounts.append(parts[1])
    except OSError:
        pass
    return mounts


def under(path, prefixes):
    return any(path == p or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def scan(req):
    min_len = int(req.get("min_length", 12))
    max_bytes = int(req.get("max_bytes", 10 * 1024 * 1024))
    needles = {name: value.encode() for name, value in req["values"].items() if value and len(value) >= min_len}
    skip = tmpfs_mounts() + list(req.get("exclude", []))
    findings, scanned = [], 0
    for root in req.get("paths", ["/etc", "/opt"]):
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not under(os.path.join(dirpath, d), skip)]
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                if under(path, skip) or os.path.islink(path):
                    continue
                try:
                    if not os.path.isfile(path) or os.path.getsize(path) > max_bytes:
                        continue
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                scanned += 1
                for name, needle in needles.items():
                    if needle in data:
                        findings.append({"path": path, "secret": name})
    return {"files_scanned": scanned, "secrets_checked": len(needles), "findings": findings}


def main():
    report = scan(json.load(sys.stdin))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
