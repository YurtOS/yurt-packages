"""The phase harness for tests/test_publish_rustup_dist.py: a gh shim, a local bare Pages remote and
stub tooling stand in for GitHub; each phase runs in its own process and temp directory."""
import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


def _load_support(name):
    """tests/<name>.py, loaded once by file path (discover does not collect it: no test_ prefix)."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


stage = _load_support("support_stage").stage
TOOLS = Path(__file__).resolve().parent.parent / "tools"

VERSION, REVISION, DATE = "1.99.0", "0.0.1", "2026-10-01"
RELEASE = f"rust-v{VERSION}-{REVISION}"
TAG = f"rustup-{VERSION}-{DATE}"
PRODUCER = "x86_64-unknown-linux-gnu"
HOSTS = ("aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
GUEST = "wasm32-yurt-linux-musl"
PREFIX = f"https://github.com/YurtOS/yurt-packages/releases/download/{TAG}"
MOVING = f"dist/channel-rust-{VERSION}.toml"
DATED = f"dist/{DATE}/channel-rust-{VERSION}.toml"
PROVENANCE = f"dist/{DATE}/yurt-release-manifest.json"
FIVE = (MOVING, f"{MOVING}.sha256", DATED, f"{DATED}.sha256", PROVENANCE)
PUBLISHED = DATED
COMPONENTS = sorted(
    [f"{p}-{VERSION}-{h}.tar.gz" for h in HOSTS for p in ("rustc", "cargo", "rust-std")] + [f"rust-std-{VERSION}-{GUEST}.tar.gz"]
)
SITE_URL = "https://github.com/YurtOS/yurt-packages"
READ, GITHUB, PUSH = "read-token-secret", "github-token-secret", "push-token-secret"
SECRETS = (READ, GITHUB, PUSH)
AUTH_HEADER = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{PUSH}".encode()).decode()
MUTATING = ("release delete", "release create", "release upload")
GIT_ENV = {
    "PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}

GH_SHIM = """#!/usr/bin/env bash
echo "$*" >> "$GH_LOG"
echo "${GH_TOKEN:-}|${GITHUB_TOKEN:-}|${PUSH_TOKEN:-}" >> "$GH_LOG.tok"
if [ "$1" = api ]; then
  [ -z "${FAKE_API_FAIL:-}" ] || exit 1
  printf '%s\\n' "$FAKE_SHA"
  exit 0
fi
case "$1 $2" in
  "release download")
    tag=$3
    while [ $# -gt 0 ]; do [ "$1" = -D ] && dir=$2; shift; done
    assets=""
    for host in aarch64-apple-darwin x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu; do
      assets="$assets${assets:+, }{\\"kind\\": \\"hosted\\", \\"host\\": \\"$host\\", \\"name\\": \\"rust-$host.tar.gz\\", \\"sha256\\": \\"$(printf e%.0s {1..64})\\", \\"extraction_root\\": \\"rust-$host\\"}"
    done
    cat > "$dir/release-manifest.json" <<EOF
{"release": {"tag": "${FAKE_TAG:-$tag}", "rust_version": "${FAKE_VERSION:-1.99.0}", "revision": "${FAKE_REVISION:-0.0.1}"},
 "source": {"git_revision": "$(printf f%.0s {1..40})"},
 "build_configuration": {"hosts": ["aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu"]},
 "assets": [$assets]}
EOF
    ;;
  "release view") [ -e "$GH_STATE/$3" ] ;;
  "release delete") rm -rf "$GH_STATE/$3" "$GH_STATE/$3.assets" ;;
  "release create") touch "$GH_STATE/$3"; mkdir -p "$GH_STATE/$3.assets" ;;
  "release upload")
    [ -z "${FAKE_UPLOAD_FAIL:-}" ] || exit 1
    tag=$3
    shift 3
    for f in "$@"; do if [ -f "$f" ]; then cp "$f" "$GH_STATE/$tag.assets/"; fi; done ;;
