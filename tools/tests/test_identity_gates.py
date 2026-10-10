"""Self-tests of the identity gates (tools/rbac_lint.py, tools/role_matrix.py) on small sample trees."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent

MODEL = """
rbac_model_roles:
  platform-admin: {capabilities: {monitoring: admin}}
  operator: {capabilities: {monitoring: write}}
  member: {capabilities: {monitoring: none}}
"""


def run(script: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(TOOLS / script), *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())


class Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        write(self.root, "roles/rbac_model/defaults/main.yml", MODEL)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class RbacLint(Tmp):
    def test_clean_tree(self) -> None:
        write(self.root, "roles/grafana/defaults/main.yml", "grafana_oidc_role_map:\n  - {role: operator, grafana_role: Editor}\n")
        write(self.root, "inventories/prod/group_vars/all/rbac.yml", "rbac_model_group_assignments:\n  sre: [operator]\n")
        rc, out = run("rbac_lint.py", "--root", str(self.root))
        self.assertEqual(rc, 0, out)

    def test_user_grants(self) -> None:
        write(self.root, "roles/grafana/defaults/main.yml", "grafana_oidc_role_map:\n  - {role: alice, grafana_role: Admin}\n")
        write(
            self.root,
            "inventories/prod/group_vars/all/rbac.yml",
            """
            rbac_model_group_assignments:
              bob@example.com: [platform-admin]
              sre: [operator, carol]
            openbao_role_policies:
              dave: [admin]
            """,
        )
        write(self.root, "roles/x/templates/proxy.cfg.j2", 'allowed_emails = ["eve@example.com"]\n')
        write(self.root, "roles/x/templates/grafana.env.j2", "GF_X_ROLE_ATTRIBUTE_PATH=login == 'frank' && 'Admin'\n")
        write(self.root, "roles/x/templates/sudoers.j2", "grace ALL=(ALL) NOPASSWD: ALL\n%admins ALL=(ALL) ALL\n")
        write(self.root, "roles/x/templates/roles_mapping.yml.j2", 'all_access:\n  users: ["heidi"]\n')
        write(
            self.root,
            "roles/x/tasks/main.yml",
            """
            - name: Give ivan the admin role
              community.general.keycloak_user_rolemapping:
                target_username: ivan
                roles: [{name: admin}]
            - name: Service account is fine
              community.general.keycloak_user_rolemapping:
                service_account_user_client_id: sync
                roles: [{name: view-users}]
            """,
        )
        rc, out = run("rbac_lint.py", "--root", str(self.root))
        self.assertEqual(rc, 1, out)
        for needle in ["names 'alice'", "bob@example.com", "names 'carol'", "names 'dave'", "[user-allowlist]",
                       "[claim-equals-user]", "grace ALL", "[literal-users-list]", "Give ivan"]:
            self.assertIn(needle, out)
        self.assertNotIn("%admins", out)
        self.assertNotIn("Service account is fine", out)

    def test_allow_list(self) -> None:
        write(self.root, "roles/x/templates/roles_mapping.yml.j2", 'all_access:\n  users: ["break-glass-admin"]\n')
        write(
            self.root,
            "compliance/rbac-lint-allow.yml",
            """
            allow:
              - rule: literal-users-list
                path: roles/x/templates/*
                match: break-glass-admin
                reason: sealed break-glass account
            """,
        )
        rc, out = run("rbac_lint.py", "--root", str(self.root), "--report")
        self.assertEqual(rc, 0, out)
        self.assertIn("ALLOWED roles/x/templates/roles_mapping.yml.j2:2 [literal-users-list] sealed", out)


class RoleMatrix(Tmp):
    MATRIX = """
    roles: [platform-admin, operator, member]
    services:
      grafana:
        source: {shape: list_attr, file: roles/grafana/defaults/main.yml, var: grafana_oidc_role_map, role_key: role, value_key: grafana_role}
        permissions: {platform-admin: Admin, operator: Editor, member: none}
      openbao:
        source: {shape: keys, file: roles/openbao/defaults/main.yml, var: openbao_role_policies}
        permissions: {platform-admin: [admin], operator: [ops], member: none}
      alertmanager:
        source: {shape: capability, file: inv.yml, instance: am, permission: access}
        permissions: {platform-admin: access, operator: access, member: none}
    """

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "roles/grafana/defaults/main.yml",
              "grafana_oidc_role_map:\n  - {role: platform-admin, grafana_role: Admin}\n  - {role: operator, grafana_role: Editor}\n")
        write(self.root, "roles/openbao/defaults/main.yml", "openbao_role_policies:\n  platform-admin: [admin]\n  operator: [ops]\n")
        write(self.root, "inv.yml", "oauth2_proxy_instances:\n  - {name: am, capability: monitoring, min_level: write}\n")

    def test_matches(self) -> None:
        write(self.root, "compliance/role-matrix.yml", self.MATRIX)
        rc, out = run("role_matrix.py", "--root", str(self.root), "--md")
        # Other ROLE_VARS are not covered in this small tree: only those errors are expected.
        errors = [line for line in out.splitlines() if line.startswith("ERROR") and "no compliance/role-matrix.yml row" not in line]
        self.assertEqual(errors, [], out)
        self.assertIn("| grafana | Admin | Editor | none |", out)

    def test_drift_and_missing_role(self) -> None:
        write(self.root, "compliance/role-matrix.yml",
              self.MATRIX.replace("operator: Editor, member: none", "operator: Viewer").replace(
                  "min_level: write", "min_level: admin"))
        write(self.root, "inv.yml", "oauth2_proxy_instances:\n  - {name: am, capability: monitoring, min_level: admin}\n")
        rc, out = run("role_matrix.py", "--root", str(self.root))
        self.assertEqual(rc, 1, out)
        self.assertIn("grafana/operator: matrix says Viewer, roles/grafana/defaults/main.yml gives Editor", out)
        self.assertIn("grafana: no permission given for member", out)
        self.assertIn("alertmanager/operator: matrix says access", out)
        self.assertIn("wazuh_manager_api_role_mapping maps roles to permissions but no", out)


if __name__ == "__main__":
    unittest.main()
