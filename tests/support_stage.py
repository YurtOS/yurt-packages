"""Shared by the validator and phase tests: tools/rustup_dist_stage.py loaded once, and fixtures that
build the five dist members (manifests through stage.render_manifest, the one renderer)."""
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "rustup_dist_stage.py"
_spec = importlib.util.spec_from_file_location("rustup_dist_stage", TOOL)
stage = importlib.util.module_from_spec(_spec)
sys.modules["rustup_dist_stage"] = stage
_spec.loader.exec_module(stage)

VERSION, REVISION, DATE = "1.99.0", "0.0.1", "2026-10-01"
RELEASE = f"rust-v{VERSION}-{REVISION}"
TAG = f"rustup-{VERSION}-{DATE}"
PRODUCER = "aarch64-apple-darwin"
TOOLING_SHA = "0123456789abcdef0123456789abcdef01234567"
HOSTS = ("aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
GUEST = "wasm32-yurt-linux-musl"
PACKAGES = ("rustc", "cargo", "rust-std")
PREFIX = f"https://github.com/YurtOS/yurt-packages/releases/download/{TAG}"
PLAN = stage.Plan(RELEASE, VERSION, REVISION, DATE, PRODUCER, TOOLING_SHA, TAG)

MOVING = f"dist/channel-rust-{VERSION}.toml"
MOVING_SHA = f"{MOVING}.sha256"
DATED = f"dist/{DATE}/channel-rust-{VERSION}.toml"
DATED_SHA = f"{DATED}.sha256"
PROVENANCE = f"dist/{DATE}/yurt-release-manifest.json"
FIVE = (MOVING, MOVING_SHA, DATED, DATED_SHA, PROVENANCE)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def component_entries(version=VERSION, producer=PRODUCER):
    """package -> target -> component bytes, the set the emitter writes."""
    entries = {package: {} for package in PACKAGES}
    for host in HOSTS:
        for package in PACKAGES:
            entries[package][host] = f"{package}-{version}-{host} payload\n".encode()
    entries["rust-std"][GUEST] = f"rust-std-{version}-{GUEST} from {producer}\n".encode()
    return entries


def manifest_text(entries, version=VERSION, revision=REVISION, date=DATE, base=PREFIX):
    """The canonical manifest for component bytes: stage.render_manifest (the one renderer, golden-
    tested against the real emitter) over each component's (file name, sha256). Not a renderer."""
    assert set(entries) <= set(PACKAGES), "edit the text explicitly for packages render_manifest does not know"
    named = {p: {t: (f"{p}-{version}-{t}.tar.gz", sha(d)) for t, d in entries.get(p, {}).items()} for p in PACKAGES}
    return stage.render_manifest(version, revision, date, named, base)


def relabelled(label):
    """The canonical manifest with every version label replaced (a deliberate text mutation)."""
    return manifest_text(component_entries()).replace(f'"{VERSION} (yurt {REVISION})"', f'"{label}"')


def provenance(**overrides):
    data = {
        "release_tag": RELEASE,
        "rust_version": VERSION,
        "revision": REVISION,
        "source_git_revision": "f" * 40,
        "date": DATE,
        "producer_host": PRODUCER,
        "archives": {h: {"host": h, "kind": "hosted", "name": f"rust-{h}.tar.gz", "sha256": "e" * 64} for h in HOSTS},
        "guest_sdks": {"wasm32-yurt-linux-musl": {"version": "0.0.1"}},
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def sidecar(manifest: bytes, name=f"channel-rust-{VERSION}.toml") -> bytes:
    return f"{sha(manifest)}  {name}\n".encode()


def dist_members(entries=None, manifest=None, prov=None):
    """The five members, byte-for-byte as the emitter writes them."""
    entries = entries if entries is not None else component_entries()
    text = (manifest if manifest is not None else manifest_text(entries)).encode()
    prov_bytes = prov if isinstance(prov, bytes) else (json.dumps(prov or provenance(), indent=2, sort_keys=True) + "\n").encode()
    return {MOVING: text, MOVING_SHA: sidecar(text), DATED: text, DATED_SHA: sidecar(text), PROVENANCE: prov_bytes}


def write_tar(path: Path, members):
    """members: iterable of (name, bytes) or (name, bytes, tweak) where tweak(TarInfo) adjusts the header."""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for item in members:
            name, data, tweak = (*item, None) if len(item) == 2 else item
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            if tweak:
                tweak(info)
            tar.addfile(info, io.BytesIO(data) if info.type in (tarfile.REGTYPE, tarfile.AREGTYPE) else None)
    return path


def write_components(directory: Path, entries=None, version=VERSION):
    entries = entries if entries is not None else component_entries()
    directory.mkdir(parents=True, exist_ok=True)
    for package, targets in entries.items():
        for target, data in targets.items():
            (directory / f"{package}-{version}-{target}.tar.gz").write_bytes(data)
    return directory


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stage-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))

    def raises(self, substring, fn, *args):
        with self.assertRaises(stage.StageError) as caught:
            fn(*args)
        self.assertIn(substring, str(caught.exception))

    def tar_of(self, members=None, name="dist.tar"):
        members = members if members is not None else dist_members()
        return write_tar(self.tmp / name, list(members.items()) if isinstance(members, dict) else members)

    def staging(self, members=None):
        directory = self.tmp / "staging"
        directory.mkdir()
        write_tar(directory / "dist.tar", list((members or dist_members()).items()))
        return directory

    def site(self, *dated):
        site = self.tmp / "site"
        (site / "dist").mkdir(parents=True)
        for name in dated:
            (site / "dist" / name).mkdir()
        return site
