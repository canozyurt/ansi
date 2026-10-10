"""Self-tests of tools/release.py on small sample trees (python3 -m unittest discover -s tools/tests)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

TOOL = Path(__file__).resolve().parent.parent / "release.py"

VERSIONS = """\
---
versions_images:
  app: docker.io/example/app:2.0.0
  db: docker.io/example/db:16.4

versions_postgres:
  major: 18
  packages: {postgresql-18: 18.6-1}

# What the previous release deployed.
versions_upgrade_from:
  app: docker.io/example/app:1.9.0

# Recipe of the previous release.
versions_postgres_upgrade_from: {}
"""


class Release(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write("roles/versions/defaults/main.yml", VERSIONS)
        self.write("molecule/app-upgrade/molecule.yml", "---\n")
        self.write("molecule/app-upgrade/prepare.yml", """\
            - import_playbook: ../shared/check_upgrade_from.yml
              vars:
                check_upgrade_from_keys: [app]
            """)
        self.write("molecule/db-upgrade/molecule.yml", "---\n")
        self.write("molecule/db-upgrade/prepare.yml", """\
            - import_playbook: ../shared/check_upgrade_from.yml
              vars:
                check_upgrade_from_keys: [db]
                check_upgrade_from_vars: [versions_postgres_upgrade_from]
            """)
        self.write("molecule/app/molecule.yml", "---\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))

    def run_tool(self, *args: str) -> tuple[int, str]:
        proc = subprocess.run([sys.executable, str(TOOL), "--root", str(self.root), *args],
                              capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout + proc.stderr

    def test_has_upgrade_path(self) -> None:
        self.assertEqual(self.run_tool("has-upgrade-path", "app-upgrade")[0], 0)
        rc, out = self.run_tool("has-upgrade-path", "db-upgrade")
        self.assertEqual(rc, 3)
        self.assertIn("versions_upgrade_from.db", out)
        self.assertIn("versions_postgres_upgrade_from", out)
        self.assertEqual(self.run_tool("has-upgrade-path", "app")[0], 0)
        self.assertEqual(self.run_tool("has-upgrade-path", "missing")[0], 2)

    def test_bump_upgrade_from_keeps_comments_and_fills_both_blocks(self) -> None:
        released = self.root / "released.yml"
        released.write_text(VERSIONS)
        rc, out = self.run_tool("bump-upgrade-from", "v1.0.0", "--from-file", str(released))
        self.assertEqual(rc, 0, out)
        text = (self.root / "roles/versions/defaults/main.yml").read_text()
        data = yaml.safe_load(text)
        self.assertEqual(data["versions_upgrade_from"], data["versions_images"])
        self.assertEqual(data["versions_postgres_upgrade_from"], data["versions_postgres"])
        self.assertIn("# Recipe of the previous release.", text)
        self.assertEqual(self.run_tool("has-upgrade-path", "db-upgrade")[0], 0)

    def test_bump_refuses_bad_tag(self) -> None:
        self.assertEqual(self.run_tool("bump-upgrade-from", "1.0")[0], 2)


if __name__ == "__main__":
    unittest.main()
