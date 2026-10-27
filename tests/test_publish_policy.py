"""tools/publish_policy.py, and where publish-package.yml runs it.

Archives are committed to git, so a publish must add a new version: the same version and build
is refused, and another build of a published version only with the allow_new_build input.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

try:
    import yaml
except ImportError as error:  # fail loudly: the workflow tests must never be skipped
    raise ImportError("PyYAML is required for the workflow tests: pip install pyyaml==6.0.2") from error

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import publish_policy  # noqa: E402

PUBLISH = ROOT / ".github" / "workflows" / "publish-package.yml"
TOOL = ROOT / "tools" / "publish_policy.py"


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "packages").mkdir()
        self.manifest = self.root / "yurt-pack.toml"

    def publish(self, name: str, *releases: tuple[str, str]) -> None:
        versions = [
            {"version": version, "build": build, "url": f"artifacts/{name}/{version}/{name}-{version}-{build}.yurtpkg"}
            for version, build in releases
        ]
        (self.root / "packages" / f"{name}.json").write_text(json.dumps({"name": name, "versions": versions}))

    def port(self, name: str, version: str, build: str) -> None:
        self.manifest.write_text(f'name = "{name}"\nversion = "{version}"\nbuild = "{build}"\nplatform = "wasm32-yurt-linux-musl"\n')

    def run_check(self, allow_new_build: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        argv = ["check", "--repo-root", str(self.root), "--manifest", str(self.manifest)]
        argv += ["--allow-new-build", "true" if allow_new_build else "false"]
        with redirect_stdout(out):
            code = publish_policy.main(argv)
        return code, out.getvalue()

    def test_a_new_package_publishes(self) -> None:
        self.port("zstd", "1.5.7", "yurt_0")
        self.assertEqual(self.run_check(), (0, "zstd 1.5.7-yurt_0: a new package\n"))

    def test_a_new_version_publishes(self) -> None:
        self.publish("zlib", ("1.3.1", "1"), ("1.3.1", "yurt_1"))
        self.port("zlib", "1.3.2", "yurt_0")
        self.assertEqual(self.run_check(), (0, "zlib 1.3.2-yurt_0: a new version\n"))

    def test_the_same_version_and_build_is_refused(self) -> None:
        self.publish("zlib", ("1.3.1", "yurt_1"))
        self.port("zlib", "1.3.1", "yurt_1")
        for allow in [False, True]:
            code, out = self.run_check(allow)
            self.assertEqual(code, 1)
            self.assertIn("::error::zlib 1.3.1-yurt_1 is already published. A release is never replaced", out)

    def test_another_build_of_a_published_version_needs_the_input(self) -> None:
        self.publish("zsh", ("5.9.0", "yurt_0"), ("5.9.0", "yurt_1"), ("5.8.0", "yurt_9"))
        self.port("zsh", "5.9.0", "yurt_2")
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("::error::zsh 5.9.0 is already published (builds: yurt_0, yurt_1)", out)
        self.assertIn("dispatch again with allow_new_build", out)
        code, out = self.run_check(allow_new_build=True)
        self.assertEqual(code, 0, out)
        self.assertIn("zsh 5.9.0-yurt_2: a new build of zsh 5.9.0 (allow_new_build", out)

    def test_a_yanked_release_still_counts(self) -> None:
        # Yanking hides a release from resolution; its archive is still in git.
        (self.root / "packages" / "gh.json").write_text(
            json.dumps({"name": "gh", "versions": [{"version": "2.102.0", "build": "yurt_0", "yanked": True}]})
        )
        self.port("gh", "2.102.0", "yurt_1")
        self.assertEqual(self.run_check()[0], 1)

    def test_unreadable_inputs_fail_closed(self) -> None:
        self.port("zlib", "1.3.1", "yurt_1")
        (self.root / "packages" / "zlib.json").write_text("{not json")
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("::error::reading", out)
        self.manifest.write_text('name = "zlib"\nversion = "1.3.1"\n')
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("has no build", out)

    def test_the_committed_repository(self) -> None:
        # Against this checkout's own packages/zlib.json, whatever it holds by then: a published
        # release, another build of its version, and a version above every published one.
        published = json.loads((ROOT / "packages" / "zlib.json").read_text())["versions"]
        version, build = published[-1]["version"], published[-1]["build"]
        new_build = build + "-new"
        new_version = ".".join(str(int(part) + 1) for part in max(
            tuple(int(p) for p in entry["version"].split("-")[0].split(".")) for entry in published
        ))

        def check(version: str, build: str, allow: bool) -> int:
            self.port("zlib", version, build)
            return subprocess.run(
                [sys.executable, str(TOOL), "check", "--repo-root", str(ROOT), "--manifest", str(self.manifest),
                 "--allow-new-build", "true" if allow else "false"],
                capture_output=True,
            ).returncode

        self.assertEqual(check(version, build, True), 1)
        self.assertEqual(check(version, new_build, False), 1)
        self.assertEqual(check(version, new_build, True), 0)
        self.assertEqual(check(new_version, "yurt_0", False), 0)


class PolicyWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = yaml.safe_load(PUBLISH.read_text())
        self.steps = self.wf["jobs"]["publish"]["steps"]
        self.names = [step.get("name") for step in self.steps]

    def test_the_input_defaults_to_new_versions_only(self) -> None:
        trigger = self.wf.get("on", self.wf.get(True))
        allow = trigger["workflow_dispatch"]["inputs"]["allow_new_build"]
        self.assertEqual((allow["type"], allow["default"], allow["required"]), ("boolean", False, False))

    def test_the_policy_runs_before_anything_is_built(self) -> None:
        # In the build job before anything builds, and again in the publish job (on a fresh
        # checkout, before publish-local and the commit).
        for job, later_steps in [
            ("build", ["Set up Rust", "Build package artifact", "Upload the archive"]),
            ("publish", ["Publish package metadata", "Commit repository update", "Push to main"]),
        ]:
            names = [step.get("name") for step in self.wf["jobs"][job]["steps"]]
            step = self.wf["jobs"][job]["steps"][names.index("Publish only a new version")]
            self.assertEqual(step["env"], {"PORT": "${{ inputs.package }}", "ALLOW_NEW_BUILD": "${{ inputs.allow_new_build }}"})
            self.assertNotIn("${{", step["run"])
            self.assertIn('python3 yurt-packages/tools/publish_policy.py check', step["run"])
            self.assertIn('--manifest "yurt-ports/ports/${PORT}/yurt-pack.toml"', step["run"])
            self.assertIn('--allow-new-build "$ALLOW_NEW_BUILD"', step["run"])
            position = names.index("Publish only a new version")
            self.assertGreater(position, names.index("The yurt-ports commit is on main"), job)
            for later in later_steps:
                self.assertLess(position, names.index(later), (job, later))

    def test_publish_local_still_rejects_an_existing_release(self) -> None:
        publish = self.wf["jobs"]["publish"]["steps"]
        run = next(step for step in publish if step.get("name") == "Publish package metadata")["run"]
        self.assertIn("--reject-existing", run)


if __name__ == "__main__":
    unittest.main()
