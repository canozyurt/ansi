#!/usr/bin/env python3
"""Check compliance/role-matrix.yml against the role model and the code (IAM-09).

* The matrix lists exactly the default roles of roles/rbac_model (rbac_model_roles).
* Every service row gives a permission for every role.
* Every row with a machine-readable source matches the variable that implements it (see the shapes
  in compliance/role-matrix.yml). A difference fails: either the code changed access without review,
  or the matrix is out of date.
* Every role-mapping variable known to tools/rbac_lint.py (ROLE_VARS) is the source of some row, so a
  new service mapping cannot skip the matrix.

Usage:
    tools/role_matrix.py                 # check
    tools/role_matrix.py --md            # also print the matrix as a Markdown table
    tools/role_matrix.py --root DIR      # another tree (self-tests)
    tools/role_matrix.py --compare results.json
        # compare what test users got ({"<service>": {"<role>": value}}, from playbooks/test_role_matrix.yml)
        # with the matrix; a row's optional probe_filter (regex) keeps only the list items a probe can see
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rbac_lint import ROLE_VARS, Loader  # noqa: E402

LEVELS = ["none", "self", "read", "operate", "write", "admin"]
# Role-mapping variables that are not a service's access (they assign groups or are checked elsewhere).
NOT_IN_MATRIX = {"rbac_model_group_assignments", "admin_login_accounts", "forgejo_oidc_group_team_map"}


def load(path: Path):
    return yaml.load(path.read_text(), Loader=Loader)  # noqa: S506 (SafeLoader subclass)


def norm(value):
    if value is None or value == "none" or value == []:
        return "none"
    if isinstance(value, list):
        return sorted(str(v) for v in value)
    return str(value)


def all_roles_expr(value) -> bool:
    return isinstance(value, str) and "rbac_model_roles" in value and "{{" in value


def from_code(root: Path, source: dict, roles: list[str], model: dict) -> dict[str, object]:
    """Permission per role as the code defines it."""
    shape = source["shape"]
    data = load(root / source["file"]) or {}
    default = source.get("default", "none")
    out: dict[str, object] = {role: default for role in roles}
    if shape == "list_attr":
        for item in reversed(data.get(source["var"]) or []):  # reversed: the first match wins
            out[item[source["role_key"]]] = item[source["value_key"]]
    elif shape == "keys":
        for role, value in (data.get(source["var"]) or {}).items():
            out[role] = value
    elif shape == "values":
        got: dict[str, list] = {}
        for permission, members in (data.get(source["var"]) or {}).items():
            for role in members or []:
                got.setdefault(role, []).append(permission)
        out.update(got)
    elif shape == "lists":
        for var, permission in reversed(list(source["vars"].items())):
            members = data.get(var)
            members = roles if all_roles_expr(members) else (members or [])
            for role in members:
                out[role] = permission
    elif shape == "scalars":
        for var, permission in reversed(list(source["vars"].items())):
            role = data.get(var)
            if role:
                out[role] = permission
    elif shape == "capability":
        instance = next(i for i in data.get("oauth2_proxy_instances") or [] if i.get("name") == source["instance"])
        need = LEVELS.index(instance["min_level"])
        for role in roles:
            level = (model.get(role) or {}).get("capabilities", {}).get(instance["capability"], "none")
            out[role] = source["permission"] if LEVELS.index(level) >= need else "none"
    else:
        raise ValueError(f"unknown shape {shape}")
    return out


def check(root: Path) -> tuple[list[str], dict]:
    errors: list[str] = []
    matrix = load(root / "compliance" / "role-matrix.yml") or {}
    model = (load(root / "roles" / "rbac_model" / "defaults" / "main.yml") or {}).get("rbac_model_roles") or {}
    roles = matrix.get("roles") or []
    if sorted(roles) != sorted(model):
        errors.append(f"roles {sorted(roles)} differ from rbac_model_roles {sorted(model)}")
    covered = set()
    for name, service in (matrix.get("services") or {}).items():
        perms = service.get("permissions") or {}
        missing = [r for r in roles if r not in perms]
        extra = [r for r in perms if r not in roles]
        if missing:
            errors.append(f"{name}: no permission given for {', '.join(missing)}")
        if extra:
            errors.append(f"{name}: unknown roles {', '.join(extra)}")
        source = service.get("source") or {}
        if source.get("shape") == "documented":
            if not source.get("note"):
                errors.append(f"{name}: a documented row needs a note saying where the access is fixed")
            continue
        try:
            code = from_code(root, source, roles, model)
        except (OSError, KeyError, StopIteration, ValueError, TypeError) as exc:
            errors.append(f"{name}: cannot read the source {source}: {exc!r}")
            continue
        covered.update([source["var"]] if "var" in source else list(source.get("vars", {})))
        for role in roles:
            want, have = norm(perms.get(role)), norm(code.get(role))
            if want != have:
                errors.append(f"{name}/{role}: matrix says {want}, {source.get('file')} gives {have}")
    for var in sorted(set(ROLE_VARS) - NOT_IN_MATRIX - covered):
        errors.append(f"{var} maps roles to permissions but no compliance/role-matrix.yml row uses it as source")
    return errors, matrix


def compare(matrix: dict, results: dict) -> tuple[list[str], dict]:
    """Differences between observed access and the matrix. Errors observed during a probe also count."""
    import re

    problems: list[str] = []
    report: dict = {}
    for name, observed in results.items():
        service = (matrix.get("services") or {}).get(name)
        if service is None:
            problems.append(f"{name}: not in the matrix")
            continue
        pattern = service.get("probe_filter")
        for role, value in observed.items():
            want = norm(service["permissions"].get(role))
            if pattern and isinstance(want, list):
                want = [v for v in want if re.search(pattern, v)] or "none"
            have = norm(value)
            ok = want == have
            report.setdefault(name, {})[role] = {"expected": want, "observed": have, "ok": ok}
            if not ok:
                problems.append(f"{name}/{role}: expected {want}, observed {have}")
    return problems, report


def markdown(matrix: dict) -> str:
    roles = matrix.get("roles") or []
    lines = ["| Service | " + " | ".join(roles) + " |", "| --- |" + " --- |" * len(roles)]
    for name, service in (matrix.get("services") or {}).items():
        cells = []
        for role in roles:
            value = norm(service["permissions"].get(role))
            cells.append(", ".join(value) if isinstance(value, list) else value)
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--md", action="store_true")
    parser.add_argument("--compare", type=Path, help="JSON of observed access per service and role")
    args = parser.parse_args()
    if args.compare:
        import json

        matrix = load(args.root.resolve() / "compliance" / "role-matrix.yml") or {}
        problems, report = compare(matrix, json.loads(args.compare.read_text()))
        print(json.dumps({"ok": not problems, "problems": problems, "services": report}, indent=2, sort_keys=True))
        return 1 if problems else 0
    errors, matrix = check(args.root.resolve())
    if args.md:
        print(markdown(matrix))
    for error in errors:
        print(f"ERROR {error}")
    print(f"role matrix: {len(matrix.get('services') or {})} services, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
