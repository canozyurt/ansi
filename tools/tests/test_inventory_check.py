"""Self-tests of tools/inventory_check.py on a small sample tree (python3 -m unittest discover -s tools/tests)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "inventory_check.py"

CHANGES = """\
changes:
  - kind: renamed
    var: app_old
    new: app_new
    removal_planned: v1.0.0
    note: Clearer name.
  - kind: removed
    var: app_gone
    note: Nothing reads it.
  - kind: required
    var: app_secret
    groups: [app]
    note: Store it in the vault.
  - kind: required
    var: db_name
    groups: [db]
    note: Name the database.
  - kind: renamed
    var: db_label
    new: db_name
    note: One name for the database.
  - kind: default
    var: app_tls_min
    old: "1.2"
    new: "1.3"
    note: TLS 1.3 only.
"""

HOSTS = """\
all:
  children:
    app:
      hosts:
        app-1:
        app-2:
          app_secret: inline
    db:
      hosts:
        db-1:
"""


class InventoryCheck(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.inv = Path(self._tmp.name) / "inv"
        self.write(self.root, "inventory-changes.yml", CHANGES)
        self.write(self.inv, "hosts.yml", HOSTS)
        self.write(self.inv, "group_vars/all/main.yml", "app_old: 1\n")
        self.write(self.inv, "host_vars/app-1.yml", "app_secret: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  3132\n")
        self.write(self.inv, "group_vars/db.yml", "db_label: main\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def write(base: Path, rel: str, text: str) -> None:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def run_tool(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(TOOL), "--root", str(self.root), str(self.inv), *args],
                              capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def test_deprecated_names_warn_and_satisfy_required(self) -> None:
        rc, out = self.run_tool()
        self.assertEqual(rc, 0, out)
        self.assertIn("WARN", out)
        self.assertIn("[unreleased] app_old: deprecated, renamed to app_new (group_vars/all/main.yml). "
                      "It still works until v1.0.0.", out)
        self.assertIn("db_label: deprecated, renamed to db_name", out)
        self.assertNotIn("db_name: required", out)  # the deprecated name still counts
        self.assertIn("NOTE  ", out)
        self.assertIn("app_tls_min: default changed from '1.2' to '1.3'", out)
        self.assertIn("0 error(s), 2 warning(s)", out)
        self.assertEqual(self.run_tool("--strict")[0], 1)

    def test_errors(self) -> None:
        self.write(self.inv, "group_vars/all/main.yml", "app_old: 1\napp_new: 2\napp_gone: x\napp_tls_min: '1.2'\n")
        self.write(self.inv, "host_vars/app-1.yml", "other: 1\n")
        self.write(self.inv, "group_vars/db.yml", "{}\n")
        rc, out = self.run_tool()
        self.assertEqual(rc, 1)
        self.assertIn("app_old: renamed to app_new, and both names are set", out)
        self.assertIn("app_gone: removed; its value is ignored (group_vars/all/main.yml). Nothing reads it.", out)
        self.assertIn("app_secret: required for app but not set for 1 host(s): app-1.", out)
        self.assertIn("db_name: required for db but not set for 1 host(s): db-1.", out)
        self.assertNotIn("app_tls_min", out)  # pinned by the inventory

    def test_encrypted_vars_file_cannot_be_verified(self) -> None:
        self.write(self.inv, "host_vars/app-1/vault.yml", "$ANSIBLE_VAULT;1.1;AES256\n3132\n")
        (self.inv / "host_vars/app-1.yml").unlink()
        rc, out = self.run_tool()
        self.assertEqual(rc, 0, out)
        self.assertIn("app_secret: required for app; cannot tell whether 1 host(s) set it in a vault-encrypted "
                      "vars file (app-1)", out)

    def test_release_tag(self) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.com",
                            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                           capture_output=True, text=True, check=True)
        git("init", "-q")
        self.write(self.root, "inventory-changes.yml", "changes: []\n")
        git("add", "-A")
        git("commit", "-q", "-m", "first")
        git("tag", "v0.1.0")
        self.write(self.root, "inventory-changes.yml", CHANGES)
        git("commit", "-q", "-am", "second")
        git("tag", "v0.2.0")
        self.write(self.root, "inventory-changes.yml", "changes: [{kind: removed, var: app_old, note: x}]\n")
        rc, out = self.run_tool("--release", "v0.1.0")
        self.assertEqual(rc, 0, out)
        self.assertIn("0 error(s), 0 warning(s) against v0.1.0", out)
        rc, out = self.run_tool("--release", "v0.2.0")
        self.assertIn("[v0.2.0] app_old: deprecated", out)
        rc, out = self.run_tool()  # the working tree: app_old is gone now
        self.assertEqual(rc, 1)
        self.assertIn("[unreleased] app_old: removed", out)
        self.assertEqual(self.run_tool("--release", "v9.0.0")[0], 2)

    def test_invalid_manifest(self) -> None:
        self.write(self.root, "inventory-changes.yml", "changes:\n  - {kind: renamed, var: a, note: x}\n"
                                                        "  - {kind: moved, var: b, note: x}\n")
        rc, out = self.run_tool()
        self.assertEqual(rc, 2)
        self.assertIn("renamed needs new", out)
        self.assertIn("kind must be one of", out)


if __name__ == "__main__":
    unittest.main()
