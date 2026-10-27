"""Runs publish-package.yml's "Commit repository update" step against a local bare repository.

The step commits one package's archive and metadata and pushes it to main. Other writers (a
merged pull request, a Release run) push to main while the build runs, so the step must rebase
onto a main that moved and retry, and still refuse a main that changed the same package's files.
Each test clones a bare "origin" (shallow, as actions/checkout does), stages what publish-local
leaves behind, lets another writer move main, and runs the step's script with bash.
"""

from __future__ import annotations

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


def step(name: str) -> dict:
    steps = yaml.safe_load(PUBLISH.read_text())["jobs"]["publish"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def commit_step() -> str:
    return step("Commit repository update")["run"]


def push_step() -> str:
    return step("Push to main")["run"]


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
        self.write(self.work / ARCHIVE, "zstd archive\n")
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

    def run_step(self, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """The publish job's "Commit repository update" step, then (if it passed) "Push to main",
        each with only the env the workflow gives it, as GitHub runs consecutive steps."""
        runner_temp = self.tmp / "runner-temp"
        runner_temp.mkdir(exist_ok=True)
        base = {
            **self.env,
            "PATH": f"{self.bin}{os.pathsep}{self.env['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            **(extra or {}),
        }
        commit = self.run_script(commit_step(), {**base, "PACKAGE": "zstd", "ARCHIVE": ARCHIVE})
        if commit.returncode != 0:
            return commit
        push = self.run_script(push_step(), {**base, "PUSH_TOKEN": "not-a-token"})
        return subprocess.CompletedProcess(
            push.args, push.returncode, commit.stdout + push.stdout, commit.stderr + push.stderr
        )

    def run_script(self, text: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        script = self.tmp / "step.sh"
        script.write_text(text)
        # GitHub's shell for `run:` steps.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
        )

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
        self.assertNotIn("${{", push_step())

    def test_only_the_push_step_gets_the_push_token(self) -> None:
        self.assertNotIn("PUSH_TOKEN", step("Commit repository update").get("env", {}))
        self.assertEqual(step("Push to main")["env"], {"PUSH_TOKEN": "${{ secrets.PACKAGE_PUBLISH_TOKEN }}"})

    def test_a_dry_run_stops_before_the_push(self) -> None:
        self.assertEqual(step("Push to main")["if"], "${{ !inputs.dry_run }}")


if __name__ == "__main__":
    unittest.main()
