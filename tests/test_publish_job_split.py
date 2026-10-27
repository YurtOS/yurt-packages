"""Publish Package runs as two jobs with least-privilege tokens.

`build` runs the port's build with a read-only token and a GITHUB_TOKEN that cannot write, and
hands over only the archive. `publish` runs on a fresh runner, runs no port or build code, checks
the archive as data, commits only that package's files, and holds the push token in its last
step only. This file pins those properties, and runs the publish job's archive check against
local fixtures.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError as error:  # fail loudly: the workflow tests must never be skipped
    raise ImportError("PyYAML is required for the workflow tests: pip install pyyaml==6.0.2") from error

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / ".github" / "workflows" / "publish-package.yml"


def load() -> dict:
    return yaml.safe_load(PUBLISH.read_text())


class JobSplitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = load()
        self.build = self.wf["jobs"]["build"]
        self.publish = self.wf["jobs"]["publish"]

    def names(self, job: dict) -> list[str]:
        return [step.get("name") for step in job["steps"]]

    def test_two_jobs_publish_after_build(self) -> None:
        self.assertEqual(list(self.wf["jobs"]), ["build", "publish"])
        self.assertEqual(self.publish["needs"], "build")

    def test_no_token_the_workflow_hands_out_can_write(self) -> None:
        self.assertEqual(self.wf["permissions"], {})
        self.assertEqual(self.build["permissions"], {"contents": "read"})
        self.assertEqual(self.publish["permissions"], {"contents": "read"})

    def test_the_push_token_is_in_the_last_publish_step_only(self) -> None:
        text = PUBLISH.read_text()
        self.assertEqual(text.count("secrets.PACKAGE_PUBLISH_TOKEN"), 1)
        last = self.publish["steps"][-1]
        self.assertEqual(last["name"], "Push to main")
        self.assertEqual(last["env"], {"PUSH_TOKEN": "${{ secrets.PACKAGE_PUBLISH_TOKEN }}"})
        self.assertNotIn("PACKAGE_PUBLISH_TOKEN", yaml.safe_dump(self.build))

    def test_the_build_job_reads_with_the_read_token_only(self) -> None:
        dumped = yaml.safe_dump(self.build)
        secrets = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", dumped))
        self.assertEqual(secrets, {"PACKAGE_READ_TOKEN"})

    def test_the_publish_job_runs_no_build_code(self) -> None:
        uses = sorted({step["uses"].split("@")[0] for step in self.publish["steps"] if "uses" in step})
        self.assertEqual(uses, ["actions/checkout", "actions/download-artifact", "dtolnay/rust-toolchain"])
        dumped = yaml.safe_dump(self.publish)
        for build_only in ["package.sh", "port-cache.sh", "install-yurt-sdk", "rust-cache", "curl ", "setup-"]:
            self.assertNotIn(build_only, dumped, build_only)

    def test_no_rust_cache_reaches_the_publish_job(self) -> None:
        cache = next(step for step in self.build["steps"] if step.get("uses", "").startswith("Swatinem/rust-cache@"))
        self.assertEqual(cache["with"], {"shared-key": "publish-package-build"})

    def test_the_archive_is_handed_over_with_its_hash(self) -> None:
        self.assertEqual(
            self.build["outputs"],
            {
                "ports_sha": "${{ steps.ports.outputs.sha }}",
                "sha256": "${{ steps.archive.outputs.sha256 }}",
                "artifact_id": "${{ steps.upload.outputs.artifact-id }}",
            },
        )
        upload = next(step for step in self.build["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        download = next(step for step in self.publish["steps"] if step.get("uses", "").startswith("actions/download-artifact@"))
        self.assertEqual(upload["id"], "upload")
        self.assertEqual(download["with"]["artifact-ids"], "${{ steps.ids.outputs.artifact_id }}")
        ids = next(step for step in self.publish["steps"] if step.get("name") == "Check the artifact id")
        self.assertEqual(ids["env"], {"ARTIFACT_ID": "${{ needs.build.outputs.artifact_id }}"})
        self.assertIn('[[ "$ARTIFACT_ID" =~ ^[0-9]+$ ]]', ids["run"])
        check = next(step for step in self.publish["steps"] if step.get("name") == "Check the archive")
        self.assertEqual(check["env"]["SHA256"], "${{ needs.build.outputs.sha256 }}")
        names = self.names(self.publish)
        self.assertLess(names.index("Check the archive"), names.index("Publish package metadata"))
        metadata = next(step for step in self.publish["steps"] if step.get("name") == "Publish package metadata")
        self.assertEqual(metadata["env"]["ARTIFACT"], "${{ steps.archive.outputs.path }}")

    def test_the_publish_job_reads_the_manifest_at_the_built_commit(self) -> None:
        checkout = next(step for step in self.publish["steps"] if step.get("name") == "Check out yurt-ports")
        self.assertEqual(checkout["with"]["ref"], "${{ needs.build.outputs.ports_sha }}")
        names = self.names(self.publish)
        self.assertLess(names.index("The yurt-ports commit is on main"), names.index("Publish package metadata"))

    def test_the_commit_allows_only_the_package_s_two_paths(self) -> None:
        commit = next(step for step in self.publish["steps"] if step.get("name") == "Commit repository update")
        self.assertEqual(commit["env"], {"PACKAGE": "${{ inputs.package }}", "ARCHIVE": "${{ steps.archive.outputs.rel }}"})
        run = commit["run"]
        self.assertLess(run.index('allowed="$('), run.index("git commit"))

    def test_both_jobs_refuse_a_real_publish_off_main(self) -> None:
        for job in [self.build, self.publish]:
            step = job["steps"][0]
            self.assertEqual(step["name"], "Runs on main only")
            self.assertEqual(step["env"], {"REF": "${{ github.ref }}", "DRY_RUN": "${{ inputs.dry_run }}"})
            self.assertIn('[ "$REF" = refs/heads/main ] || [ "$DRY_RUN" = true ]', step["run"])

    def test_a_dry_run_is_an_input_that_defaults_off(self) -> None:
        trigger = self.wf.get("on", self.wf.get(True))
        dry = trigger["workflow_dispatch"]["inputs"]["dry_run"]
        self.assertEqual((dry["type"], dry["default"], dry["required"]), ("boolean", False, False))

    def test_yurt_greet_is_gone(self) -> None:
        self.assertNotIn("yurt-greet", PUBLISH.read_text())


class CheckArchiveTest(unittest.TestCase):
    """The publish job's "Check the archive" step, run with bash against local fixtures."""

    def setUp(self) -> None:
        steps = load()["jobs"]["publish"]["steps"]
        self.script = next(step for step in steps if step.get("name") == "Check the archive")["run"]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = self.tmp / "work"
        manifest = self.work / "yurt-ports/ports/zstd/yurt-pack.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('name = "zstd"\nversion = "1.5.7"\nbuild = "1"\n')
        self.package = self.tmp / "runner-temp/package"
        self.package.mkdir(parents=True)
        self.archive = self.package / "zstd-1.5.7-1.yurtpkg"
        self.archive.write_bytes(b"archive bytes")
        self.sha = hashlib.sha256(b"archive bytes").hexdigest()

    def run_check(self, port: str = "zstd", sha: str | None = None) -> tuple[int, str, dict[str, str]]:
        script = self.tmp / "step.sh"
        script.write_text(self.script)
        output = self.tmp / "github-output"
        output.write_text("")
        env = {
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(self.tmp / "runner-temp"),
            "GITHUB_OUTPUT": str(output),
            "PORT": port,
            "SHA256": self.sha if sha is None else sha,
        }
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=self.work, env=env, capture_output=True, text=True,
        )
        outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)
        return result.returncode, result.stdout + result.stderr, outputs

    def test_accepts_the_archive_the_manifest_names(self) -> None:
        code, out, outputs = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertEqual(outputs, {"path": str(self.archive), "rel": "artifacts/zstd/1.5.7/zstd-1.5.7-1.yurtpkg"})

    def test_refuses_a_hash_mismatch(self) -> None:
        code, out, outputs = self.run_check(sha="0" * 64)
        self.assertNotEqual(code, 0)
        self.assertIn("sha256 does not match", out)
        self.assertEqual(outputs, {})

    def test_refuses_a_missing_hash(self) -> None:
        self.assertNotEqual(self.run_check(sha="")[0], 0)

    def test_refuses_a_second_file(self) -> None:
        (self.package / ".hidden").write_text("x")
        code, out, _ = self.run_check()
        self.assertNotEqual(code, 0)
        self.assertIn("exactly one regular file", out)

    def test_refuses_a_symlink(self) -> None:
        self.archive.unlink()
        target = self.tmp / "elsewhere"
        target.write_bytes(b"archive bytes")
        self.archive.symlink_to(target)
        self.assertNotEqual(self.run_check()[0], 0)

    def test_refuses_another_name(self) -> None:
        self.archive.rename(self.package / "zlib-1.3.1-1.yurtpkg")
        code, out, _ = self.run_check()
        self.assertNotEqual(code, 0)
        self.assertIn("not zstd-1.5.7-1.yurtpkg", out)

    def test_refuses_a_manifest_for_another_package(self) -> None:
        other = self.work / "yurt-ports/ports/evil/yurt-pack.toml"
        other.parent.mkdir(parents=True)
        other.write_text('name = "zstd"\nversion = "1.5.7"\nbuild = "1"\n')
        code, out, _ = self.run_check(port="evil")
        self.assertNotEqual(code, 0)
        self.assertIn("manifest names zstd", out)

    def test_refuses_an_empty_artifact(self) -> None:
        self.archive.unlink()
        self.assertNotEqual(self.run_check()[0], 0)


if __name__ == "__main__":
    unittest.main()
