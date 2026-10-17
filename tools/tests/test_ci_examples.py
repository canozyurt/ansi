"""The pipeline examples users copy into their deployment repository (examples/ci, docs/ci.md) stay
usable as the tools change, and this repository's own CI never deploys anywhere."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLES = ROOT / "examples" / "ci"
FLAVOURS = ("github", "forgejo")
# What a deployment needs from an operator's own infrastructure: production inventories, deploy
# credentials, the image mirror. None of it belongs in this repository's workflows.
OPERATOR_ONLY = re.compile(r"inventories/production|environment:\s*production|BAO_DEPLOY|IMAGE_MIRROR|\[self-hosted, deploy\]")


def examples(flavour: str) -> dict[str, Path]:
    return {p.name: p for p in sorted((EXAMPLES / flavour).glob("*.yml"))}


class CiExamples(unittest.TestCase):
    def test_both_ci_systems_have_every_pipeline(self) -> None:
        github, forgejo = examples("github"), examples("forgejo")
        self.assertTrue(github, "no examples under examples/ci/github")
        self.assertEqual(sorted(github), sorted(forgejo))

    def test_examples_parse_and_reference_existing_files(self) -> None:
        for flavour in FLAVOURS:
            for name, path in examples(flavour).items():
                with self.subTest(example=f"{flavour}/{name}"):
                    text = path.read_text()
                    data = yaml.safe_load(text)
                    self.assertIn("jobs", data)
                    self.assertIn("docs/ci.md", text, "the header must point to the setup page")
                    for rel in sorted(set(re.findall(r"\b((?:tools|playbooks)/[\w.-]+)", text))):
                        self.assertTrue((ROOT / rel).is_file(), f"{rel} does not exist")
                        if rel.startswith("tools/") and not rel.endswith(".py"):
                            self.assertTrue((ROOT / rel).stat().st_mode & 0o111, f"{rel} is not executable")

    def test_reference_file_names_a_release(self) -> None:
        lines = [l.strip() for l in (EXAMPLES / "compliant-infra.ref").read_text().splitlines()]
        self.assertTrue([l for l in lines if l and not l.startswith("#")])

    def test_release_bumps_go_through_upgrade(self) -> None:
        # site.yml refuses version changes on clusters: deploy.yml must leave a bump of
        # compliant-infra.ref to upgrade.yml, which runs upgrade.yml and only then site.yml.
        for flavour in FLAVOURS:
            with self.subTest(flavour=flavour):
                deploy = yaml.safe_load((EXAMPLES / flavour / "deploy.yml").read_text())
                self.assertEqual(deploy["jobs"]["deploy"]["needs"], "route")
                upgrade = yaml.safe_load((EXAMPLES / flavour / "upgrade.yml").read_text())
                job = upgrade["jobs"]["upgrade"]
                self.assertEqual(job["needs"], "plan")
                runs = [step.get("run", "") for step in job["steps"]]
                first = next(i for i, r in enumerate(runs) if "playbooks/upgrade.yml" in r)
                then = next(i for i, r in enumerate(runs) if "playbooks/site.yml" in r)
                self.assertLess(first, then)
                self.assertFalse([s for s in job["steps"][first:then + 1] if "if" in s], "site.yml must run only after upgrade.yml succeeded")

    def test_route_and_plan_decisions(self) -> None:
        # (pin before, pin after, event) -> deploy.yml deploys, upgrade.yml upgrades
        cases = {
            ("v0.1.0", "v0.2.0", "push"): ("false", "true"),
            ("v0.1.0", "v0.1.0", "push"): ("true", "false"),
            (None, "v0.1.0", "push"): ("true", "false"),
            ("v0.1.0", "v0.2.0", "workflow_dispatch"): ("true", "true"),
        }
        for flavour in FLAVOURS:
            deploy = self.step_script(flavour, "deploy.yml", "route", "route")
            plan = self.step_script(flavour, "upgrade.yml", "plan", "plan")
            plan = plan.split('echo "upgrade=$upgrade" >> "$GITHUB_OUTPUT"')[0] + 'echo "upgrade=$upgrade" >> "$GITHUB_OUTPUT"\n'  # stop before the clone
            for (old, new, event), expected in cases.items():
                with self.subTest(flavour=flavour, old=old, new=new, event=event), tempfile.TemporaryDirectory() as repo:
                    before = self.commit(repo, old)
                    self.commit(repo, new)
                    self.assertEqual((self.output(repo, deploy, before, event)["deploy"],
                                      self.output(repo, plan, before, event)["upgrade"]), expected)

    @staticmethod
    def step_script(flavour: str, workflow: str, job: str, step_id: str) -> str:
        data = yaml.safe_load((EXAMPLES / flavour / workflow).read_text())
        return next(s["run"] for s in data["jobs"][job]["steps"] if s.get("id") == step_id)

    @staticmethod
    def commit(repo: str, pin: str | None) -> str:
        git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"]
        if not Path(repo, ".git").exists():
            subprocess.run(["git", "init", "-q", repo], check=True)
        ref = Path(repo, "compliant-infra.ref")
        if pin:
            ref.write_text(f"# pinned release\n{pin}\n")
        Path(repo, "inventory").write_text(str(pin))
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", str(pin)], check=True)
        return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

    @staticmethod
    def output(repo: str, script: str, before: str, event: str) -> dict[str, str]:
        with tempfile.NamedTemporaryFile("r") as out:
            env = os.environ | {"BEFORE": before, "GITHUB_EVENT_NAME": event, "GITHUB_OUTPUT": out.name}
            subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=env, check=True, capture_output=True)
            return dict(line.split("=", 1) for line in out.read().split())

    def test_repository_ci_does_not_deploy(self) -> None:
        workflows = list((ROOT / ".github" / "workflows").glob("*.y*ml")) + list((ROOT / ".forgejo" / "workflows").glob("*.y*ml"))
        for path in workflows:
            with self.subTest(workflow=str(path.relative_to(ROOT))):
                match = OPERATOR_ONLY.search(path.read_text())
                self.assertIsNone(match, f"operator-specific setting {match and match.group(0)!r}; move it to examples/ci")


if __name__ == "__main__":
    unittest.main()
