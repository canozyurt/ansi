"""Self-tests of the CI gate scripts on small sample trees (python3 -m unittest discover -s tools/tests)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent


def run(script: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(TOOLS / script), *args], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip())
    return path


class Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ContainerPolicy(Tmp):
    def test_flags_and_exceptions(self) -> None:
        write(self.root, "roles/x/defaults/main.yml", "x_image: docker.io/library/nginx:latest\ny_image: '{{ versions_images.y }}'\n")
        write(
            self.root,
            "roles/x/tasks/main.yml",
            """
            - name: Bad
              community.docker.docker_container:
                name: bad
                image: "{{ x_image }}"
                privileged: true
            - name: Good
              community.docker.docker_container:
                name: good
                image: "{{ y_image }}"
                user: "1000:1000"
                # policy-exception: read_only: writes its cache at start
                cap_drop: [ALL]
                security_opts: [no-new-privileges:true]
                memory: 256m
                pids_limit: 64
            - name: Restart only
              community.docker.docker_container:
                name: good
                image: "{{ x_image }}"
                state: started
                restart: true
            """,
        )
        rc, out = run("container_policy.py", "--root", str(self.root))
        self.assertEqual(rc, 1, out)
        for rule in ("pinned_image", "cap_drop", "no_new_privs", "user", "read_only", "memory", "pids_limit", "privileged"):
            self.assertIn(f"Bad: [{rule}]", out)
        self.assertNotIn("FAIL roles/x/tasks/main.yml: Good", out)
        self.assertNotIn("Restart only", out)
        self.assertIn("2 container tasks checked", out)


class Pins(Tmp):
    def test_unpinned(self) -> None:
        write(
            self.root,
            "roles/versions/defaults/main.yml",
            """
            versions_images:
              a: docker.io/x/a:latest
              b: docker.io/x/b:1.2.3
              c: docker.io/x/c:3
            versions_tools:
              t: {version: 1.0.0, url: "https://example.org/1.0.0/t", sha256: {amd64: bad}}
            """,
        )
        write(self.root, "requirements.yml", "collections:\n  - {name: a.b, version: '>=1.0.0'}\n")
        write(self.root, "requirements-dev.txt", "ansible-core==2.19.13\nPyYAML>=6\n")
        write(
            self.root,
            ".github/workflows/ci.yml",
            """
            jobs:
              a:
                env:
                  SCAN_IMAGE: docker.io/x/scan:latest
                steps:
                  - uses: actions/checkout@v4
                  - run: curl -sSLo tool https://example.org/tool
            """,
        )
        rc, out = run("pins.py", "--root", str(self.root))
        self.assertEqual(rc, 1, out)
        self.assertIn("versions_images.a", out)
        self.assertIn("versions_images.c", out)
        self.assertNotIn("ERROR roles/versions/defaults/main.yml: versions_images.b", out)
        self.assertIn("sha256 for amd64 malformed", out)
        self.assertIn("collection a.b", out)
        self.assertIn("PyYAML>=6", out)
        self.assertIn("SCAN_IMAGE", out)
        self.assertIn("download without sha256sum", out)
        self.assertIn("WARN  .github/workflows/ci.yml", out)

    def test_list_images(self) -> None:
        write(self.root, "roles/versions/defaults/main.yml", "versions_images:\n  b: docker.io/x/b:1.2.3\n")
        rc, out = run("pins.py", "--root", str(self.root), "--list-images")
        self.assertEqual((rc, out.strip()), (0, "docker.io/x/b:1.2.3"))


class TrivyGate(Tmp):
    def test_waivers(self) -> None:
        report = {
            "ArtifactName": "docker.io/library/haproxy:3.4.6-alpine",
            "Results": [
                {"Target": "os", "Vulnerabilities": [
                    {"VulnerabilityID": "CVE-1", "PkgName": "a", "Severity": "CRITICAL"},
                    {"VulnerabilityID": "CVE-2", "PkgName": "b", "Severity": "CRITICAL"},
                    {"VulnerabilityID": "CVE-3", "PkgName": "c", "Severity": "HIGH"},
                ]},
                {"Target": "/app/.env", "Secrets": [{"RuleID": "aws-access-key-id", "Severity": "HIGH"}]},
            ],
        }
        (self.root / "r.json").write_text(json.dumps(report))
        write(self.root, "w.yml", """
            waivers:
              - {id: CVE-2, image: docker.io/library/haproxy, reason: unreachable, expires: 2026-12-01}
              - {id: CVE-9, reason: old, expires: 2026-01-01}
            """)
        rc, out = run("trivy_gate.py", str(self.root / "r.json"), "--waivers", str(self.root / "w.yml"), "--today", "2026-10-09")
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL  docker.io/library/haproxy:3.4.6-alpine: CVE-1", out)
        self.assertIn("WAIVED docker.io/library/haproxy:3.4.6-alpine: CVE-2", out)
        self.assertNotIn("CVE-3", out)
        self.assertIn("secret aws-access-key-id", out)
        self.assertIn("expired on 2026-01-01", out)

    def test_clean(self) -> None:
        (self.root / "r.json").write_text(json.dumps({"ArtifactName": "x", "Results": []}))
        rc, out = run("trivy_gate.py", str(self.root / "r.json"), "--waivers", str(self.root / "none.yml"))
        self.assertEqual(rc, 0, out)


class VaultCheck(Tmp):
    def test_plaintext(self) -> None:
        write(self.root, "inv/group_vars/all/main.yml", """
            vault_ok: !vault |
              $ANSIBLE_VAULT;1.1;AES256
              6162
            vault_lookup: "{{ lookup('community.hashi_vault.vault_kv2_get', 'x') }}"
            vault_bad: hunter2
            """)
        write(self.root, "inv/group_vars/all/vault.yml", "vault_x: plain\n")
        write(self.root, "inv/group_vars/web/vault.yml", "$ANSIBLE_VAULT;1.1;AES256\n6162\n")
        rc, out = run("vault_check.py", str(self.root / "inv"))
        self.assertEqual(rc, 1, out)
        self.assertIn("vault_bad is not encrypted", out)
        self.assertIn("vault file is not encrypted", out)
        self.assertNotIn("vault_ok", out)
        self.assertNotIn("vault_lookup", out)
        self.assertNotIn("web/vault.yml", out)


class InventoryLint(Tmp):
    def test_separation(self) -> None:
        write(self.root, "prod/hosts.yml", """
            all:
              children:
                backup: {hosts: {bk-1.prod.internal: {ansible_host: 10.0.0.5}}}
            """)
        write(self.root, "prod/group_vars/all/main.yml", """
            inventory_environment: production
            forgejo_s3_endpoint: s3.prod.internal:9000
            forgejo_s3_bucket: prod-forgejo
            """)
        write(self.root, "test/hosts.yml", """
            all:
              vars: {inventory_environment: test}
              children:
                web: {hosts: {bk-1.prod.internal: , web-1.test.internal: }}
            """)
        write(self.root, "test/group_vars/all/main.yml", "restore_from: https://s3.prod.internal/prod-forgejo\nok: s3.test.internal\n")
        write(self.root, "dev/hosts.yml", "all:\n  hosts:\n    dev-1.internal:\n")
        rc, out = run("inventory_lint.py", str(self.root / "prod"), str(self.root / "test"), str(self.root / "dev"))
        self.assertEqual(rc, 1, out)
        self.assertIn("dev: inventory_environment is not set", out)
        self.assertIn("host bk-1.prod.internal is in inventory prod", out)
        self.assertIn("production target 's3.prod.internal'", out)
        self.assertIn("production target 'prod-forgejo'", out)
        self.assertNotIn("s3.test.internal", out)


class ToolRegistry(Tmp):
    def test_missing_entries(self) -> None:
        write(self.root, "roles/versions/defaults/main.yml", "versions_images:\n  a: x/a:1.0.0\n  b: x/b:1.0.0\n")
        write(self.root, "compliance/tools.yml", """
            tools:
              - name: A
                versions: [images/a]
                license: MIT
                osi_approved: true
                sso: {status: "no", note: "none"}
                audit_log: {status: "yes"}
                ha: {status: "yes"}
            """)
        rc, out = run("tool_registry.py", "--root", str(self.root))
        self.assertEqual(rc, 1, out)
        self.assertIn("images/b has no entry", out)
        self.assertIn("no documented exception for sso", out)


if __name__ == "__main__":
    unittest.main()
