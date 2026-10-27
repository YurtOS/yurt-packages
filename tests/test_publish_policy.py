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


class PolicyCase(unittest.TestCase):
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

    def run_check(self, allow_new_build: bool = False, allow_downgrade: bool = False) -> tuple[int, str]:
        out = io.StringIO()
        argv = ["check", "--repo-root", str(self.root), "--manifest", str(self.manifest)]
        argv += ["--allow-new-build", "true" if allow_new_build else "false"]
        if allow_downgrade:
            argv += ["--allow-downgrade", "true"]
        with redirect_stdout(out):
            code = publish_policy.main(argv)
        return code, out.getvalue()


class PolicyTest(PolicyCase):
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
        # release (the newest), another build of its version, and a version above every published one.
        published = json.loads((ROOT / "packages" / "zlib.json").read_text())["versions"]
        newest = max(published, key=lambda entry: publish_policy.release_key(entry["version"], entry["build"]))
        version, build = newest["version"], newest["build"]
        # A yurt_N above every published build of that version, so pkg would rank it newer.
        new_build = "yurt_%d" % (1 + max(
            int(entry["build"][5:]) if entry["build"].startswith("yurt_") and entry["build"][5:].isdigit() else 0
            for entry in published if entry["version"] == version
        ))
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


class DowngradeTest(PolicyCase):
    """A release pkg would rank below the newest published one needs allow_downgrade.

    pkg's resolver orders releases by semver version, then by the N of a `yurt_N` build (any
    other build counts as 0), and skips yanked releases.
    """

    def refused(self, out: str) -> None:
        self.assertIn("::error::", out)
        self.assertIn("allow_downgrade", out)

    def test_openssl_3_5_8_after_4_0_0(self) -> None:
        # The openssl port is at 3.5.8 while 4.0.0 is published.
        self.publish("openssl", ("4.0.0", "1"))
        self.port("openssl", "3.5.8", "1")
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.refused(out)
        self.assertIn("openssl 3.5.8-1 is older than the newest published openssl 4.0.0-1", out)
        self.assertEqual(self.run_check(allow_downgrade=True)[0], 0)

    def test_the_committed_openssl(self) -> None:
        if not (ROOT / "packages" / "openssl.json").exists():
            self.skipTest("openssl is not published")
        newest = max(
            (entry for entry in json.loads((ROOT / "packages" / "openssl.json").read_text())["versions"]),
            key=lambda entry: publish_policy.release_key(entry["version"], entry["build"]),
        )
        major = int(newest["version"].split(".")[0])
        if major == 0:
            self.skipTest("no older major version to try")
        self.port("openssl", f"{major - 1}.99.99", "yurt_0")
        result = subprocess.run(
            [sys.executable, str(TOOL), "check", "--repo-root", str(ROOT), "--manifest", str(self.manifest)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout)
        self.refused(result.stdout)

    def test_versions_compare_as_semver_not_text(self) -> None:
        self.publish("tool", ("1.9.0", "yurt_0"))
        self.port("tool", "1.10.0", "yurt_0")
        self.assertEqual(self.run_check()[0], 0)
        self.publish("tool", ("1.10.0", "yurt_0"))
        self.port("tool", "1.9.0", "yurt_0")
        self.assertEqual(self.run_check()[0], 1)

    def test_a_prerelease_is_older_than_its_release(self) -> None:
        self.publish("go", ("1.28.0", "1"))
        self.port("go", "1.28.0-devel", "1")
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.refused(out)
        self.publish("go", ("1.28.0-devel", "1"))
        self.port("go", "1.28.0", "1")
        self.assertEqual(self.run_check()[0], 0)

    def test_prerelease_identifiers_follow_semver(self) -> None:
        # Numeric identifiers compare as numbers and rank below alphanumeric ones.
        self.publish("tool", ("1.0.0-rc.2", "yurt_0"))
        self.port("tool", "1.0.0-rc.10", "yurt_0")
        self.assertEqual(self.run_check()[0], 0)
        self.publish("tool", ("1.0.0-alpha", "yurt_0"))
        self.port("tool", "1.0.0-1", "yurt_0")
        self.assertEqual(self.run_check()[0], 1)
        self.publish("tool", ("1.0.0-alpha", "yurt_0"))
        self.port("tool", "1.0.0-alpha.1", "yurt_0")
        self.assertEqual(self.run_check()[0], 0)

    def test_a_lower_yurt_build_of_the_same_version(self) -> None:
        self.publish("zsh", ("5.9.0", "yurt_2"))
        self.port("zsh", "5.9.0", "yurt_1")
        code, out = self.run_check(allow_new_build=True)
        self.assertEqual(code, 1)
        self.refused(out)
        self.assertEqual(self.run_check(allow_new_build=True, allow_downgrade=True)[0], 0)
        # Each input covers only its own rule.
        self.assertEqual(self.run_check(allow_downgrade=True)[0], 1)

    def test_a_build_pkg_ranks_equal_is_not_newer(self) -> None:
        # pkg ranks a non-yurt_N build as 0, so 0.1.1-yurt_0 ties with the published 0.1.1-2.
        self.publish("pkg", ("0.1.0", "yurt_0"), ("0.1.1", "1"), ("0.1.1", "2"))
        self.port("pkg", "0.1.1", "yurt_0")
        code, out = self.run_check(allow_new_build=True)
        self.assertEqual(code, 1)
        self.assertIn("is not newer than the newest published pkg 0.1.1-", out)
        self.refused(out)

    def test_a_higher_yurt_build_is_newer(self) -> None:
        self.publish("zlib", ("1.3.1", "1"), ("1.3.1", "yurt_1"))
        self.port("zlib", "1.3.1", "yurt_2")
        self.assertEqual(self.run_check(allow_new_build=True)[0], 0)

    def test_a_newer_version_with_a_lower_build_is_newer(self) -> None:
        self.publish("tool", ("1.0.0", "yurt_5"))
        self.port("tool", "1.1.0", "yurt_0")
        self.assertEqual(self.run_check()[0], 0)

    def test_yanked_releases_do_not_count_as_newest(self) -> None:
        (self.root / "packages" / "openssl.json").write_text(
            json.dumps(
                {
                    "name": "openssl",
                    "versions": [
                        {"version": "3.5.7", "build": "1", "yanked": False},
                        {"version": "4.0.0", "build": "1", "yanked": True},
                    ],
                }
            )
        )
        self.port("openssl", "3.5.8", "1")
        self.assertEqual(self.run_check()[0], 0)
        self.port("openssl", "3.5.6", "1")
        self.assertEqual(self.run_check()[0], 1)

    def test_build_metadata_orders_as_the_semver_crate_does(self) -> None:
        key = publish_policy.semver_key
        self.assertLess(key("1.0.0"), key("1.0.0+a"))
        self.assertLess(key("1.0.0+a"), key("1.0.0+b"))
        self.assertLess(key("1.0.0+a"), key("1.0.0+a.1"))
        # Digit runs: by value, then by length; below alphanumeric identifiers.
        for lower, higher in [("0", "00"), ("00", "1"), ("1", "01"), ("01", "001"), ("002", "10"), ("9", "a")]:
            self.assertLess(key(f"1.0.0+{lower}"), key(f"1.0.0+{higher}"), (lower, higher))
        # Build metadata ranks after the prerelease: a release with metadata is still above
        # every prerelease.
        self.assertLess(key("1.0.0-rc.1+z"), key("1.0.0"))
        self.publish("tool", ("1.0.0+b", "yurt_0"))
        self.port("tool", "1.0.0+a", "yurt_1")
        self.assertEqual(self.run_check()[0], 1)
        self.publish("tool", ("1.0.0", "yurt_0"))
        self.port("tool", "1.0.0+a", "yurt_0")
        self.assertEqual(self.run_check()[0], 0)

    def test_only_what_the_semver_crate_parses_is_a_version(self) -> None:
        key = publish_policy.semver_key
        for bad in ["1.0.0\n", "1\u0663.0.0", "18446744073709551616.0.0", "01.0.0", "1.0.0-01", "1.0", "1.0.0+"]:
            self.assertIsNone(key(bad), repr(bad))
        self.assertIsNotNone(key("18446744073709551615.0.0"))
        self.assertIsNotNone(key("1.0.0-0a.01a+001"))

    def test_yurt_build_numbers_parse_as_rust_u64(self) -> None:
        rank = publish_policy.build_rank
        self.assertEqual(rank("yurt_7"), 7)
        self.assertEqual(rank("yurt_01"), 1)
        self.assertEqual(rank("yurt_+5"), 5)
        self.assertEqual(rank("yurt_18446744073709551615"), 2**64 - 1)
        for zero in ["yurt_18446744073709551616", "yurt_\u0663", "yurt_", "yurt_-1", "yurt_1\n", "1", "yurt_1a"]:
            self.assertEqual(rank(zero), 0, repr(zero))

    def test_a_manifest_version_that_is_not_semver_is_refused(self) -> None:
        self.publish("sudo", ("1.9.17", "yurt_0"))
        self.port("sudo", "1.9.17p2", "yurt_0")
        code, out = self.run_check(allow_downgrade=True)
        self.assertEqual(code, 1)
        self.assertIn("not a semver version", out)


class PolicyWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = yaml.safe_load(PUBLISH.read_text())

    def test_the_input_defaults_to_new_versions_only(self) -> None:
        trigger = self.wf.get("on", self.wf.get(True))
        for name in ["allow_new_build", "allow_downgrade"]:
            allow = trigger["workflow_dispatch"]["inputs"][name]
            self.assertEqual((allow["type"], allow["default"], allow["required"]), ("boolean", False, False), name)

    def test_the_policy_runs_before_anything_is_built(self) -> None:
        # In the build job before anything builds, and again in the publish job (on a fresh
        # checkout, before publish-local and the commit).
        for job, later_steps in [
            ("build", ["Set up Rust", "Build package artifact", "Upload the archive"]),
            ("publish", ["Publish package metadata", "Commit repository update"]),
        ]:
            names = [step.get("name") for step in self.wf["jobs"][job]["steps"]]
            step = self.wf["jobs"][job]["steps"][names.index("Publish only a new version")]
            self.assertEqual(
                step["env"],
                {
                    "PORT": "${{ inputs.package }}",
                    "ALLOW_NEW_BUILD": "${{ inputs.allow_new_build }}",
                    "ALLOW_DOWNGRADE": "${{ inputs.allow_downgrade }}",
                },
                job,
            )
            self.assertNotIn("${{", step["run"])
            self.assertIn('python3 yurt-packages/tools/publish_policy.py check', step["run"])
            self.assertIn('--manifest "yurt-ports/ports/${PORT}/yurt-pack.toml"', step["run"])
            self.assertIn('--allow-new-build "$ALLOW_NEW_BUILD"', step["run"])
            self.assertIn('--allow-downgrade "$ALLOW_DOWNGRADE"', step["run"])
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
