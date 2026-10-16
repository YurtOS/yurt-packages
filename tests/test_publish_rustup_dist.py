"""Tests for tools/publish_rustup_dist.sh using a gh shim, a local bare Pages remote and stub tooling."""
import base64
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "publish_rustup_dist.sh"
RELEASE, DATE, VERSION = "rust-v1.99.0-0.0.1", "2026-10-20", "1.99.0"
TAG = f"rustup-{VERSION}-{DATE}"
PUBLISHED = f"dist/{DATE}/channel-rust-{VERSION}.toml"
AUTH_HEADER = "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:secret-token").decode()
MUTATING = ("release delete", "release create", "release upload")

GH_SHIM = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
echo "${GH_TOKEN:-}" >> "$GH_LOG.tok"
case "$1 $2" in
  "release download")
    while [ $# -gt 0 ]; do [ "$1" = -D ] && dir=$2; shift; done
    echo '{"release": {"rust_version": "1.99.0", "revision": "'"${FAKE_REVISION:-0.0.1}"'"}}' > "$dir/release-manifest.json"
    [ -z "${FAKE_ON_DOWNLOAD:-}" ] || bash "$FAKE_ON_DOWNLOAD" ;;
  "release view") [ -e "$GH_STATE/$3" ] ;;
  "release delete") rm -f "$GH_STATE/$3" ;;
  "release create") touch "$GH_STATE/$3" ;;
  "release upload") [ -z "${FAKE_UPLOAD_FAIL:-}" ] ;;
esac
"""

EMITTER = """import argparse, json, pathlib, sys
p = argparse.ArgumentParser()
for a in ("release-dir", "site", "components", "date", "base-url", "producer-host"):
    p.add_argument("--" + a, required=True)
a = p.parse_args()
import os
open(pathlib.Path(a.site).parent / "emit.log", "w").write(" ".join(sys.argv[1:]))
import base64
needles = (b"secret-token", base64.b64encode(b"x-access-token:secret-token"))
leaks = [str(f) for f in (pathlib.Path(a.site) / ".git").rglob("*") if f.is_file() and any(n in f.read_bytes() for n in needles)]
open(pathlib.Path(a.site).parent / "emit.leak", "w").write(chr(10).join(leaks))
open(pathlib.Path(a.site).parent / "emit.tok", "w").write(os.environ.get("GH_TOKEN", "") + os.environ.get("GITHUB_TOKEN", ""))
dist = pathlib.Path(a.site) / "dist"
(dist / a.date).mkdir(parents=True)
for d in (dist, dist / a.date):
    (d / "channel-rust-1.99.0.toml").write_text("manifest\\n")
