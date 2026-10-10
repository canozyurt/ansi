#!/usr/bin/env python3
"""Mirror, sign and attach SBOMs for every image in roles/versions (CHG-08).

For each image <registry>/<path>:<tag>[@sha256:...] in versions_images:
  1. copy it (all platforms) to <mirror>/<registry>/<path>:<tag> with crane, the layout roles/versions
     rewrites references to when image_registry_mirror is set;
  2. when the pin carries a digest, refuse a mirrored digest that differs from it;
  3. generate an SPDX SBOM with syft and attach it as a signed cosign attestation (type spdxjson);
  4. sign the mirrored digest with cosign (key from OpenBao transit through hashivault://, or a CI secret),
     without the public transparency log (internal registry; Zot verifies against its trust store).
Images already signed with this key are not signed again. Zot scans every mirrored image with Trivy
(roles/registry); registry-check-signature refuses unsigned images when image_signature_policy is enforce.

Environment:
  IMAGE_MIRROR       mirror prefix, e.g. registry.example.internal (required)
  COSIGN_KEY         cosign key reference: hashivault://<transit key> (VAULT_ADDR, VAULT_TOKEN set) or a
                     key file path (COSIGN_PASSWORD set)
  REGISTRY_USER / REGISTRY_PASSWORD   robot with create/update on the mirrored namespaces (crane auth login)

Usage:
    tools/image_mirror.py --dry-run           # print the commands only
    tools/image_mirror.py --report out.json   # run, and write what was mirrored and signed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "roles" / "versions" / "defaults" / "main.yml"
REF = re.compile(r"^(?P<repo>[a-z0-9.-]+(?::[0-9]+)?/[^:@]+):(?P<tag>[^@]+)(?:@(?P<digest>sha256:[0-9a-f]{64}))?$")


def images() -> dict[str, str]:
    data = yaml.safe_load(VERSIONS.read_text())
    return dict(data["versions_images"])


def plan(mirror: str, ref: str) -> dict:
    match = REF.match(ref)
    if not match:
        raise ValueError(f"{ref}: expected <registry>/<path>:<tag>[@sha256:<digest>]")
    repo, tag, digest = match["repo"], match["tag"], match["digest"]
    source = f"{repo}@{digest}" if digest else f"{repo}:{tag}"
    return {"source": ref, "copy_from": source, "target": f"{mirror}/{repo}:{tag}",
            "target_repo": f"{mirror}/{repo}", "pinned_digest": digest}


class Runner:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def __call__(self, *argv: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
        print("+ " + " ".join(argv), flush=True)
        if self.dry_run:
            return subprocess.CompletedProcess(argv, 0, "sha256:" + "0" * 64 if capture else "", "")
        return subprocess.run(argv, check=check, text=True, capture_output=capture)


def mirror_one(run: Runner, item: dict, key: str, workdir: Path) -> dict:
    run("crane", "copy", item["copy_from"], item["target"])
    digest = run("crane", "digest", item["target"], capture=True).stdout.strip()
    if item["pinned_digest"] and not run.dry_run and digest != item["pinned_digest"]:
        raise RuntimeError(f"{item['target']} has digest {digest}, the release pins {item['pinned_digest']}")
    subject = f"{item['target_repo']}@{digest}"
    verified = run("cosign", "verify", "--key", key, "--insecure-ignore-tlog=true", subject,
                   check=False, capture=True)
    already = verified.returncode == 0 and not run.dry_run
    if not already:
        sbom = workdir / (re.sub(r"[^A-Za-z0-9_.-]", "_", item["target"]) + ".spdx.json")
        run("syft", "scan", f"registry:{subject}", "-o", f"spdx-json={sbom}")
        run("cosign", "attest", "--yes", "--tlog-upload=false", "--key", key, "--type", "spdxjson",
            "--predicate", str(sbom), subject)
        run("cosign", "sign", "--yes", "--tlog-upload=false", "--key", key, subject)
    return {**item, "digest": digest, "signed": True, "newly_signed": not already}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--only", action="append", default=[], help="versions_images key (repeatable)")
    args = parser.parse_args()

    mirror = os.environ.get("IMAGE_MIRROR", "registry.example.internal" if args.dry_run else "").rstrip("/")
    key = os.environ.get("COSIGN_KEY", "hashivault://ci-image-signing" if args.dry_run else "")
    if not mirror or not key:
        print("IMAGE_MIRROR and COSIGN_KEY are required", file=sys.stderr)
        return 2
    run = Runner(args.dry_run)
    if os.environ.get("REGISTRY_USER") and not args.dry_run:
        host = mirror.split("/")[0]
        subprocess.run(["crane", "auth", "login", host, "-u", os.environ["REGISTRY_USER"], "--password-stdin"],
                       input=os.environ.get("REGISTRY_PASSWORD", ""), text=True, check=True)
    selected = {k: v for k, v in images().items() if not args.only or k in args.only}
    results, failures = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for name, ref in sorted(selected.items()):
            try:
                results.append({"name": name, **mirror_one(run, plan(mirror, ref), key, Path(tmp))})
            except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
                failures.append({"name": name, "source": ref, "error": str(exc)})
                print(f"ERROR {name}: {exc}", file=sys.stderr)
    report = {"mirror": mirror, "key": key, "images": results, "failures": failures}
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"{len(results)} images mirrored and signed, {len(failures)} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
