"""Static checks of .github/workflows/release.yml, and of how publish-package.yml hands over to it.

Clients pin the signer `release.yml@refs/heads/main` (issuer GitHub Actions), so the file
name, the main-only gate and the permissions are part of the contract. What only a hosted
run proves (the Fulcio certificate's identity, the push) is listed in the README.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError as error:  # fail loudly: the static checks must never be skipped
    raise ImportError("PyYAML is required for the workflow tests: pip install pyyaml==6.0.2") from error

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
PUBLISH = WORKFLOWS / "publish-package.yml"
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = load(RELEASE)
        self.job = self.wf["jobs"]["sign"]
        self.steps = self.job["steps"]

    def run_text(self) -> str:
        return "\n".join(step.get("run", "") for step in self.steps)

    def test_only_job_is_sign(self) -> None:
        self.assertEqual(list(self.wf["jobs"]), ["sign"])

    def test_triggers(self) -> None:
        on = self.wf[True]  # PyYAML reads `on:` as True
        self.assertEqual(set(on), {"workflow_dispatch", "workflow_run"})
        self.assertIsNone(on["workflow_dispatch"])
        self.assertEqual(
            on["workflow_run"],
            {"workflows": ["Publish Package"], "types": ["completed"], "branches": ["main"]},
        )
        names = {load(path)["name"] for path in WORKFLOWS.glob("*.yml")}
        self.assertIn("Publish Package", names)

    def test_job_runs_only_on_main_of_this_repository(self) -> None:
        condition = " ".join(self.job["if"].split())
        for clause in [
            "github.repository == 'YurtOS/yurt-packages'",
            "github.ref == 'refs/heads/main'",
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.head_branch == 'main'",
        ]:
            self.assertIn(clause, condition)
        checkout = self.steps[0]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"], {"ref": "main"})

    def test_minimum_permissions(self) -> None:
        self.assertEqual(self.wf["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "write", "id-token": "write"})

    def test_no_secrets(self) -> None:
        self.assertNotIn("secrets.", RELEASE.read_text())

    def test_actions_are_pinned_by_sha(self) -> None:
        uses = [step["uses"] for step in self.steps if "uses" in step]
        self.assertGreaterEqual(len(uses), 3)
        for value in uses:
            self.assertRegex(value, PINNED)

    def test_cosign_version_is_pinned(self) -> None:
        installer = next(step for step in self.steps if step.get("uses", "").startswith("sigstore/cosign-installer@"))
        self.assertRegex(installer["with"]["cosign-release"], r"^v\d+\.\d+\.\d+$")

    def test_serialized_with_the_other_publishers(self) -> None:
        self.assertEqual(self.wf["concurrency"], {"group": "publish-package", "cancel-in-progress": False})
        self.assertEqual(load(PUBLISH)["concurrency"]["group"], "publish-package")

    def test_step_order(self) -> None:
        text = self.run_text()
        order = [
            "repo_signing.py regenerate-index",
            "repo_signing.py plan",
            'cosign sign-blob --yes --bundle "${archive}.bundle" "$archive"',
            "cosign sign-blob --yes --bundle index.json.bundle index.json",
            "repo_signing.py check --strict --cosign cosign",
            "repo_signing.py prune-legacy",
            "git push origin HEAD:main",
        ]
        positions = [text.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))

    def test_strict_check_has_no_legacy_allowance(self) -> None:
        check = next(step for step in self.steps if "repo_signing.py check" in step.get("run", ""))
        self.assertNotIn("--legacy", check["run"])
        self.assertIn("--strict", check["run"])

    def test_no_expression_reaches_a_shell(self) -> None:
        for step in self.steps:
            self.assertNotIn("${{", step.get("run", ""), step.get("name"))


class PublishPackageHandoverTest(unittest.TestCase):
    def setUp(self) -> None:
        steps = load(PUBLISH)["jobs"]["publish"]["steps"]
        self.commit = next(step for step in steps if step.get("name") == "Commit repository update")["run"]

    def test_never_stages_a_bundle_or_the_index(self) -> None:
        adds = [line.strip() for line in self.commit.splitlines() if line.strip().startswith("git add")]
        self.assertEqual(adds, ["git add -- artifacts packages ':(exclude)*.bundle'"])
        self.assertIn("grep -E '\\.bundle$|^index\\.json$'", self.commit)

    def test_guard_runs_before_the_commit(self) -> None:
        self.assertLess(self.commit.index("refusing to commit a bundle"), self.commit.index("git commit"))


if __name__ == "__main__":
    unittest.main()
