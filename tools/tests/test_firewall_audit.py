"""Unit tests for tools/firewall_audit.py (NET-02, NET-03). Run: python3 -m unittest discover tools/tests"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "firewall_audit.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "zones"


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, cwd=ROOT, check=False)


class FixtureInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run("--inventory", str(FIXTURE / "hosts.yml"), "--flows", str(FIXTURE / "flows.yml"))
        cls.findings = [line for line in cls.result.stderr.splitlines() if line.startswith("FINDING ")]

    def has(self, *parts):
        return any(all(p in f for p in parts) for f in self.findings)

    def test_fails(self):
        self.assertEqual(self.result.returncode, 1, self.result.stderr)

    def test_good_database_rule_passes(self):
        self.assertFalse(self.has("db-good"), self.findings)

    def test_database_open_to_whole_mgmt_zone(self):
        # Refused even though a mgmt -> data flow is declared for the port.
        self.assertTrue(self.has("db-mgmt-zone", "whole mgmt zone"), self.findings)

    def test_admin_open_to_any(self):
        self.assertTrue(self.has("admin-any", "open to any"), self.findings)
        self.assertTrue(self.has("admin-any", "internet -> data", "NET-03"), self.findings)

    def test_undeclared_inter_zone_flow(self):
        self.assertTrue(self.has("undeclared", "app -> data", "matches no flow"), self.findings)

    def test_missing_class(self):
        self.assertTrue(self.has("no-class", "class None"), self.findings)

    def test_source_spanning_zones(self):
        self.assertTrue(self.has("too-wide", "spans several zones"), self.findings)

    def test_address_outside_zone(self):
        self.assertTrue(self.has("data-2.test", "outside its zone"), self.findings)

    def test_ssh_rule_is_audited(self):
        self.assertFalse(self.has("rule ssh"), self.findings)


class ExampleInventory(unittest.TestCase):
    def test_example_inventory_is_clean_and_doc_current(self):
        result = run("--check-doc", str(ROOT / "docs" / "network.md"))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
