#!/usr/bin/env python3
"""Fail on unpinned images, collections, packages and tool downloads (CHG-05).

What counts as pinned:

* Images (roles/versions ``versions_images`` and ``versions_upgrade_from``, literal ``image:``
  values, ``docker run`` in CI, Dockerfile ``FROM``): an explicit, non-floating tag
  (not ``latest``/``stable``/..., not a bare major like ``:3``). The rule for new entries is
  ``image:tag@sha256:<digest>`` (docs/supply-chain.md); ``--require-digest`` turns a missing
  digest into an error once every entry has one. Today digests are reported, not required.
* Ansible collections (requirements.yml) and Python tools (requirements-dev.txt): exact versions.
* Host tools (``versions_tools``, ``versions_postgres.patroni``): a version, a URL that names it
  and a SHA-256 per architecture. ``get_url`` tasks must set ``checksum:``.
* CI downloads (``curl``/``wget`` in workflow steps) must be checked with ``sha256sum`` in the same step.
* GitHub Actions should be pinned to a commit SHA; a tag is a warning (an error with ``--strict``).
* pre-commit hooks: ``rev`` must be a tag or commit, not a branch.

Molecule test platform images are test fixtures and only warned about.

Exceptions: a ``# pin-exception: <reason>`` comment on the line before or on the offending line.

Usage:
    tools/pins.py                     # exit 1 on any error
    tools/pins.py --strict            # warnings become errors
    tools/pins.py --require-digest    # every deployed image needs @sha256:
    tools/pins.py --list-images       # print every pinned image (one per line) for SBOM/scan jobs
    tools/pins.py --root DIR          # check another tree (self-tests)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

FLOATING_TAGS = {"latest", "stable", "edge", "main", "master", "nightly", "dev", "rolling", "current", "lts"}
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXACT_VERSION_RE = re.compile(r"^\d+(\.\d+)+([.-][0-9A-Za-z.+~-]+)?$")
MAJOR_ONLY_RE = re.compile(r"^v?\d+(-[a-z0-9]+)?$")
EXCEPTION_RE = re.compile(r"#\s*pin-exception:\s*\S")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


def split_image(ref: str) -> tuple[str, str, str]:
    """Return (repository, tag, digest) of an image reference."""
    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
    else:
        repo, tag = ref, ""
    return repo, tag, digest


def image_problem(ref: str, require_digest: bool = False) -> str:
    """Why an image reference is not pinned, or "" when it is."""
    ref = ref.strip()
    _, tag, digest = split_image(ref)
    if digest and not DIGEST_RE.search("@" + digest):
        return f"malformed digest in {ref!r}"
    if not tag and not digest:
        return f"{ref!r} has no tag"
    if tag.lower() in FLOATING_TAGS:
        return f"{ref!r} uses the floating tag {tag!r}"
    if tag and MAJOR_ONLY_RE.match(tag) and not digest:
        return f"{ref!r} uses a major-version tag that moves; pin the full version"
    if require_digest and not digest:
        return f"{ref!r} has no @sha256: digest"
    return ""


def has_exception(lines: list[str], index: int) -> bool:
    """A pin-exception comment on the line itself or the line(s) just before it."""
    for i in (index, index - 1, index - 2):
        if 0 <= i < len(lines) and EXCEPTION_RE.search(lines[i]):
            return True
    return False


def load(path: Path):
    return yaml.safe_load(path.read_text())


def check_versions(root: Path, report: Report, require_digest: bool) -> list[str]:
    path = root / "roles" / "versions" / "defaults" / "main.yml"
    where = str(path.relative_to(root))
    images: list[str] = []
    if not path.exists():
        report.error(where, "missing")
        return images
    data = load(path) or {}
    for block in ("versions_images", "versions_upgrade_from"):
        for name, ref in (data.get(block) or {}).items():
            problem = image_problem(str(ref), require_digest)
            if problem:
                report.error(where, f"{block}.{name}: {problem}")
            elif not split_image(str(ref))[2]:
                report.warn(where, f"{block}.{name}: no digest yet ({ref})")
            if block == "versions_images":
                images.append(str(ref))
    for name, version in (data.get("versions_packages") or {}).items():
        if not re.match(r"^\d[0-9A-Za-z.+:~-]*$", str(version)) or "*" in str(version):
            report.error(where, f"versions_packages.{name}: {version!r} is not an exact package version")
    for name, tool in (data.get("versions_tools") or {}).items():
        check_tool(where, f"versions_tools.{name}", tool, report)
    pg = data.get("versions_postgres") or {}
    for pkg, version in (pg.get("packages") or {}).items():
        if not re.match(r"^\d[0-9A-Za-z.+:~-]*$", str(version)) or "*" in str(version):
            report.error(where, f"versions_postgres.packages.{pkg}: {version!r} is not exact")
    if pg.get("patroni"):
        patroni = pg["patroni"]
        if not SHA256_RE.match(str(patroni.get("sha256", ""))):
            report.error(where, "versions_postgres.patroni: sha256 missing or malformed")
        if str(patroni.get("version", "")) not in str(patroni.get("url", "")):
            report.error(where, "versions_postgres.patroni: url does not name the version")
    return images


def check_tool(where: str, label: str, tool, report: Report) -> None:
    if not isinstance(tool, dict):
        report.error(where, f"{label}: expected a mapping with version, url and sha256")
        return
    version = str(tool.get("version", ""))
    if not EXACT_VERSION_RE.match(version):
        report.error(where, f"{label}: version {version!r} is not exact")
    if version and version not in str(tool.get("url", "")):
        report.error(where, f"{label}: url does not name version {version}")
    sums = tool.get("sha256")
    if isinstance(sums, dict):
        if not sums:
            report.error(where, f"{label}: no sha256 per architecture")
        for arch, value in sums.items():
            if not SHA256_RE.match(str(value)):
                report.error(where, f"{label}: sha256 for {arch} malformed")
    elif not SHA256_RE.match(str(sums or "")):
        report.error(where, f"{label}: sha256 missing or malformed")


def check_requirements(root: Path, report: Report) -> None:
    path = root / "requirements.yml"
    if path.exists():
        data = load(path) or {}
        for coll in data.get("collections") or []:
            name = coll.get("name") if isinstance(coll, dict) else coll
            version = str(coll.get("version", "")) if isinstance(coll, dict) else ""
            if not re.match(r"^\d+\.\d+\.\d+$", version):
                report.error("requirements.yml", f"collection {name}: version {version or '<none>'!r} is not exact")
        for role in data.get("roles") or []:
            if not isinstance(role, dict) or not role.get("version"):
                report.error("requirements.yml", f"role {role}: no version")
    path = root / "requirements-dev.txt"
    if path.exists():
        for n, line in enumerate(path.read_text().splitlines(), 1):
            line = line.split("#", 1)[0].strip()
            if line and "==" not in line:
                report.error(f"requirements-dev.txt:{n}", f"{line!r} is not pinned with ==")


IMAGE_LINE_RE = re.compile(r"^\s*-?\s*image:\s*['\"]?([^'\"#\s]+)['\"]?\s*(#.*)?$")


def check_yaml_sources(root: Path, report: Report) -> None:
    files = (
        sorted(root.glob("roles/**/*.yml"))
        + sorted(root.glob("playbooks/**/*.yml"))
        + sorted(root.glob("inventories/example/**/*.yml"))
    )
    for path in files:
        rel = str(path.relative_to(root))
        if rel.startswith("roles/versions/"):
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            match = IMAGE_LINE_RE.match(line)
            if match and "{{" not in match.group(1) and not has_exception(lines, i):
                ref = match.group(1)
                problem = image_problem(ref)
                if problem:
                    report.error(f"{rel}:{i + 1}", problem)
                else:
                    report.error(f"{rel}:{i + 1}", f"literal image {ref!r}: take it from versions_images")
        check_get_url(rel, path, lines, report)


def check_get_url(rel: str, path: Path, lines: list[str], report: Report) -> None:
    try:
        data = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError:
        return
    if not isinstance(data, list):
        return
    stack = list(data)
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        for key in ("block", "rescue", "always"):
            stack.extend(item.get(key) or [])
        args = item.get("ansible.builtin.get_url") or item.get("get_url")
        if not isinstance(args, dict) or args.get("checksum"):
            continue
        name = str(item.get("name", ""))
        index = next((i for i, line in enumerate(lines) if name and name in line), -1)
        block = lines[max(index - 2, 0) : index + 12] if index >= 0 else []
        if any(EXCEPTION_RE.search(line) for line in block):
            continue
        report.error(rel, f"get_url task {name!r} has no checksum (or # pin-exception: reason)")


USES_RE = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^'\"\s#]+)")
DOCKER_RUN_RE = re.compile(r"docker\s+run\b(.*)")
DOWNLOAD_RE = re.compile(r"\b(curl|wget)\b[^\n|]*https?://")


def docker_run_image(args: str) -> str:
    """First positional argument of ``docker run`` (skips options and their values)."""
    tokens = args.replace("\\", " ").split()
    skip_value = {"-v", "--volume", "-e", "--env", "-w", "--workdir", "--name", "--entrypoint", "-u", "--user",
                  "--network", "--platform", "--env-file", "--mount", "-p", "--publish"}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in skip_value:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok.strip("'\"")
    return ""


def check_workflows(root: Path, report: Report, image_vars: set[str]) -> None:
    for path in sorted((root / ".github" / "workflows").glob("*.y*ml")):
        rel = str(path.relative_to(root))
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            match = USES_RE.match(line)
            if match and not match.group(1).startswith("./"):
                ref = match.group(1)
                if "@" not in ref:
                    report.error(f"{rel}:{i + 1}", f"action {ref} has no version")
                elif not GIT_SHA_RE.match(ref.split("@", 1)[1]) and not has_exception(lines, i):
                    report.warn(f"{rel}:{i + 1}", f"action {ref} is pinned to a tag, not a commit SHA")
            run = DOCKER_RUN_RE.search(line)
            if run:
                image = docker_run_image(run.group(1))
                if image and not image.startswith("$") and "{{" not in image and "${{" not in image:
                    problem = image_problem(image)
                    if problem and not has_exception(lines, i):
                        report.error(f"{rel}:{i + 1}", problem)
        data = load(path) or {}
        envs = [("workflow", data.get("env") or {})] + [
            (name, job.get("env") or {}) for name, job in (data.get("jobs") or {}).items()
        ]
        for scope, env in envs:
            for key, value in env.items():
                if key.endswith("_IMAGE") and "${{" not in str(value):
                    problem = image_problem(str(value))
                    if problem:
                        report.error(rel, f"{scope} env {key}: {problem}")
        for job_name, job in (data.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                run = str(step.get("run", ""))
                if DOWNLOAD_RE.search(run) and "sha256sum" not in run:
                    report.error(rel, f"job {job_name}, step {step.get('name', '?')!r}: download without sha256sum check")


def check_precommit(root: Path, report: Report) -> None:
    path = root / ".pre-commit-config.yaml"
    if not path.exists():
        return
    for repo in (load(path) or {}).get("repos") or []:
        if repo.get("repo") in ("local", "meta"):
            continue
        rev = str(repo.get("rev", ""))
        if not rev or rev in FLOATING_TAGS or not (re.match(r"^v?\d", rev) or GIT_SHA_RE.match(rev)):
            report.error(".pre-commit-config.yaml", f"{repo.get('repo')}: rev {rev!r} is not a tag or commit")


def check_dockerfiles(root: Path, report: Report) -> None:
    for path in sorted(root.glob("roles/**/*Dockerfile*")):
        rel = str(path.relative_to(root))
        for i, line in enumerate(path.read_text().splitlines()):
            match = re.match(r"^\s*FROM\s+(\S+)", line, re.I)
            if not match:
                continue
            ref = match.group(1)
            if ref.startswith("$") or ref == "scratch":
                continue  # build argument filled from versions_images by the role
            problem = image_problem(ref)
            if problem:
                report.error(f"{rel}:{i + 1}", problem)


def check_molecule(root: Path, report: Report) -> None:
    unpinned: dict[str, set[str]] = {}
    for path in sorted(root.glob("molecule/*/molecule.yml")) + sorted(root.glob("roles/*/molecule/*/molecule.yml")):
        data = load(path) or {}
        for platform in data.get("platforms") or []:
            image = str(platform.get("image", ""))
            if image_problem(re.sub(r"\$\{[^}]*\}", "x", image)):
                unpinned.setdefault(image, set()).add(path.parent.name)
    for image, scenarios in sorted(unpinned.items()):
        report.warn("molecule", f"test platform image {image} is not pinned ({len(scenarios)} scenarios)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--strict", action="store_true", help="treat warnings as errors")
    parser.add_argument("--require-digest", action="store_true", help="every deployed image needs a digest")
    parser.add_argument("--list-images", action="store_true", help="print the deployed images (versions_images) and exit")
    parser.add_argument("--quiet", action="store_true", help="do not print warnings")
    args = parser.parse_args()
    root = args.root
    report = Report()
    images = check_versions(root, report, args.require_digest)
    if args.list_images:
        for image in sorted(set(images)):
            print(image)
        return 1 if report.errors else 0
    check_requirements(root, report)
    check_yaml_sources(root, report)
    check_workflows(root, report, set())
    check_precommit(root, report)
    check_dockerfiles(root, report)
    check_molecule(root, report)
    if not args.quiet or args.strict:
        for w in report.warnings:
            print(f"WARN  {w}")
    for e in report.errors:
        print(f"ERROR {e}")
    digests = sum(1 for i in images if "@sha256:" in i)
    failed = report.errors + (report.warnings if args.strict else [])
    print(f"{len(images)} images ({digests} with digest); {len(report.errors)} errors, {len(report.warnings)} warnings")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
