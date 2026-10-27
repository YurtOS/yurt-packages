"""Runs publish-package.yml's commit, hand-over and push steps against a local bare repository.

The publish job's "Commit repository update" step commits one package's archive and metadata
and bundles the commit. The push job, on a fresh checkout of the run's commit, verifies the
bundle ("Verify the commit") and pushes it to main ("Push to main"). Other writers (a
merged pull request, a Release run) push to main while the build runs, so the step must rebase
onto a main that moved and retry, and still refuse a main that changed the same package's files.
Each test clones a bare "origin" (shallow, as actions/checkout does), stages what publish-local
leaves behind, lets another writer move main, and runs the step's script with bash.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

try:
    import yaml
except ImportError as error:  # fail loudly: the workflow tests must never be skipped
    raise ImportError("PyYAML is required for the workflow tests: pip install pyyaml==6.0.2") from error

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / ".github" / "workflows" / "publish-package.yml"
REAL_GIT = shutil.which("git")
ARCHIVE = "artifacts/zstd/1.5.7/zstd-1.5.7-1.yurtpkg"
ARCHIVE_TEXT = "zstd archive\n"
ARCHIVE_SHA256 = hashlib.sha256(ARCHIVE_TEXT.encode()).hexdigest()


def step(name: str, job: str = "publish") -> dict:
    steps = yaml.safe_load(PUBLISH.read_text())["jobs"][job]["steps"]
    return next(step for step in steps if step.get("name") == name)


def commit_step() -> str:
    return step("Commit repository update")["run"]


def verify_step() -> str:
    return step("Verify the commit", "push")["run"]


def push_step() -> str:
    return step("Push to main", "push")["run"]


class PublishPushTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(REAL_GIT)
        self.tmp = Path(tempfile.mkdtemp(prefix="publish-push-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        empty = self.tmp / "gitconfig"
        empty.write_text("")
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(empty),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "LC_ALL": "C",
        }
        self.origin = self.tmp / "origin.git"
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin))
        seed = self.tmp / "seed"
        self.git("clone", "-q", str(self.origin), str(seed))
        self.write(seed / "index.json", json.dumps({"index_version": 39}) + "\n")
        self.write(seed / "index.json.bundle", "signed index\n")
        self.write(seed / "packages/zlib.json", '{"name": "zlib"}\n')
        self.write(seed / "artifacts/zlib/1.3.1/zlib-1.3.1-1.yurtpkg", "zlib\n")
        self.write(seed / "artifacts/zlib/1.3.1/zlib-1.3.1-1.yurtpkg.bundle", "signed zlib\n")
        self.git("add", "-A", cwd=seed)
        self.git("commit", "-q", "-m", "seed", cwd=seed)
        self.git("push", "-q", "origin", "HEAD:main", cwd=seed)
        self.checkout()
        # Another writer's clone of origin.
        self.other = self.tmp / "other"
        self.git("clone", "-q", str(self.origin), str(self.other))
        # Shims: base64 without GNU's -w0 on macOS, a sleep that returns at once, and a git
        # whose pushes first run $PUSH_HOOK (another writer moving main in the window between
        # the step's fetch and its push).
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.shim(
            "base64",
            "exec python3 -c 'import base64, sys; sys.stdout.write(base64.b64encode(sys.stdin.buffer.read()).decode())'",
        )
        self.shim("sleep", "exit 0")
        self.shim(
            "git",
            f"""
            for arg in "$@"; do
              if [ "$arg" = push ] && [ -n "${{PUSH_HOOK:-}}" ]; then "$PUSH_HOOK"; break; fi
            done
            exec {REAL_GIT} "$@"
            """,
        )

    def checkout(self) -> None:
        """The run's checkout (shallow, like actions/checkout's default) and what publish-local
        leaves in it: the archive and metadata (committed), plus a rewritten index.json and
        placeholder bundles (never committed)."""
        self.start = self.head()
        self.work = self.tmp / "work"
        if self.work.exists():
            shutil.rmtree(self.work)
        self.git("clone", "-q", "--depth", "1", f"file://{self.origin}", str(self.work))
        self.write(self.work / ARCHIVE, ARCHIVE_TEXT)
        self.write(self.work / f"{ARCHIVE}.bundle", "local test fixture bundle\n")
        self.write(self.work / "packages/zstd.json", '{"name": "zstd", "versions": ["1.5.7-1"]}\n')
        self.write(self.work / "index.json", json.dumps({"index_version": 40}) + "\n")
        self.write(self.work / "index.json.bundle", "local test fixture bundle\n")

    # -- helpers ---------------------------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            [REAL_GIT, *args], cwd=cwd, env=self.env, check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    def head(self) -> str:
        return self.git("--git-dir", str(self.origin), "rev-parse", "main")

    @staticmethod
    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def shim(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + textwrap.dedent(body).strip() + "\n")
        path.chmod(0o755)

    def move_main(self, files: dict[str, str | None], message: str = "another writer") -> str:
        """Push a commit from another clone: path -> new text, or None to delete."""
        self.git("pull", "-q", "--ff-only", "origin", "main", cwd=self.other)
        for rel, text in files.items():
            if text is None:
                self.git("rm", "-q", rel, cwd=self.other)
            else:
                self.write(self.other / rel, text)
                self.git("add", rel, cwd=self.other)
        self.git("commit", "-q", "-m", message, cwd=self.other)
        self.git("push", "-q", "origin", "HEAD:main", cwd=self.other)
        return self.head()

    def push_hook(self, times: int) -> dict[str, str]:
        """A hook that moves main (an unrelated index change) before each of the first `times` pushes."""
        counter = self.tmp / "hook-count"
        counter.write_text("0")
        hook = self.tmp / "hook.sh"
        hook.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                set -e
                n=$(cat {counter})
                [ "$n" -lt {times} ] || exit 0
                echo $((n + 1)) > {counter}
                cd {self.other}
                {REAL_GIT} pull -q --ff-only origin main
                echo "move $n" > index.json.bundle
                {REAL_GIT} commit -q -am "race $n"
                {REAL_GIT} push -q origin HEAD:main
                """
            )
        )
        hook.chmod(0o755)
        return {"PUSH_HOOK": str(hook)}

    def run_step(
        self,
        extra: dict[str, str] | None = None,
        tamper=None,
        sha256: str = ARCHIVE_SHA256,
    ) -> subprocess.CompletedProcess[str]:
        """The publish job's "Commit repository update" step; then, on a fresh runner, the push
        job's "Verify the commit" and "Push to main", each step with only the env the workflow
        gives it. `tamper(work)` stands in for a publish job that changes its commit after the
        commit step: it runs on the publish clone, and the hand-over bundle is made again."""
        publish_temp = self.tmp / "publish-temp"
        publish_temp.mkdir(exist_ok=True)
        outputs = publish_temp / "github-output"
        outputs.write_text("")
        base = {
            **self.env,
            "PATH": f"{self.bin}{os.pathsep}{self.env['PATH']}",
            **(extra or {}),
        }
        commit = self.run_script(
            commit_step(),
            {**base, "RUNNER_TEMP": str(publish_temp), "GITHUB_OUTPUT": str(outputs), "PACKAGE": "zstd", "ARCHIVE": ARCHIVE},
            self.work,
        )
        bundle = publish_temp / "handover" / "commit.bundle"
        if commit.returncode != 0 or not bundle.exists():
            return commit
        self.assertIn("committed=true", outputs.read_text())
        if tamper is not None:
            tamper(self.work)
            bundle.unlink()
            self.git("bundle", "create", str(bundle), "HEAD", f"^{self.start}", cwd=self.work)
        # The push job: a fresh runner, a checkout of the run's commit, the bundle downloaded.
        push_temp = self.tmp / "push-temp"
        (push_temp / "handover").mkdir(parents=True, exist_ok=True)
        shutil.copy(bundle, push_temp / "handover" / "commit.bundle")
        clone = self.tmp / "push-clone"
        if clone.exists():
            shutil.rmtree(clone)
        self.git("init", "-q", str(clone))
        self.git("remote", "add", "origin", f"file://{self.origin}", cwd=clone)
        self.git("fetch", "-q", "--depth", "1", "origin", self.start, cwd=clone)
        self.git("checkout", "-q", "-B", "main", "FETCH_HEAD", cwd=clone)
        env = {**base, "RUNNER_TEMP": str(push_temp)}
        verify = self.run_script(
            verify_step(), {**env, "PACKAGE": "zstd", "VERSION": "1.5.7", "BUILD": "1", "SHA256": sha256}, clone
        )
        out, err = commit.stdout + verify.stdout, commit.stderr + verify.stderr
        if verify.returncode != 0:
            return subprocess.CompletedProcess(verify.args, verify.returncode, out, err)
        push = self.run_script(push_step(), {**env, "PUSH_TOKEN": "not-a-token"}, clone)
        return subprocess.CompletedProcess(push.args, push.returncode, out + push.stdout, err + push.stderr)

    def run_script(self, text: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
        script = self.tmp / "step.sh"
        script.write_text(text)
        # GitHub's shell for `run:` steps.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def assert_not_pushed(self, result: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertEqual(self.head(), self.start)

    def assert_published_on(self, parent: str, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.head()
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", f"{head}^"), parent)
        files = self.git("--git-dir", str(self.origin), "diff", "--name-only", parent, head).splitlines()
        self.assertEqual(sorted(files), sorted([ARCHIVE, "packages/zstd.json"]))

    def assert_refused(self, head: str, result: subprocess.CompletedProcess[str], path: str) -> None:
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("main changed this package's files", result.stdout)
        self.assertIn(path, result.stdout)
        self.assertEqual(self.head(), head)

    # -- tests -----------------------------------------------------------------------------

    def test_pushes_when_main_did_not_move(self) -> None:
        self.assert_published_on(self.start, self.run_step())

    # -- the push job trusts only what it verifies in the bundle ---------------------------

    def amend(self, files: dict[str, str | None]):
        def tamper(work: Path) -> None:
            for rel, text in files.items():
                if text is None:
                    self.git("rm", "-q", rel, cwd=work)
                else:
                    self.write(work / rel, text)
                    self.git("add", rel, cwd=work)
            self.git("commit", "-q", "--amend", "--no-edit", cwd=work)

        return tamper

    def test_the_push_job_refuses_a_commit_that_changes_another_path(self) -> None:
        result = self.run_step(tamper=self.amend({"packages/zlib.json": '{"name": "zlib", "tampered": true}\n'}))
        self.assert_not_pushed(result, "packages/zlib.json")

    def test_the_push_job_refuses_an_index_or_bundle(self) -> None:
        result = self.run_step(tamper=self.amend({"index.json": "{}\n"}))
        self.assert_not_pushed(result, "index.json")

    def test_the_push_job_refuses_an_archive_the_build_did_not_make(self) -> None:
        result = self.run_step(tamper=self.amend({ARCHIVE: "other bytes\n"}))
        self.assert_not_pushed(result, "sha256")

    def test_the_push_job_refuses_the_build_s_hash_on_other_bytes(self) -> None:
        self.assert_not_pushed(self.run_step(sha256="0" * 64), "sha256")

    def test_the_push_job_refuses_a_second_commit(self) -> None:
        def tamper(work: Path) -> None:
            self.write(work / "README.md", "more\n")
            self.git("add", "README.md", cwd=work)
            self.git("commit", "-q", "-m", "extra", cwd=work)

        self.assert_not_pushed(self.run_step(tamper=tamper), "one commit on")

    def test_the_push_job_refuses_a_commit_on_another_parent(self) -> None:
        moved = self.move_main({"README.md": "docs\n"})

        def tamper(work: Path) -> None:
            # Drop what publish-local left uncommitted, then move the commit onto the new main.
            self.git("reset", "-q", "--hard", cwd=work)
            self.git("fetch", "-q", "origin", "main", cwd=work)
            self.git("rebase", "-q", "FETCH_HEAD", cwd=work)

        result = self.run_step(tamper=tamper)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("one commit on", result.stdout + result.stderr)
        self.assertEqual(self.head(), moved)

    def test_the_push_job_refuses_an_archive_that_is_a_symlink(self) -> None:
        def tamper(work: Path) -> None:
            (work / ARCHIVE).unlink()
            (work / ARCHIVE).symlink_to("../../../packages/zstd.json")
            self.git("add", ARCHIVE, cwd=work)
            self.git("commit", "-q", "--amend", "--no-edit", cwd=work)

        self.assert_not_pushed(self.run_step(tamper=tamper), "regular file")

    def test_the_push_job_refuses_metadata_that_is_a_symlink(self) -> None:
        def tamper(work: Path) -> None:
            (work / "packages/zstd.json").unlink()
            (work / "packages/zstd.json").symlink_to("../index.json")
            self.git("add", "packages/zstd.json", cwd=work)
            self.git("commit", "-q", "--amend", "--no-edit", cwd=work)

        self.assert_not_pushed(self.run_step(tamper=tamper), "packages/zstd.json is not a regular file")

    def test_the_push_job_refuses_executable_metadata_or_archive(self) -> None:
        for rel in ["packages/zstd.json", ARCHIVE]:
            with self.subTest(rel=rel):
                self.setUp()

                def tamper(work: Path) -> None:
                    self.git("update-index", "--chmod=+x", rel, cwd=work)
                    self.git("commit", "-q", "--amend", "--no-edit", cwd=work)

                self.assert_not_pushed(self.run_step(tamper=tamper), f"{rel} is not a regular file")

    def test_the_push_job_refuses_a_version_the_manifest_does_not_name(self) -> None:
        # The build's bytes under a version that would get past the publish policy.
        other = "artifacts/zstd/99.0/zstd-99.0-1.yurtpkg"

        def tamper(work: Path) -> None:
            self.write(work / other, ARCHIVE_TEXT)
            self.git("add", other, cwd=work)
            self.git("rm", "-q", "--cached", ARCHIVE, cwd=work)
            self.git("commit", "-q", "--amend", "--no-edit", cwd=work)

        self.assert_not_pushed(self.run_step(tamper=tamper), other)

    def test_the_push_job_refuses_an_archive_outside_its_version_directory(self) -> None:
        def tamper(work: Path) -> None:
            self.git("mv", ARCHIVE, "artifacts/zstd/1.5.7/zstd-9.9-1.yurtpkg", cwd=work)
            self.git("commit", "-q", "--amend", "--no-edit", cwd=work)

        self.assert_not_pushed(self.run_step(tamper=tamper), "artifacts/zstd/1.5.7/zstd-9.9-1.yurtpkg")

    def test_rebases_onto_a_main_that_moved_elsewhere(self) -> None:
        # The zstd case: a pull request merged during the build, and Release signed another
        # package's archive.
        moved = self.move_main(
            {
                "README.md": "docs\n",
                "index.json": json.dumps({"index_version": 40}) + "\n",
                "artifacts/zlib/1.3.1/zlib-1.3.1-1.yurtpkg.bundle": "re-signed zlib\n",
            }
        )
        self.assert_published_on(moved, self.run_step())

    def test_a_bundle_signed_for_another_release_of_the_package_is_not_a_conflict(self) -> None:
        moved = self.move_main({"artifacts/zstd/1.5.6/zstd-1.5.6-1.yurtpkg.bundle": "signed older zstd\n"})
        self.assert_published_on(moved, self.run_step())

    def test_retries_when_main_moves_between_fetch_and_push(self) -> None:
        result = self.run_step(self.push_hook(times=2))
        self.assertIn("(attempt 3)", result.stdout)
        raced = self.git("--git-dir", str(self.origin), "rev-parse", "main^")
        self.assertEqual(self.git("--git-dir", str(self.origin), "log", "-1", "--format=%s", raced), "race 1")
        self.assert_published_on(raced, result)

    def test_gives_up_when_main_keeps_moving(self) -> None:
        result = self.run_step(self.push_hook(times=100))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("5 push attempts failed (main kept moving", result.stdout)
        self.assertEqual(self.git("--git-dir", str(self.origin), "log", "-1", "--format=%s", "main"), "race 4")

    def test_refuses_a_main_that_changed_the_package_metadata(self) -> None:
        moved = self.move_main({"packages/zstd.json": '{"name": "zstd", "other": true}\n'}, "another zstd publish")
        self.assert_refused(moved, self.run_step(), "packages/zstd.json")

    def test_refuses_a_main_that_removed_the_package(self) -> None:
        # zstd was published before this run's checkout, and removed from main during it.
        self.move_main({"packages/zstd.json": '{"name": "zstd"}\n', "artifacts/zstd/1.5.6/zstd-1.5.6-1.yurtpkg": "old\n"})
        self.checkout()
        moved = self.move_main(
            {"packages/zstd.json": None, "artifacts/zstd/1.5.6/zstd-1.5.6-1.yurtpkg": None}, "remove zstd"
        )
        self.assert_refused(moved, self.run_step(), "packages/zstd.json")

    def test_refuses_a_main_that_added_another_archive_of_the_package(self) -> None:
        moved = self.move_main({"artifacts/zstd/1.5.7/zstd-1.5.7-2.yurtpkg": "other build\n"})
        self.assert_refused(moved, self.run_step(), "artifacts/zstd/1.5.7/zstd-1.5.7-2.yurtpkg")

    def test_refuses_a_main_that_signed_this_archive_path(self) -> None:
        moved = self.move_main({f"{ARCHIVE}.bundle": "a bundle for the same path\n"})
        self.assert_refused(moved, self.run_step(), f"{ARCHIVE}.bundle")

    def test_refuses_when_main_moves_onto_the_package_between_fetch_and_push(self) -> None:
        # First push attempt loses the race to a commit that touches packages/zstd.json.
        hook = self.tmp / "hook.sh"
        flag = self.tmp / "hook-done"
        hook.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                set -e
                [ ! -e {flag} ] || exit 0
                touch {flag}
                cd {self.other}
                {REAL_GIT} pull -q --ff-only origin main
                echo '{{"name": "zstd", "raced": true}}' > packages/zstd.json
                {REAL_GIT} add packages/zstd.json
                {REAL_GIT} commit -q -m "racing zstd publish"
                {REAL_GIT} push -q origin HEAD:main
                """
            )
        )
        hook.chmod(0o755)
        result = self.run_step({"PUSH_HOOK": str(hook)})
        self.assert_refused(self.head(), result, "packages/zstd.json")
        self.assertEqual(self.git("--git-dir", str(self.origin), "log", "-1", "--format=%s", "main"), "racing zstd publish")

    def test_a_push_that_landed_but_reported_failure_is_not_a_conflict(self) -> None:
        # The first push reaches main, then the client sees an error (a dropped connection).
        self.shim(
            "git",
            f"""
            for arg in "$@"; do
              if [ "$arg" = push ] && [ ! -e {self.tmp}/pushed-once ]; then
                touch {self.tmp}/pushed-once
                {REAL_GIT} "$@" || exit 1
                exit 1
              fi
            done
            exec {REAL_GIT} "$@"
            """,
        )
        result = self.run_step()
        self.assertIn("main already has", result.stdout)
        self.assert_published_on(self.start, result)

    def test_a_package_whose_name_extends_this_one_is_not_a_conflict(self) -> None:
        # Pathspecs match whole components: artifacts/zstd does not cover artifacts/zstd-extra.
        moved = self.move_main(
            {"packages/zstd-extra.json": "{}\n", "artifacts/zstd-extra/1.0.0/zstd-extra-1.0.0-1.yurtpkg": "x\n"}
        )
        self.assert_published_on(moved, self.run_step())

    def test_refuses_to_commit_another_package_s_files(self) -> None:
        # Only the archive and packages/zstd.json may be staged.
        self.write(self.work / "packages/zlib.json", '{"name": "zlib", "tampered": true}\n')
        result = self.run_step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the publish must change exactly", result.stdout)
        self.assertIn("packages/zlib.json", result.stdout)
        self.assertEqual(self.head(), self.start)

    def test_refuses_an_archive_other_than_the_checked_one(self) -> None:
        (self.work / ARCHIVE).unlink()
        self.write(self.work / "artifacts/zstd/1.5.7/zstd-1.5.7-9.yurtpkg", "another build\n")
        result = self.run_step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the publish must change exactly", result.stdout)
        self.assertEqual(self.head(), self.start)

    def test_nothing_to_commit_pushes_nothing(self) -> None:
        for rel in [ARCHIVE, "packages/zstd.json"]:
            (self.work / rel).unlink()
        result = self.run_step()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No package repository changes to commit.", result.stdout)
        self.assertEqual(self.head(), self.start)


class PublishPushStaticTest(unittest.TestCase):
    def test_no_expression_reaches_the_shell(self) -> None:
        self.assertNotIn("${{", commit_step())
        self.assertNotIn("${{", verify_step())
        self.assertNotIn("${{", push_step())

    def test_only_the_push_step_gets_the_push_token(self) -> None:
        self.assertNotIn("PUSH_TOKEN", step("Commit repository update").get("env", {}))
        self.assertEqual(
            step("Verify the commit", "push")["env"],
            {
                "PACKAGE": "${{ inputs.package }}",
                "VERSION": "${{ needs.build.outputs.version }}",
                "BUILD": "${{ needs.build.outputs.build }}",
                "SHA256": "${{ needs.build.outputs.sha256 }}",
            },
        )
        self.assertEqual(step("Push to main", "push")["env"], {"PUSH_TOKEN": "${{ secrets.PACKAGE_PUBLISH_TOKEN }}"})

    def test_a_dry_run_stops_before_the_push(self) -> None:
        # The push job still verifies the commit on a dry run; only the push is skipped.
        self.assertEqual(step("Push to main", "push")["if"], "${{ !inputs.dry_run }}")
        self.assertNotIn("if", step("Verify the commit", "push"))


if __name__ == "__main__":
    unittest.main()
