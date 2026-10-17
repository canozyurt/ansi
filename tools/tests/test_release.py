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

    # ------------------------------------------------------------------ first release and cut
    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.com",
                               "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
                              capture_output=True, text=True, check=True).stdout

    def make_releasable(self) -> None:
        """A tree that passes check_docs: CI runs every upgrade scenario, no DR or CLD gaps."""
        self.write(".github/workflows/ci.yml", """\
            jobs:
              m:
                strategy:
                  matrix:
                    scenario:
                      - app-upgrade
                      - db-upgrade
            """)
        self.write("docs/dr/README.md", "Rebuild order: [app](app.md)\n")
        self.write("docs/dr/app.md", "# app\n")
        self.write("roles/dr_drill/defaults/main.yml", "dr_drill_all_services: [app]\n")
        self.write("docs/shared-responsibility.md",
                   " ".join(["CLD.6.3.1", "CLD.8.1.5", "CLD.9.5.1", "CLD.9.5.2", "CLD.12.1.5",
                             "CLD.12.4.5", "CLD.13.1.4"]) + "\n")
        self.write("compliance/checklist.yml", "requirements: []\n")
        self.write("playbooks/upgrade.yml", "- hosts: app\n  roles:\n    - role: keycloak\n")
        self.write("roles/keycloak/defaults/main.yml", "keycloak_image: \"{{ versions_images.app }}\"\n")
        self.write("molecule/keycloak-upgrade/molecule.yml", "---\n")
        self.write("molecule/keycloak-upgrade/converge.yml",
                   "- import_playbook: ../../playbooks/upgrade.yml\n")
        ci = self.root / ".github/workflows/ci.yml"
        ci.write_text(ci.read_text() + "                      - keycloak-upgrade\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def test_first_release_notes(self) -> None:
        self.git("init", "-q")
        rc, out = self.run_tool("notes", "v0.1.0")
        self.assertEqual(rc, 0, out)
        self.assertIn("First release of this repository", out)
        self.assertIn("| images/app | (new) | docker.io/example/app:2.0.0 |", out)
        self.assertIn("app-upgrade: ready, from the upstream predecessor versions", out)
        self.assertIn("db-upgrade: not yet", out)

    def test_cut_first_release(self) -> None:
        self.make_releasable()
        rc, out = self.run_tool("check")
        self.assertEqual(rc, 0, out)
        notes = self.root / "notes.md"
        rc, out = self.run_tool("cut", "v0.1.0", "--notes-out", str(notes))
        self.assertEqual(rc, 0, out)
        self.assertIn("git tag -s --cleanup=verbatim v0.1.0 -F", out)
        self.assertIn("bump-upgrade-from v0.1.0", out)
        self.assertIn("First release", notes.read_text())
        self.assertEqual(self.git("tag", "--list").strip(), "", "cut must never create a tag")

    def test_cut_refuses(self) -> None:
        self.make_releasable()
        rc, out = self.run_tool("cut", "v1.0.0", "--notes-out", str(self.root / "n.md"))
        self.assertEqual(rc, 1)
        self.assertIn("the first release is v0.1.0", out)
        self.git("tag", "v0.1.0")
        self.write("extra.txt", "x\n")
        rc, out = self.run_tool("cut", "v0.1.0", "--notes-out", str(self.root / "n.md"))
        self.assertEqual(rc, 1)
        self.assertIn("already exists", out)
        self.assertIn("HEAD is already released as v0.1.0", out)
        self.assertIn("uncommitted changes", out)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "next")
        rc, out = self.run_tool("cut", "v0.2.0", "--notes-out", str(self.root / "n.md"))
        self.assertEqual(rc, 0, out)
        self.assertIn("after v0.1.0", out)

    def test_coverage_flags_a_pinned_role_without_an_upgrade_play(self) -> None:
        self.make_releasable()
        self.write("roles/valkey_cluster/defaults/main.yml", "valkey_cluster_image: \"{{ versions_images.db }}\"\n")
        self.write("roles/newsvc/defaults/main.yml", "newsvc_image: \"{{ versions_images.db }}\"\n")
        rc, out = self.run_tool("check")
        self.assertEqual(rc, 1)
        self.assertIn("playbooks/upgrade.yml does not upgrade roles/valkey_cluster", out)
        self.assertIn("molecule/valkey-upgrade (upgrade scenario of roles/valkey_cluster) is missing", out)
        self.assertIn("roles/newsvc reads roles/versions but has no upgrade coverage", out)


if __name__ == "__main__":
    unittest.main()
