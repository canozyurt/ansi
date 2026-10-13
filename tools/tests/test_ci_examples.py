"""The pipeline examples users copy into their deployment repository (examples/ci, docs/ci.md) stay
usable as the tools change, and this repository's own CI never deploys anywhere."""

from __future__ import annotations

import re
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

    def test_repository_ci_does_not_deploy(self) -> None:
        workflows = list((ROOT / ".github" / "workflows").glob("*.y*ml")) + list((ROOT / ".forgejo" / "workflows").glob("*.y*ml"))
        for path in workflows:
            with self.subTest(workflow=str(path.relative_to(ROOT))):
                match = OPERATOR_ONLY.search(path.read_text())
                self.assertIsNone(match, f"operator-specific setting {match and match.group(0)!r}; move it to examples/ci")


if __name__ == "__main__":
    unittest.main()
