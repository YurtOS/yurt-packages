"""Publish Package and CI jobs have time limits, so a hang fails fast instead of holding a runner
for GitHub's 6-hour default."""

from __future__ import annotations

import unittest
from pathlib import Path

try:
    import yaml
except ImportError as error:  # fail loudly: the workflow tests must never be skipped
    raise ImportError("PyYAML is required for the workflow tests: pip install pyyaml==6.0.2") from error

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


class JobTimeoutTest(unittest.TestCase):
    def test_limits(self) -> None:
        # Publish Package: the slowest of its first 39 runs took 19.9 minutes. clang builds LLVM
        # twice (163 to 245 minutes in yurt-clang's release) and gets the 6-hour ceiling. The CI jobs take a few minutes.
        jobs = yaml.safe_load((WORKFLOWS / "publish-package.yml").read_text())["jobs"]
        self.assertEqual(jobs["build"]["timeout-minutes"], "${{ inputs.package == 'clang' && 360 || 45 }}")
        # The publish job builds yurt-repo-ci from cold and commits.
        self.assertEqual(jobs["publish"]["timeout-minutes"], 20)
        # The push job runs git only.
        self.assertEqual(jobs["push"]["timeout-minutes"], 10)
        for name, job in yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["jobs"].items():
            self.assertEqual(job["timeout-minutes"], 10, name)


if __name__ == "__main__":
    unittest.main()
