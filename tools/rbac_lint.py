#!/usr/bin/env python3
"""Refuse access grants made to a username instead of a role (IAM-03).

Access is granted only through IdP groups assigned to the default roles (roles/rbac_model); services map
roles, never people, to their native permissions. This lint fails CI when the code or an inventory grants
something to an individual:

1. Role references. Every variable that maps roles to permissions (ROLE_VARS below) may only name roles
   defined in rbac_model_roles / rbac_model_extra_roles. A username or e-mail there is an error.
   rbac_model_group_assignments maps IdP *groups* to roles; a key that looks like an e-mail is an error.
2. Per-user constructs (TEXT_RULES below) anywhere in roles, playbooks and inventories: oauth2-proxy
   e-mail/user allow lists, role expressions comparing the login or e-mail, OpenBao roles bound to a
   user's claim, userpass users, Keycloak role mappings to a person, sudoers lines for a named user,
   literal "users:" lists in security mappings.
3. keycloak_user_rolemapping tasks must target a service account (service_account_user_client_id).

Local accounts that must exist (break-glass admins, service accounts) are listed with a reason in
compliance/rbac-lint-allow.yml (rule, path glob, line regex, reason).

Usage:
    tools/rbac_lint.py                 # this repository
    tools/rbac_lint.py --root DIR      # another tree (self-tests, a private inventory checkout)
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
from pathlib import Path

import yaml

SCAN_GLOBS = [
    "roles/*/defaults/**/*.yml",
    "roles/*/vars/**/*.yml",
    "roles/*/tasks/**/*.yml",
    "roles/*/handlers/**/*.yml",
    "roles/*/templates/**/*",
    "playbooks/**/*.yml",
    "inventories/**/*.yml",
]
SKIP_PARTS = {".git", "collections", ".cache", ".ansible", ".venv", "node_modules", "molecule"}

# variable -> how role names appear in it
#   keys      dict keys are roles
#   values    dict values are lists of roles
#   list      list of roles
#   scalar    one role (empty allowed)
#   attr:<a>  list of dicts, attribute <a> is a role (or a list of roles)
ROLE_VARS: dict[str, list[str]] = {
    "rbac_model_group_assignments": ["values"],
    "grafana_oidc_role_map": ["attr:role"],
    "wazuh_indexer_role_mapping": ["values"],
    "wazuh_manager_api_role_mapping": ["keys"],
    "openbao_role_policies": ["keys"],
    "openbao_ssh_role_principals": ["keys", "values"],
    "rabbitmq_cluster_role_scopes": ["keys"],
    "registry_admin_roles": ["list"],
    "registry_push_roles": ["list"],
    "registry_read_roles": ["list"],
    "forgejo_oidc_admin_role": ["scalar"],
    "forgejo_oidc_restricted_role": ["scalar"],
    "forgejo_oidc_group_team_map": ["keys"],
    "grc_role_user_groups": ["keys"],
    "admin_login_accounts": ["attr:name", "attr:principals"],
    "wireguard_gateway_idp_required_roles": ["list"],
}

TEXT_RULES = [
    ("user-allowlist",
     re.compile(r"(authenticated[_-]emails[_-]file|allowed[_-]emails|allowedEmails|allowed[_-]user\b|--allowed-user)"),
     "oauth2-proxy style allow list of e-mails or users; allow roles (allowedGroups) instead"),
    ("claim-equals-user",
     re.compile(r"\b(login|email|preferred_username|username|sub)\s*==\s*['\"`]"),
     "role expression compares a user's login, e-mail or subject; map the roles claim instead"),
    ("claim-contains-user",
     re.compile(r"contains\(\s*(login|email|preferred_username|username|sub)\b"),
     "role expression tests a user's login or e-mail; map the roles claim instead"),
    ("bound-user-claim",
     re.compile(r"bound_claims[^\n]*\b(sub|email|preferred_username|username)\b"),
     "OpenBao role bound to an individual's claim; bind groups_claim roles instead"),
    ("userpass-user",
     re.compile(r"auth/userpass/users/"),
     "OpenBao userpass user: people log in through OIDC"),
    ("entity-grant",
     re.compile(r"identity/entity(-alias)?/(name|id)/"),
     "policy attached to an OpenBao entity (a person); attach it to the role's identity group"),
    ("keycloak-user-role-mapping",
     re.compile(r"/users/[^\s\"']*/role-mappings"),
     "role mapping written to a Keycloak user; assign the user to an IdP group that holds the role"),
    ("sudoers-user",
     re.compile(r"^\s*(?!%|#|Defaults\b|\{\{)[A-Za-z0-9_.@-]+\s+ALL\s*=\s*\("),
     "sudo granted to a named user; grant it to a group or a role account (admin_login)"),
    ("literal-users-list",
     re.compile(r"^\s*['\"]?users['\"]?\s*:\s*\[\s*['\"]?[A-Za-z0-9]"),
     "literal list of users in a security mapping; map backend roles instead"),
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


class Loader(yaml.SafeLoader):
    pass


Loader.add_constructor("!vault", lambda loader, node: "<vault>")
Loader.add_constructor("!unsafe", lambda loader, node: loader.construct_scalar(node))


def files(root: Path) -> list[Path]:
    out = set()
    for pattern in SCAN_GLOBS:
        for path in root.glob(pattern):
            if path.is_file() and not SKIP_PARTS.intersection(path.relative_to(root).parts):
                out.add(path)
    return sorted(out)


def load_yaml(path: Path):
    try:
        return yaml.load(path.read_text(), Loader=Loader)  # noqa: S506 (SafeLoader subclass)
    except (yaml.YAMLError, UnicodeDecodeError):
        return None


def templated(value) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def known_roles(root: Path, yaml_files: list[Path]) -> set[str]:
    roles: set[str] = set()
    model = load_yaml(root / "roles" / "rbac_model" / "defaults" / "main.yml") or {}
    roles.update((model.get("rbac_model_roles") or {}).keys())
    for path in yaml_files:
        data = load_yaml(path)
        if isinstance(data, dict) and isinstance(data.get("rbac_model_extra_roles"), dict):
            roles.update(data["rbac_model_extra_roles"].keys())
    return roles


def role_names(value, how: str) -> list:
    """The role names a variable value refers to, per ROLE_VARS shape. Templated parts are skipped."""
    names: list = []
    if templated(value) or value is None:
        return names
    if how == "keys" and isinstance(value, dict):
        names += list(value.keys())
    elif how == "values" and isinstance(value, dict):
        for v in value.values():
            if isinstance(v, list):
                names += [x for x in v if not templated(x)]
            elif isinstance(v, str) and not templated(v):
                names.append(v)
    elif how == "list" and isinstance(value, list):
        names += [x for x in value if not templated(x)]
    elif how == "scalar" and isinstance(value, str) and value:
        names.append(value)
    elif how.startswith("attr:") and isinstance(value, list):
        attr = how.split(":", 1)[1]
        for item in value:
            if isinstance(item, dict) and attr in item and not templated(item[attr]):
                got = item[attr]
                names += got if isinstance(got, list) else [got]
    return names


def line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), 1):
        if re.search(rf"^\s*{re.escape(needle)}\s*:", line):
            return number
    return 1


def load_allow(root: Path) -> list[dict]:
    path = root / "compliance" / "rbac-lint-allow.yml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    entries = data.get("allow") or []
    for entry in entries:
        if not all(entry.get(k) for k in ("rule", "path", "reason")):
            raise SystemExit(f"ERROR {path}: every entry needs rule, path and reason: {entry}")
    return entries


def allowed(allow: list[dict], rule: str, rel: str, line: str) -> dict | None:
    for entry in allow:
        if entry["rule"] != rule or not fnmatch.fnmatch(rel, entry["path"]):
            continue
        if entry.get("match") and not re.search(entry["match"], line):
            continue
        return entry
    return None


def check_tasks(data, rel: str, text: str, report) -> None:
    """keycloak_user_rolemapping must target a service account."""
    if not isinstance(data, list):
        return
    stack = list(data)
    while stack:
        task = stack.pop()
        if not isinstance(task, dict):
            continue
        for key in ("block", "rescue", "always"):
            if isinstance(task.get(key), list):
                stack.extend(task[key])
        for module in ("community.general.keycloak_user_rolemapping", "keycloak_user_rolemapping"):
            args = task.get(module)
            if isinstance(args, dict) and not args.get("service_account_user_client_id"):
                name = str(task.get("name", module))
                number = next((n for n, line in enumerate(text.splitlines(), 1) if name in line), 1)
                report("keycloak-user-role-mapping", rel, number, name,
                       "keycloak_user_rolemapping to a person; map roles to groups (service accounts only)")


def lint(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    excepted: list[str] = []
    allow = load_allow(root)
    all_files = files(root)
    yaml_files = [p for p in all_files if p.suffix == ".yml"]
    roles = known_roles(root, yaml_files)
    if not roles:
        errors.append("roles/rbac_model/defaults/main.yml: no rbac_model_roles found")

    def report(rule: str, rel: str, number: int, line: str, message: str) -> None:
        entry = allowed(allow, rule, rel, line)
        where = f"{rel}:{number} [{rule}]"
        if entry:
            excepted.append(f"{where} {entry['reason']}")
        else:
            errors.append(f"{where} {message}: {line.strip()[:120]}")

    for path in all_files:
        rel = str(path.relative_to(root))
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#") and "sudoers" not in rel:
                continue
            for rule, regex, message in TEXT_RULES:
                if regex.search(line):
                    report(rule, rel, number, line, message)
        if path.suffix != ".yml":
            continue
        data = load_yaml(path)
        if "/tasks/" in f"/{rel}" or rel.startswith("playbooks/"):
            check_tasks(data, rel, text, report)
        if not isinstance(data, dict):
            continue
        for var, shapes in ROLE_VARS.items():
            if var not in data:
                continue
            number = line_of(text, var)
            for how in shapes:
                for name in role_names(data[var], how):
                    if not isinstance(name, str) or name not in roles:
                        report("unknown-role", rel, number, f"{var}: {name}",
                               f"{var} names '{name}', which is not a role of rbac_model (a user?)")
        assignments = data.get("rbac_model_group_assignments")
        if isinstance(assignments, dict):
            number = line_of(text, "rbac_model_group_assignments")
            for group in assignments:
                if isinstance(group, str) and EMAIL_RE.match(group):
                    report("user-as-group", rel, number, f"{group}",
                           "rbac_model_group_assignments maps IdP groups, not people")
    return errors, excepted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--report", action="store_true", help="also list the allowed exceptions")
    args = parser.parse_args()
    errors, excepted = lint(args.root.resolve())
    if args.report:
        for line in excepted:
            print(f"ALLOWED {line}")
    for line in errors:
        print(f"ERROR {line}")
    print(f"rbac lint: {len(errors)} errors, {len(excepted)} allowed exceptions")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
