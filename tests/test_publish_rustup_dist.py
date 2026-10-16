"""Tests for tools/publish_rustup_dist.sh using a gh shim, a local bare Pages remote and stub tooling."""
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
MUTATING = ("release delete", "release create", "release upload")

GH_SHIM = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
case "$1 $2" in
  "release download")
    while [ $# -gt 0 ]; do [ "$1" = -D ] && dir=$2; shift; done
    echo '{"release": {"rust_version": "1.99.0", "revision": "0.0.1"}}' > "$dir/release-manifest.json"
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
open(pathlib.Path(a.site).parent / "emit.log", "w").write(" ".join(sys.argv[1:]))
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
            WORK_DIR=str(self.root / "work"),
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

    def test_published_manifest_and_existing_release_refuses(self):
        self.land_manifest()
        (self.state / TAG).touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new date", result.stderr)
        self.assertFalse(set(self.verbs()) & set(MUTATING))
        self.assertTrue((self.state / TAG).exists())

    def test_published_manifest_without_release_never_recreates_assets(self):
        self.land_manifest()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(set(self.verbs()) & set(MUTATING))

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
        result = self.run_script(FAKE_ON_DOWNLOAD=str(hook))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(set(self.verbs()) & set(MUTATING))
        self.assertTrue((self.state / TAG).exists())

    def test_checker_failure_commits_no_manifest(self):
        before = self.remote_head()
        result = self.run_script(FAKE_CHECKER_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.remote_head(), before)

    def test_upload_failure_commits_no_manifest_and_skips_checker(self):
        before = self.remote_head()
        result = self.run_script(FAKE_UPLOAD_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.remote_head(), before)
        self.assertFalse((self.root / "check.log").exists())


if __name__ == "__main__":
    unittest.main()