(dist / a.date / "yurt-release-manifest.json").write_text(json.dumps({"rust_version": "1.99.0"}))
pathlib.Path(a.components).mkdir(parents=True, exist_ok=True)
(pathlib.Path(a.components) / "rustc-1.99.0-x.tar.gz").write_bytes(b"tar")
"""

CHECKER = """import os, pathlib, sys
open(pathlib.Path(os.environ["GH_LOG"]).parent / "check.log", "w").write(" ".join(sys.argv[1:]))
open(pathlib.Path(os.environ["GH_LOG"]).parent / "check.tok", "w").write(os.environ.get("GH_TOKEN", "") + os.environ.get("GITHUB_TOKEN", ""))
sys.exit(1 if os.environ.get("FAKE_CHECKER_FAIL") else 0)
"""


def git(*args, cwd):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


class PublishRustupDist(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.remote = self.root / "remote.git"
        git("init", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        self.other = self.clone("other")
        (self.other / "README").write_text("x\n")
        self.commit_push(self.other, "init")
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(f'#!/bin/sh\necho "${{GIT_CONFIG_COUNT:-}}|${{GIT_CONFIG_KEY_0:-}}|${{GIT_CONFIG_VALUE_0:-}}" >> {self.root}/push.env\n')
        hook.chmod(0o755)
        self.site = self.root / "work" / "site"
        self.site.parent.mkdir()
        git("clone", str(self.remote), str(self.site), cwd=self.root)
        self.tooling = self.root / "tooling"
        (self.tooling / "scripts").mkdir(parents=True)
        (self.tooling / "scripts" / "emit_rustup_dist.py").write_text(EMITTER)
        (self.tooling / "scripts" / "verify_rustup_dist_urls.py").write_text(CHECKER)
        git("init", "-b", "main", cwd=self.tooling)
        git("add", ".", cwd=self.tooling)
        git("commit", "-m", "tooling", cwd=self.tooling)
        self.tooling_sha = git("rev-parse", "HEAD", cwd=self.tooling)
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.bin, self.state = self.root / "bin", self.root / "state"
        self.bin.mkdir()
        self.state.mkdir()
        gh = self.bin / "gh"
        gh.write_text(GH_SHIM)
        gh.chmod(0o755)
        self.log = self.root / "gh.log"
        self.env = dict(
            os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", GH_LOG=str(self.log), GH_STATE=str(self.state),
            RUST_RELEASE=RELEASE, PUBLISH_DATE=DATE, PRODUCER_HOST="x86_64-unknown-linux-gnu", TOOLING_REF="main",
            YURT_RUST_TOOLING_DIR=str(self.tooling), YURT_SITE_DIR=str(self.site), PAGES_BRANCH="main",
            WORK_DIR=str(self.root / "scratch"), GH_TOKEN="secret-token", GITHUB_TOKEN="secret-github",
            TMPDIR=str(self.tmpdir),
        )

    def clone(self, name):
        path = self.root / name
        git("clone", str(self.remote), str(path), cwd=self.root)
        return path

    def commit_push(self, clone, message):
        git("add", ".", cwd=clone)
        git("commit", "-m", message, cwd=clone)
        git("push", "origin", "HEAD:main", cwd=clone)

    def land_manifest(self):
        (self.other / PUBLISHED).parent.mkdir(parents=True, exist_ok=True)
        (self.other / PUBLISHED).write_text("earlier\n")
        self.commit_push(self.other, "earlier publication")

    def run_script(self, **overrides):
        env = {k: v for k, v in dict(self.env, **overrides).items() if v is not None}
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def verbs(self):
        return [" ".join(c.split()[:2]) for c in self.calls()]

    def remote_head(self):
        return git("rev-parse", "main", cwd=self.remote)

    def test_invalid_inputs_rejected_before_any_gh_call(self):
        bad = {
            "RUST_RELEASE": ["rust-v1.99-0.0.1", "v1.99.0-0.0.1", "rust-v1.99.0-0.0.1; id", "rust-v1.99.0-0.0.1\n"],
            "PUBLISH_DATE": ["2026-1-1", "2026-02-30", "2026-13-01", "20261020", "2026-10-20; id"],
            "PRODUCER_HOST": ["x86_64-apple-darwin", "aarch64-unknown-linux-musl", "x86_64-unknown-linux-gnu x"],
            "TOOLING_REF": ["--upload-pack=x", "a b", "a;b", "$(id)", "a..b", ""],
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    result = self.run_script(**{name: value})
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(self.calls(), [])
        self.assertNotEqual(self.run_script(RUST_RELEASE=None).returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_publishes_and_records_tooling_sha(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.verbs(), ["release download", "release view", "release create", "release upload"])
        self.assertIn(f"release download {RELEASE} -R YurtOS/yurt-rust", self.calls()[0])
        self.assertIn(self.tooling_sha, self.calls()[2])
        emit = (self.root / "work" / "emit.log").read_text()
        self.assertIn(f"--base-url https://github.com/YurtOS/yurt-packages/releases/download/{TAG}", emit)
        self.assertIn("--producer-host x86_64-unknown-linux-gnu", emit)
        self.assertIn(f"--channel {VERSION} --date {DATE}", (self.root / "check.log").read_text())
        probe = self.clone("probe")
        provenance = json.loads((probe / f"dist/{DATE}/yurt-release-manifest.json").read_text())
        self.assertEqual(provenance["tooling_sha"], self.tooling_sha)
        self.assertTrue((probe / f"dist/channel-rust-{VERSION}.toml").exists())

    def assert_refused_after_download(self, result):
        """The script got past validation and downloaded, then refused without touching any release."""
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new date", result.stderr)
        self.assertEqual(self.verbs()[0], "release download")
        self.assertFalse(set(self.verbs()) & set(MUTATING))

    def test_published_manifest_and_existing_release_refuses(self):
        self.land_manifest()
        (self.state / TAG).touch()
        self.assert_refused_after_download(self.run_script())
        self.assertTrue((self.state / TAG).exists())

    def test_published_manifest_without_release_never_recreates_assets(self):
        self.land_manifest()
        self.assert_refused_after_download(self.run_script())

    def test_existing_release_without_manifest_is_deleted_and_recreated(self):
        (self.state / TAG).touch()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.verbs(), ["release download", "release view", "release delete", "release create", "release upload"]
        )
        self.assertTrue((self.clone("probe") / PUBLISHED).exists())

    def test_stale_checkout_sees_manifest_landed_mid_run_and_does_not_delete(self):
        (self.state / TAG).touch()
        hook = self.root / "land.sh"
        hook.write_text(
            textwrap.dedent(
                f"""\
                cd {self.other} && mkdir -p dist/{DATE} && echo earlier > {PUBLISHED}
                git add . && git -c user.name=t -c user.email=t@t commit -qm earlier && git push -q origin HEAD:main
                """
            )
        )
        self.assert_refused_after_download(self.run_script(FAKE_ON_DOWNLOAD=str(hook)))
        self.assertTrue((self.state / TAG).exists())

    def test_checker_failure_commits_no_manifest(self):
        before = self.remote_head()
        result = self.run_script(FAKE_CHECKER_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release upload", self.verbs())
        self.assertTrue((self.root / "check.log").exists())
        self.assertEqual(self.remote_head(), before)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.site), git("rev-parse", "origin/main", cwd=self.site))

    def test_upload_failure_commits_no_manifest_and_skips_checker(self):
        before = self.remote_head()
        result = self.run_script(FAKE_UPLOAD_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.verbs()[-1], "release upload")
        self.assertEqual(self.remote_head(), before)
        self.assertFalse((self.root / "check.log").exists())

    def test_startup_crash_does_not_look_like_a_refusal(self):
        result = self.run_script(YURT_RUST_TOOLING_DIR=str(self.root / "missing"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("new date", result.stderr)

    def test_token_reaches_gh_only_not_tooling(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(set((self.root / "gh.log.tok").read_text().split()), {"secret-token"})
        self.assertEqual((self.root / "work" / "emit.tok").read_text(), "")
        self.assertEqual((self.root / "check.tok").read_text(), "")

    def test_token_is_never_stored_in_the_site_checkout_and_authenticates_only_the_push(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual((self.root / "work" / "emit.leak").read_text(), "")
        needles = (b"secret-token", AUTH_HEADER.split()[-1].encode())
        stored = [f for f in (self.site / ".git").rglob("*") if f.is_file() and any(n in f.read_bytes() for n in needles)]
        self.assertEqual(stored, [])
        pushes = [line for line in (self.root / "push.env").read_text().splitlines() if line != "||"]
        self.assertEqual(pushes, [f"|http.https://github.com/.extraheader|{AUTH_HEADER}"])

    def test_tag_revision_must_match_release_manifest(self):
        result = self.run_script(FAKE_REVISION="0.0.2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("revision", result.stderr)
        self.assertEqual(self.verbs(), ["release download"])

    def test_work_dir_must_be_empty_or_absent(self):
        busy = self.root / "busy"
        busy.mkdir()
        (busy / "stale").write_text("x")
        result = self.run_script(WORK_DIR=str(busy))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WORK_DIR", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_unset_work_dir_scratch_is_removed(self):
        self.assertEqual(self.run_script(WORK_DIR=None).returncode, 0)
        self.assertEqual(list(self.tmpdir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