esac
"""

SDK_HOSTS = """HOST_SDK_NAMES = {
    "aarch64-apple-darwin": "macos-arm64",
    "x86_64-unknown-linux-gnu": "linux-x86_64",
    "aarch64-unknown-linux-gnu": "linux-arm64",
}
"""

EMITTER = '''"""Stand-in for yurt-rust scripts/emit_rustup_dist.py: same arguments, same output layout."""
import argparse, hashlib, importlib.util, json, os, pathlib, sys

from sdk_hosts import HOST_SDK_NAMES  # a sibling import, as in the real emitter: fails under python3 -I

GUEST = "wasm32-yurt-linux-musl"
PACKAGES = ("rustc", "cargo", "rust-std")

# The manifest text comes from the validator's render_manifest, the one renderer (golden-tested
# against the real emitter's write_manifest), so this stub cannot drift from what publish accepts.
_spec = importlib.util.spec_from_file_location("stub_stage", @STAGE@)
stub_stage = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = stub_stage
_spec.loader.exec_module(stub_stage)
render = stub_stage.render_manifest


parser = argparse.ArgumentParser()
for name in ("release-dir", "site", "components", "date", "base-url", "producer-host"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
logs = pathlib.Path(os.environ["FAKE_LOG_DIR"])
(logs / "emit.log").write_text(" ".join(sys.argv[1:]))
(logs / "emit.env").write_text(json.dumps(dict(os.environ)))
site = pathlib.Path(args.site)
needles = [n.encode() for n in os.environ.get("FAKE_NEEDLES", "").split(",") if n]
stored = [str(f) for f in (site / ".git").rglob("*") if f.is_file() and any(n in f.read_bytes() for n in needles)]
(logs / "emit.leak").write_text("\\n".join(stored))
mode = os.environ.get("FAKE_EMIT_MODE", "")
if mode == "fail":
    sys.exit("emit_rustup_dist: failing as asked")

release = json.loads((pathlib.Path(args.release_dir) / "release-manifest.json").read_text())
version, revision = release["release"]["rust_version"], release["release"]["revision"]
hosts = [host for host in release["build_configuration"]["hosts"] if host in HOST_SDK_NAMES]
components = pathlib.Path(args.components)
if components.exists() and any(components.iterdir()):
    sys.exit("emit_rustup_dist: --components must be empty or absent")
components.mkdir(parents=True, exist_ok=True)
entries = {package: {} for package in PACKAGES}
for host in hosts:
    targets = [("rustc", host), ("cargo", host), ("rust-std", host)] + ([("rust-std", GUEST)] if host == args.producer_host else [])
    for package, target in targets:
        name = f"{package}-{version}-{target}.tar.gz"
        data = f"{name} built on {host}\\n".encode()
        (components / name).write_bytes(data)
        entries[package][target] = (name, hashlib.sha256(data).hexdigest())

text = render(version, revision, args.date, entries, args.base_url)
name = f"channel-rust-{version}.toml"
sidecar = f"{hashlib.sha256(text.encode()).hexdigest()}  {name}\\n"
provenance = {
    "release_tag": release["release"]["tag"],
    "rust_version": version,
    "revision": revision,
    "source_git_revision": release["source"]["git_revision"],
    "date": args.date,
    "producer_host": args.producer_host,
    "archives": {a["host"]: a for a in release["assets"] if a.get("kind") == "hosted"},
}
provenance_text = json.dumps(provenance, indent=2, sort_keys=True) + "\\n"
if mode == "bad-provenance":
    provenance_text = '{"release_tag": '
dist = site / "dist"
dated = dist / args.date
dated.mkdir(parents=True)
(dated / name).write_text(text)
(dated / f"{name}.sha256").write_text(sidecar)
(dated / "yurt-release-manifest.json").write_text(provenance_text)
(dist / name).write_text(text)
(dist / f"{name}.sha256").write_text(sidecar)
'''.replace("@STAGE@", repr(str(TOOLS / "rustup_dist_stage.py")))

CHECKER = """import json, os, pathlib, sys
logs = pathlib.Path(os.environ["FAKE_LOG_DIR"])
(logs / "check.log").write_text(" ".join(sys.argv[1:]))
dist = pathlib.Path(sys.argv[sys.argv.index("--dist") + 1])
(logs / "check.files").write_text("\\n".join(sorted(p.relative_to(dist.parent).as_posix() for p in dist.rglob("*") if p.is_file())))
(logs / "check.env").write_text(json.dumps(dict(os.environ)))
sys.exit(1 if os.environ.get("FAKE_CHECKER_FAIL") else 0)
"""


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()


def pack(site: Path, tar_path: Path, members=None):
    """members: (name, bytes) pairs, by default the five files under site, in emitter order."""
    members = members if members is not None else [(name, (site / name).read_bytes()) for name in FIVE]
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data, *tweak in members:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            for change in tweak:
                change(info)
            tar.addfile(info, io.BytesIO(data) if info.type in (tarfile.REGTYPE, tarfile.AREGTYPE) else None)
    return tar_path


def tar_members(tar_path: Path) -> dict[str, bytes]:
    with tarfile.open(tar_path, "r:") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


class Phase:
    """One phase run: its process result, its directory (gh log, outputs) and the env it got."""

    def __init__(self, done, directory: Path, env: dict):
        self.returncode, self.stdout, self.stderr = done.returncode, done.stdout, done.stderr
        self.dir, self.env = directory, env

    def calls(self):
        log = self.dir / "gh.log"
        return log.read_text().splitlines() if log.exists() else []

    def verbs(self):
        return [" ".join(c.split()[:2]) for c in self.calls()]

    def tokens(self):
        log = self.dir / "gh.log.tok"
        return set(log.read_text().splitlines()) if log.exists() else set()

    def outputs(self):
        path = self.dir / "output"
        if not path.exists():
            return {}
        return dict(line.split("=", 1) for line in path.read_text().splitlines())


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.counter = 0
        self.remote = self.root / "remote.git"
        git("init", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        self.other = self.root / "other"
        git("clone", str(self.remote), str(self.other), cwd=self.root)
        (self.other / "README").write_text("x\n")
        self.commit_push("init")
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\n"
            # git strips GIT_CONFIG_COUNT before this local hook runs, so its field is always empty.
            f'echo "${{GIT_CONFIG_COUNT:-}}|${{GIT_CONFIG_KEY_0:-}}|${{GIT_CONFIG_VALUE_0:-}}" >> {self.root}/push.env\n'
            f'if [ -e {self.root}/reject ]; then echo "rejected by the test" >&2; exit 1; fi\n'
        )
        hook.chmod(0o755)
        self.trusted = self.root / "trusted" / "tools"
        self.trusted.mkdir(parents=True)
        for name in ("publish_rustup_dist.sh", "rustup_dist_stage.py"):
            shutil.copy(TOOLS / name, self.trusted / name)
        self.script = self.trusted / "publish_rustup_dist.sh"
        self.tooling = self.root / "tooling"
        (self.tooling / "scripts").mkdir(parents=True)
        (self.tooling / "scripts" / "emit_rustup_dist.py").write_text(EMITTER)
        (self.tooling / "scripts" / "sdk_hosts.py").write_text(SDK_HOSTS)
        (self.tooling / "scripts" / "verify_rustup_dist_urls.py").write_text(CHECKER)
        git("init", "-b", "main", cwd=self.tooling)
        git("add", ".", cwd=self.tooling)
        git("commit", "-m", "tooling", cwd=self.tooling)
        self.tooling_sha = git("rev-parse", "HEAD", cwd=self.tooling)
        self.plan = stage.Plan(RELEASE, VERSION, REVISION, DATE, PRODUCER, self.tooling_sha, TAG)
        for name in ("tmp", "bin", "state", "logs", "home"):
            (self.root / name).mkdir()
        self.tmpdir, self.state, self.logs = self.root / "tmp", self.root / "state", self.root / "logs"
        gh = self.root / "bin" / "gh"
        gh.write_text(GH_SHIM)
        gh.chmod(0o755)
        self.start_head = self.remote_head()

    # -- environments -------------------------------------------------------------------------

    def base_env(self):
        """What every job's VM has, and nothing else: no token unless a phase is given one."""
        return {
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}", "HOME": str(self.root / "home"), "TMPDIR": str(self.tmpdir),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GH_STATE": str(self.state),
            "FAKE_LOG_DIR": str(self.logs), "FAKE_SHA": self.tooling_sha, "FAKE_NEEDLES": ",".join(SECRETS),
        }

    def plan_inputs(self, **overrides):
        return dict(
            {"RUST_RELEASE": RELEASE, "PUBLISH_DATE": DATE, "PRODUCER_HOST": PRODUCER, "TOOLING_REF": "main", "GH_TOKEN": READ},
            **overrides,
        )

    def plan_values(self, **overrides):
        values = {f"PLAN_{key.upper()}": getattr(self.plan, key) for key in stage.Plan.__dataclass_fields__}
        return dict(values, **overrides)

    def unique(self, name):
        self.counter += 1
        return self.root / f"{self.counter:02d}-{name}"

    def fresh_site(self, origin=SITE_URL):
        """A clone whose origin is the real URL, as actions/checkout leaves it; a site-local
        insteadOf maps that URL (and the literal URL push uses) to the local bare remote."""
        site = self.unique("site")
        git("clone", str(self.remote), str(site), cwd=self.root)
        git("remote", "set-url", "origin", origin, cwd=site)
        git("config", f"url.{self.remote}.insteadOf", origin, cwd=site)
        if origin != SITE_URL:
            git("config", "--add", f"url.{self.remote}.insteadOf", SITE_URL, cwd=site)
        return site

    def run_phase(self, name, env):
        directory = self.unique(f"phase-{name}")
        directory.mkdir()
        full = {**self.base_env(), "GH_LOG": str(directory / "gh.log"), "OUTPUT_FILE": str(directory / "output"), **env}
        full = {k: v for k, v in full.items() if v is not None}
        done = subprocess.run(["bash", str(self.script), *([name] if name else [])], cwd=directory, env=full, capture_output=True, text=True)
        return Phase(done, directory, full)

    def emit(self, plan=None, **env):
        defaults = dict(
            plan or self.plan_values(), GH_TOKEN=READ, YURT_RUST_TOOLING_DIR=str(self.tooling),
            YURT_SITE_DIR=str(self.fresh_site()), OUT_DIR=str(self.unique("out")),
        )
        phase = self.run_phase("emit", dict(defaults, **env))
        phase.out = Path(phase.env["OUT_DIR"])
        return phase

    def publish(self, dist_dir=None, components_dir=None, plan=None, **env):
        if dist_dir is None:
            dist_dir, components_dir = self.make_artifacts()
        defaults = dict(
            plan or self.plan_values(), DIST_DIR=str(dist_dir), COMPONENTS_DIR=str(components_dir),
            YURT_SITE_DIR=str(self.fresh_site()), GH_TOKEN=GITHUB, GITHUB_RUN_ATTEMPT="1",
        )
        return self.run_phase("publish", dict(defaults, **env))

    def push(self, dist_dir=None, digests=None, plan=None, **env):
        if dist_dir is None:
            dist_dir, components_dir = self.make_artifacts()
            digests = self.digests_for(dist_dir, components_dir) if digests is None else digests
            shutil.rmtree(components_dir)  # push never sees the components
        defaults = dict(
            plan or self.plan_values(), DIST_DIR=str(dist_dir), DIGESTS=digests, YURT_SITE_DIR=str(self.fresh_site()),
            PUSH_TOKEN=PUSH, GITHUB_RUN_ATTEMPT="1",
        )
        return self.run_phase("push", dict(defaults, **env))

    # -- artifacts, built by test code (independent of the emit phase) ---------------------------

    def make_artifacts(self, mode=""):
        """Run the stub emitter directly and pack the five files: a dist staging dir and a components dir."""
        directory = self.unique("artifacts")
        release, site, dist = directory / "release", directory / "site", directory / "dist"
        for path in (release, site, dist, directory / "logs"):
            path.mkdir(parents=True)
        env = dict(self.base_env(), GH_LOG=str(directory / "gh.log"), FAKE_LOG_DIR=str(directory / "logs"), FAKE_EMIT_MODE=mode)
        subprocess.run([str(self.root / "bin" / "gh"), "release", "download", RELEASE, "-R", "YurtOS/yurt-rust", "-D", str(release)],
                       env=env, check=True)
        subprocess.run(
            ["python3", str(self.tooling / "scripts" / "emit_rustup_dist.py"), "--release-dir", str(release), "--site", str(site),
             "--components", str(directory / "components"), "--date", DATE, "--producer-host", PRODUCER, "--base-url", PREFIX],
            env=env, check=True,
        )
        pack(site, dist / "dist.tar")
        return dist, directory / "components"

    def staged(self, source: Path):
        """A fresh copy of an artifact directory, as download-artifact gives a later job."""
        target = self.unique("staged")
        shutil.copytree(source, target, symlinks=True)
        return target

    def digests_for(self, dist_dir, components_dir):
        dist = stage.read_dist_dir(Path(dist_dir), self.plan)
        components = stage.check_components(Path(components_dir), dist, self.plan)
        digests = stage.compute_digests(Path(dist_dir) / "dist.tar", dist, components)
        return json.dumps(digests, separators=(",", ":"), sort_keys=True)

    # -- the Pages remote ------------------------------------------------------------------------

    def commit_push(self, message):
        git("add", ".", cwd=self.other)
        git("commit", "-m", message, cwd=self.other)
        git("push", "origin", "HEAD:main", cwd=self.other)

    def prepare_commit(self, files, message):
        """Commit files in `other` on top of the current remote main, without pushing; returns the commit."""
        git("pull", "-q", "origin", "main", cwd=self.other)
        for name, text in files.items():
            (self.other / name).parent.mkdir(parents=True, exist_ok=True)
            (self.other / name).write_text(text)
        git("add", ".", cwd=self.other)
        git("commit", "-m", message, cwd=self.other)
        return git("rev-parse", "HEAD", cwd=self.other)

    def land(self, files, message="another publication"):
        commit = self.prepare_commit(files, message)
        git("push", "origin", "HEAD:main", cwd=self.other)
        return commit

    def land_manifest(self):
        return self.land({PUBLISHED: "earlier\n"}, "earlier publication")

    def race_on_first_push(self, site: Path, files, message="race"):
        """Prepare a commit in `other`; it lands just before the phase's first `git push`, so that push loses.

        A `git` wrapper on the phase's PATH does it (the push runs with hooks disabled, so a pre-push
        hook cannot). `site` is unused: the wrapper fires on the first push of any checkout."""
        commit = self.prepare_commit(files, message)
        flag = self.root / "race.flag"
        flag.touch()
        real = shutil.which("git", path=os.environ["PATH"])
        wrapper = self.root / "bin" / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            f'if [ -e "{flag}" ]; then\n'
            '  for arg in "$@"; do\n'
            '    if [ "$arg" = push ]; then\n'
            f'      rm -f "{flag}"\n'
            "      (unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GIT_CONFIG_PARAMETERS\n"
            f'       "{real}" -C "{self.other}" push -q origin HEAD:main) || exit 97\n'
            "      break\n"
            "    fi\n"
            "  done\n"
            "fi\n"
            f'exec "{real}" "$@"\n'
        )
        wrapper.chmod(0o755)
        return commit

    def remote_head(self):
        return git("rev-parse", "main", cwd=self.remote)

    def header_pushes(self):
        path = self.root / "push.env"
        return [line for line in path.read_text().splitlines() if line != "||"] if path.exists() else []

    def all_verbs(self):
        verbs = []
        for log in sorted(self.root.glob("*-phase-*/gh.log")):
            verbs += [" ".join(c.split()[:2]) for c in log.read_text().splitlines()]
        return verbs

    # -- assertions ------------------------------------------------------------------------------

    def assert_nothing_pushed(self, head=None):
        self.assertEqual(self.remote_head(), head or self.start_head)
        self.assertEqual(self.header_pushes(), [])

    def assert_no_release_mutation(self):
        self.assertFalse(set(self.all_verbs()) & set(MUTATING), self.all_verbs())

    def assert_refused(self, phase, substring, head=None):
        """Refused with this message before any gh call: no output, no release mutation, no push."""
        self.assertNotEqual(phase.returncode, 0, phase.stderr)
        self.assertIn(substring, phase.stderr)
        self.assertEqual(phase.calls(), [], phase.stderr)
        self.assertEqual(phase.outputs(), {})
        self.assert_no_release_mutation()
        self.assert_nothing_pushed(head)

    def assert_no_secret_in(self, env_file: Path):
        values = [v for k, v in json.loads(env_file.read_text()).items() if k != "FAKE_NEEDLES"]
        self.assertEqual([s for s in SECRETS if any(s in v for v in values)], [])
