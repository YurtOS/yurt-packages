"""Tests for tools/rustup_dist_stage.py, the trusted validator of staged rustup dist artifacts."""
import contextlib
import gzip
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
import unittest
from pathlib import Path
from unittest import mock


def _load_support(name):
    """tests/<name>.py, loaded once by file path (discover does not collect it: no test_ prefix)."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


_load_support("support_stage")
from support_stage import (  # noqa: E402
    Case,
    DATE,
    DATED,
    DATED_SHA,
    FIVE,
    GUEST,
    MOVING,
    MOVING_SHA,
    PACKAGES,
    PLAN,
    PREFIX,
    PRODUCER,
    PROVENANCE,
    RELEASE,
    REVISION,
    TAG,
    TOOL,
    TOOLING_SHA,
    VERSION,
    component_entries,
    dist_members,
    manifest_text,
    provenance,
    relabelled,
    sha,
    sidecar,
    stage,
    write_components,
    write_tar,
)


class MakePlanTests(Case):
    def test_valid_inputs_derive_the_plan(self):
        plan = stage.make_plan(RELEASE, DATE, PRODUCER, TOOLING_SHA, "2026-10-17")
        self.assertEqual(plan, PLAN)

    def test_empty_date_resolves_to_today(self):
        plan = stage.make_plan(RELEASE, "", PRODUCER, TOOLING_SHA, "2026-10-17")
        self.assertEqual((plan.date, plan.tag), ("2026-10-17", f"rustup-{VERSION}-2026-10-17"))

    def test_date_equal_to_today_is_accepted(self):
        self.assertEqual(stage.make_plan(RELEASE, DATE, PRODUCER, TOOLING_SHA, DATE).date, DATE)

    def test_bad_release_shape(self):
        for release in ("rust-v1.99-0.0.1", "v1.99.0-0.0.1", "rust-v1.99.0-0.0.1x", "rust-v1.99.0", "rust-v١.99.0-0.0.1"):
            with self.subTest(release=release):
                self.raises("is not rust-vX.Y.Z-A.B.C", stage.make_plan, release, DATE, PRODUCER, TOOLING_SHA, "2026-10-17")

    def test_non_calendar_date(self):
        for date in ("2026-02-30", "2026-13-01", "2026-1-01", "20261001", "2026-10-01T00:00"):
            with self.subTest(date=date):
                self.raises("is not a real YYYY-MM-DD date", stage.make_plan, RELEASE, date, PRODUCER, TOOLING_SHA, "2026-10-17")

    def test_future_date(self):
        self.raises("is in the future", stage.make_plan, RELEASE, "2026-10-18", PRODUCER, TOOLING_SHA, "2026-10-17")

    def test_bad_producer_host(self):
        for host in ("", "x86_64-apple-darwin", GUEST, "aarch64-apple-darwin "):
            with self.subTest(host=host):
                self.raises("is not one of the supported hosts", stage.make_plan, RELEASE, DATE, host, TOOLING_SHA, "2026-10-17")

    def test_tooling_sha_not_40_hex(self):
        for value in ("", "0" * 39, "0" * 41, "0" * 64, "A" * 40, "g" * 40, "main"):
            with self.subTest(value=value):
                self.raises("is not a 40-hex commit SHA", stage.make_plan, RELEASE, DATE, PRODUCER, value, "2026-10-17")


class DateRuleTests(Case):
    def test_no_dist_dir_is_accepted(self):
        site = self.tmp / "site"
        site.mkdir()
        stage.check_date_rules(site, PLAN)

    def test_missing_site_is_refused(self):
        """A missing site never means "skip the date rules"."""
        self.raises("is not a directory", stage.check_date_rules, self.tmp / "missing", PLAN)
        (self.tmp / "file").write_text("x")
        self.raises("is not a directory", stage.check_date_rules, self.tmp / "file", PLAN)

    def test_later_than_newest_is_accepted(self):
        stage.check_date_rules(self.site("2026-09-30", "not-a-date", "2026-02-30"), PLAN)

    def test_dated_dir_exists(self):
        self.raises(f"dist/{DATE} already exists", stage.check_date_rules, self.site(DATE), PLAN)

    def test_dated_dir_exists_as_a_file(self):
        site = self.site()
        (site / "dist" / DATE).write_text("x")
        self.raises(f"dist/{DATE} already exists", stage.check_date_rules, site, PLAN)

    def test_not_later_than_newest_dated_dir(self):
        self.raises("must be later than the newest dated dir 2026-10-05", stage.check_date_rules, self.site("2026-09-01", "2026-10-05"), PLAN)


class StagingDirTests(Case):
    def test_exactly_one_dist_tar(self):
        self.assertEqual(set(stage.read_dist_dir(self.staging(), PLAN)), set(FIVE))

    def test_extra_file(self):
        directory = self.staging()
        (directory / "notes.txt").write_text("x")
        self.raises("must contain exactly dist.tar", stage.read_dist_dir, directory, PLAN)

    def test_subdirectory(self):
        directory = self.staging()
        (directory / "dist").mkdir()
        self.raises("must contain exactly dist.tar", stage.read_dist_dir, directory, PLAN)

    def test_empty_directory(self):
        directory = self.tmp / "staging"
        directory.mkdir()
        self.raises("must contain exactly dist.tar", stage.read_dist_dir, directory, PLAN)

    def test_symlinked_dist_tar(self):
        real = self.tar_of(name="real.tar")
        directory = self.tmp / "staging"
        directory.mkdir()
        (directory / "dist.tar").symlink_to(real)
        self.raises("dist.tar is not a regular file", stage.read_dist_dir, directory, PLAN)

    def test_dist_tar_is_a_directory(self):
        directory = self.tmp / "staging"
        (directory / "dist.tar").mkdir(parents=True)
        self.raises("dist.tar is not a regular file", stage.read_dist_dir, directory, PLAN)


def as_symlink(info):
    info.type, info.linkname, info.size = tarfile.SYMTYPE, "/etc/passwd", 0


def as_hardlink(info):
    info.type, info.linkname, info.size = tarfile.LNKTYPE, MOVING, 0


def as_chardev(info):
    info.type, info.devmajor, info.devminor, info.size = tarfile.CHRTYPE, 1, 3, 0


def as_dir(info):
    info.type, info.size = tarfile.DIRTYPE, 0


def as_fifo(info):
    info.type, info.size = tarfile.FIFOTYPE, 0


class TarContainerTests(Case):
    def members(self, replace=None, extra=()):
        items = [(name, data) for name, data in dist_members().items()]
        if replace:
            items = [replace.get(name, (name, data)) for name, data in items]
        return [*items, *extra]

    def test_valid_tar_returns_the_five_members(self):
        dist = stage.read_dist_tar(self.tar_of(), PLAN)
        self.assertEqual(dist, dist_members())

    def test_traversal(self):
        tar = self.tar_of(self.members(extra=[("dist/../evil.sh", b"x")]))
        self.raises("member path contains ..", stage.read_dist_tar, tar, PLAN)

    def test_absolute(self):
        tar = self.tar_of(self.members(replace={MOVING: (f"/{MOVING}", dist_members()[MOVING])}))
        self.raises("absolute member path", stage.read_dist_tar, tar, PLAN)

    def test_duplicate(self):
        tar = self.tar_of(self.members(extra=[(MOVING, b"second copy")]))
        self.raises("duplicate member", stage.read_dist_tar, tar, PLAN)

    def test_non_normalized_names_are_unexpected(self):
        for name in (f"./{MOVING}", MOVING.replace("dist/", "dist//"), f"dist/{DATE}/./channel-rust-{VERSION}.toml"):
            with self.subTest(name=name):
                tar = self.tar_of(self.members(replace={MOVING: (name, dist_members()[MOVING])}))
                self.raises("unexpected member", stage.read_dist_tar, tar, PLAN)

    def test_link_and_special_members(self):
        for tweak, label in ((as_symlink, "symlink"), (as_hardlink, "hardlink"), (as_chardev, "device"), (as_dir, "dir"), (as_fifo, "fifo")):
            with self.subTest(kind=label):
                tar = self.tar_of(self.members(replace={MOVING_SHA: (MOVING_SHA, b"", tweak)}))
                self.raises("is not a regular file", stage.read_dist_tar, tar, PLAN)

    def test_directory_entry_is_rejected(self):
        tar = self.tar_of([("dist", b"", as_dir), *self.members()])
        self.raises("is not a regular file", stage.read_dist_tar, tar, PLAN)

    def test_oversized_member(self):
        tar = self.tar_of(self.members(replace={PROVENANCE: (PROVENANCE, b" " * (stage.MAX_MEMBER_BYTES + 1))}))
        self.raises("exceeds the per-member cap", stage.read_dist_tar, tar, PLAN)

    def test_total_cap(self):
        tar = self.tar_of()
        with mock.patch.object(stage, "MAX_TOTAL_BYTES", 100):
            self.raises("exceeds the total cap", stage.read_dist_tar, tar, PLAN)

    def test_tar_file_too_large(self):
        tar = self.tar_of()
        with mock.patch.object(stage, "MAX_TAR_BYTES", 1024):
            self.raises("dist.tar is larger than", stage.read_dist_tar, tar, PLAN)

    def test_extra_member(self):
        tar = self.tar_of(self.members(extra=[(f"dist/{DATE}/extra.txt", b"x")]))
        self.raises("unexpected member", stage.read_dist_tar, tar, PLAN)

    def test_member_for_another_version_is_unexpected(self):
        name = "dist/channel-rust-1.98.0.toml"
        tar = self.tar_of(self.members(replace={MOVING: (name, dist_members()[MOVING])}))
        self.raises("unexpected member", stage.read_dist_tar, tar, PLAN)

    def test_missing_member(self):
        tar = self.tar_of([item for item in self.members() if item[0] != PROVENANCE])
        self.raises("missing members", stage.read_dist_tar, tar, PLAN)

    def test_compressed_tar_is_rejected(self):
        raw = self.tar_of(name="plain.tar").read_bytes()
        tar = self.tmp / "dist.tar"
        tar.write_bytes(gzip.compress(raw))
        self.raises("is not an uncompressed tar", stage.read_dist_tar, tar, PLAN)

    def test_garbage_is_rejected(self):
        tar = self.tmp / "dist.tar"
        tar.write_bytes(b"not a tar at all" * 100)
        self.raises("is not an uncompressed tar", stage.read_dist_tar, tar, PLAN)


class ContentTests(Case):
    def read(self, members):
        return stage.read_dist_tar(self.tar_of(members), PLAN)

    def test_wrong_version_label_in_manifest(self):
        self.raises("manifest version", self.read, dist_members(manifest=relabelled(f"1.98.0 (yurt {REVISION})")))

    def test_wrong_revision_in_manifest(self):
        text = manifest_text(component_entries(), revision="0.0.2")
        self.raises("manifest version", self.read, dist_members(manifest=text))

    def test_bare_version_label_in_manifest(self):
        self.raises("manifest version", self.read, dist_members(manifest=relabelled(VERSION)))

    def test_package_version_label_differs(self):
        text = manifest_text(component_entries()).replace(f'[pkg.cargo]\nversion = "{VERSION} (yurt {REVISION})"', '[pkg.cargo]\nversion = "1.98.0 (yurt 0.0.1)"')
        self.raises("manifest version", self.read, dist_members(manifest=text))

    def test_wrong_date_in_manifest(self):
        text = manifest_text(component_entries(), date="2026-09-30")
        self.raises("manifest date", self.read, dist_members(manifest=text))

    def test_manifest_not_toml(self):
        self.raises("dated manifest is not valid TOML", self.read, dist_members(manifest="date = \n[[["))

    def test_manifest_wrong_shape(self):
        self.raises("dated manifest", self.read, dist_members(manifest='manifest-version = "2"\ndate = "2026-10-01"\npkg = 3\n'))

    def test_producer_host_not_a_manifest_host(self):
        entries = component_entries()
        for package in PACKAGES:
            del entries[package][PRODUCER]
        self.raises("producer host aarch64-apple-darwin is not a host of the manifest", self.read, dist_members(entries=entries))

    def test_moving_differs_from_dated(self):
        members = dist_members()
        other = members[MOVING] + b"\n"
        members[MOVING], members[MOVING_SHA] = other, sidecar(other)
        self.raises("moving manifest differs from the dated copy", self.read, members)

    def test_moving_sidecar_differs_from_dated(self):
        members = dist_members()
        members[MOVING_SHA] = members[MOVING_SHA].replace(b"  ", b" *")
        self.raises("moving sidecar differs from the dated copy", self.read, members)

    def test_sidecar_does_not_match_manifest(self):
        members = dist_members()
        members[MOVING_SHA] = members[DATED_SHA] = sidecar(b"something else")
        self.raises("sidecar does not match the manifest", self.read, members)

    def test_sidecar_names_another_file(self):
        members = dist_members()
        members[MOVING_SHA] = members[DATED_SHA] = sidecar(members[DATED], name="channel-rust-1.98.0.toml")
        self.raises("sidecar does not match the manifest", self.read, members)

    def test_provenance_wrong_identity(self):
        cases = {
            "release_tag": "rust-v1.99.0-0.0.2",
            "rust_version": "1.98.0",
            "revision": "0.0.2",
            "date": "2026-09-30",
            "producer_host": "x86_64-unknown-linux-gnu",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                self.raises(f"provenance {key}", self.read, dist_members(prov=provenance(**{key: value})))

    def test_provenance_wrong_type(self):
        self.raises("provenance rust_version", self.read, dist_members(prov=provenance(rust_version=[VERSION])))

    def test_provenance_not_json(self):
        self.raises("provenance is not valid JSON", self.read, dist_members(prov=b"{not json"))

    def test_provenance_not_utf8(self):
        self.raises("provenance is not valid JSON", self.read, dist_members(prov=b"\xff\xfe{}"))

    def test_provenance_not_an_object(self):
        self.raises("provenance is not a JSON object", self.read, dist_members(prov=b"[1, 2]\n"))

    def test_provenance_duplicate_key(self):
        text = (json.dumps(provenance(), indent=2, sort_keys=True)[:-2] + f',\n  "rust_version": "1.98.0"\n}}\n').encode()
        self.raises("provenance is not valid JSON", self.read, dist_members(prov=text))

    def test_provenance_missing_keys(self):
        for key in ("release_tag", "rust_version", "revision", "source_git_revision", "date", "producer_host", "archives"):
            with self.subTest(key=key):
                self.raises(f"provenance lacks {key}", self.read, dist_members(prov=provenance(**{key: None})))

    def test_provenance_without_guest_sdks_is_accepted(self):
        self.read(dist_members(prov=provenance(guest_sdks=None)))

    def test_provenance_unknown_key(self):
        self.raises("provenance has unexpected keys", self.read, dist_members(prov=provenance(tooling_sha="0" * 40)))


class ComponentTests(Case):
    def check(self, directory, members=None):
        return stage.check_components(directory, members or dist_members(), PLAN)

    def test_matching_components(self):
        directory = write_components(self.tmp / "components")
        digests = self.check(directory)
        expected = {f"{p}-{VERSION}-{t}.tar.gz": sha(d) for p, ts in component_entries().items() for t, d in ts.items()}
        self.assertEqual(digests, expected)
        self.assertEqual(len(digests), 10)

    def test_missing_component(self):
        directory = write_components(self.tmp / "components")
        (directory / f"cargo-{VERSION}-{PRODUCER}.tar.gz").unlink()
        self.raises(f"missing components: ['cargo-{VERSION}-{PRODUCER}.tar.gz']", self.check, directory)

    def test_extra_component(self):
        directory = write_components(self.tmp / "components")
        (directory / "rust-src-1.99.0.tar.gz").write_bytes(b"x")
        self.raises("unexpected components: ['rust-src-1.99.0.tar.gz']", self.check, directory)

    def test_unknown_target(self):
        entries = component_entries()
        entries["rust-std"]["riscv64gc-unknown-linux-gnu"] = b"x"
        directory = write_components(self.tmp / "components", entries)
        self.raises("unknown target 'riscv64gc-unknown-linux-gnu'", self.check, directory, dist_members(entries=entries))

    def test_guest_target_for_rustc_is_unknown(self):
        entries = component_entries()
        entries["rustc"][GUEST] = b"x"
        directory = write_components(self.tmp / "components", entries)
        self.raises(f"unknown target '{GUEST}' for rustc", self.check, directory, dist_members(entries=entries))

    def test_unknown_package(self):
        extra = (f'\n[pkg.rust-src]\nversion = "{VERSION} (yurt {REVISION})"\n\n[pkg.rust-src.target.{PRODUCER}]\n'
                 f'available = true\nurl = "{PREFIX}/rust-src-{VERSION}-{PRODUCER}.tar.gz"\nhash = "{sha(b"x")}"\n')
        text = manifest_text(component_entries()).replace("\n[renames]", extra + "\n[renames]", 1)
        directory = write_components(self.tmp / "components", {**component_entries(), "rust-src": {PRODUCER: b"x"}})
        self.raises("unknown package 'rust-src'", self.check, directory, dist_members(manifest=text))

    def test_hash_mismatch(self):
        directory = write_components(self.tmp / "components")
        (directory / f"rustc-{VERSION}-{PRODUCER}.tar.gz").write_bytes(b"swapped bytes")
        self.raises(f"'rustc-{VERSION}-{PRODUCER}.tar.gz' does not match the manifest hash", self.check, directory)

    def test_url_under_another_tag(self):
        entries = component_entries()
        base = "https://github.com/YurtOS/yurt-packages/releases/download/rustup-1.99.0-2026-09-30"
        directory = write_components(self.tmp / "components", entries)
        self.raises(f"is not under {PREFIX}/", self.check, directory, dist_members(manifest=manifest_text(entries, base=base)))

    def test_url_under_another_host(self):
        entries = component_entries()
        base = f"https://example.com/YurtOS/yurt-packages/releases/download/{TAG}"
        directory = write_components(self.tmp / "components", entries)
        self.raises(f"is not under {PREFIX}/", self.check, directory, dist_members(manifest=manifest_text(entries, base=base)))

    def test_url_basename_is_not_the_component_name(self):
        entries = component_entries()
        text = manifest_text(entries).replace(f"/cargo-{VERSION}-{PRODUCER}.tar.gz", f"/sub/cargo-{VERSION}-{PRODUCER}.tar.gz")
        directory = write_components(self.tmp / "components", entries)
        self.raises("url for cargo", self.check, directory, dist_members(manifest=text))

    def test_hash_is_not_hex(self):
        entries = component_entries()
        digest = sha(entries["cargo"][PRODUCER])
        text = manifest_text(entries).replace(f'hash = "{digest}"', 'hash = "nothex"')
        directory = write_components(self.tmp / "components", entries)
        self.raises("hash for cargo", self.check, directory, dist_members(manifest=text))

    def test_symlinked_component(self):
        directory = write_components(self.tmp / "components")
        name = directory / f"cargo-{VERSION}-{PRODUCER}.tar.gz"
        real = self.tmp / "real.tar.gz"
        real.write_bytes(name.read_bytes())
        name.unlink()
        name.symlink_to(real)
        self.raises("is not a regular file", self.check, directory)

    def test_component_is_a_directory(self):
        directory = write_components(self.tmp / "components")
        (directory / "nested").mkdir()
        self.raises("'nested' is not a regular file", self.check, directory)


class DigestTests(Case):
    def test_shape(self):
        tar = self.tar_of()
        dist = dist_members()
        digests = stage.compute_digests(tar, dist, {"a.tar.gz": "1" * 64})
        self.assertEqual(digests, {
            "dist.tar": sha(tar.read_bytes()),
            "members": {name: sha(data) for name, data in dist.items()},
            "components": {"a.tar.gz": "1" * 64},
        })

    def test_without_components(self):
        digests = stage.compute_digests(self.tar_of(), dist_members(), None)
        self.assertNotIn("components", digests)

    def test_verify_accepts_equal_and_ignores_components(self):
        tar, dist = self.tar_of(), dist_members()
        expected = stage.compute_digests(tar, dist, {"a.tar.gz": "1" * 64})
        stage.verify_digests(expected, stage.compute_digests(tar, dist, None))

    def test_missing_entries(self):
        tar, dist = self.tar_of(), dist_members()
        actual = stage.compute_digests(tar, dist, None)
        no_tar = {k: v for k, v in actual.items() if k != "dist.tar"}
        self.raises("missing digest for dist.tar", stage.verify_digests, no_tar, actual)
        no_members = {"dist.tar": actual["dist.tar"]}
        self.raises("missing digest for members", stage.verify_digests, no_members, actual)
        short = {"dist.tar": actual["dist.tar"], "members": {k: v for k, v in actual["members"].items() if k != PROVENANCE}}
        self.raises(f"missing digest for {PROVENANCE}", stage.verify_digests, short, actual)
        self.raises("missing digest", stage.verify_digests, {}, actual)
        self.raises("digests are not a JSON object", stage.verify_digests, [], actual)

    def test_mismatched_entries(self):
        tar, dist = self.tar_of(), dist_members()
        actual = stage.compute_digests(tar, dist, None)
        bad_tar = {**actual, "dist.tar": "0" * 64}
        self.raises("digest mismatch for dist.tar", stage.verify_digests, bad_tar, actual)
        bad_member = {**actual, "members": {**actual["members"], DATED: "0" * 64}}
        self.raises(f"digest mismatch for {DATED}", stage.verify_digests, bad_member, actual)
        extra = {**actual, "members": {**actual["members"], "dist/extra": "0" * 64}}
        self.raises("unexpected digest for dist/extra", stage.verify_digests, extra, actual)


class ApplyTests(Case):
    def test_writes_exactly_the_five_files(self):
        site = self.site("2026-09-01")
        (site / "dist" / f"channel-rust-{VERSION}.toml").write_text("old moving manifest\n")
        written = stage.apply_dist(site, dist_members(), PLAN)
        self.assertEqual(sorted(written), sorted(FIVE))
        for name, data in dist_members().items():
            path = site / name
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o644, name)
            if name != PROVENANCE:
                self.assertEqual(path.read_bytes(), data)
        recorded = json.loads((site / PROVENANCE).read_text())
        self.assertEqual(recorded, {**provenance(), "tooling_sha": TOOLING_SHA})
        self.assertTrue((site / PROVENANCE).read_text().endswith("}\n"))

    def test_mode_ignores_umask(self):
        site = self.site()
        old = os.umask(0o077)
        try:
            stage.apply_dist(site, dist_members(), PLAN)
        finally:
            os.umask(old)
        for name in FIVE:
            self.assertEqual(stat.S_IMODE((site / name).lstat().st_mode), 0o644, name)

    def test_refuses_existing_dated_dir(self):
        self.raises(f"dist/{DATE} already exists", stage.apply_dist, self.site(DATE), dist_members(), PLAN)

    def test_refuses_symlinked_dist(self):
        site = self.tmp / "site"
        site.mkdir()
        (self.tmp / "elsewhere").mkdir()
        (site / "dist").symlink_to(self.tmp / "elsewhere")
        self.raises("dist is not a directory", stage.apply_dist, site, dist_members(), PLAN)
        self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])

    def test_refuses_symlinked_moving_manifest(self):
        site = self.site()
        victim = self.tmp / "victim"
        victim.write_text("keep\n")
        (site / MOVING).symlink_to(victim)
        self.raises("is not a regular file", stage.apply_dist, site, dist_members(), PLAN)
        self.assertEqual(victim.read_text(), "keep\n")

    def test_refuses_wrong_member_set(self):
        members = dist_members()
        del members[PROVENANCE]
        self.raises("missing members", stage.apply_dist, self.site(), members, PLAN)


CANONICAL = "dated manifest is not the canonical rendering"
EVIL = "https://evil.example/rustc.tar.xz"


def tampered(old, new):
    text = manifest_text(component_entries())
    assert old in text, old
    return text.replace(old, new, 1)


class CanonicalManifestTests(Case):
    def read(self, text):
        return stage.read_dist_tar(self.tar_of(dist_members(manifest=text)), PLAN)

    def target_header(self, package, target=PRODUCER):
        return f"[pkg.{package}.target.{target}]\navailable = true\n"

    def test_valid_manifest_round_trips(self):
        text = manifest_text(component_entries())
        self.assertEqual(self.read(text)[DATED], text.encode())

    def test_extra_xz_url(self):
        line = f'url = "{PREFIX}/rustc-{VERSION}-{PRODUCER}.tar.gz"\n'
        self.raises(CANONICAL, self.read, tampered(line, line + f'xz_url = "{EVIL}"\nxz_hash = "{"a" * 64}"\n'))

    def test_extra_zst_url(self):
        line = f'url = "{PREFIX}/cargo-{VERSION}-{PRODUCER}.tar.gz"\n'
        self.raises(CANONICAL, self.read, tampered(line, line + f'zst_url = "{EVIL}"\nzst_hash = "{"a" * 64}"\n'))

    def test_url_on_a_rust_target(self):
        header = self.target_header("rust")
        self.raises(CANONICAL, self.read, tampered(header, header + f'url = "{EVIL}"\nhash = "{"a" * 64}"\n'))

    def test_extra_top_level_key(self):
        self.raises(CANONICAL, self.read, tampered(f'date = "{DATE}"\n', f'date = "{DATE}"\nextra = "x"\n'))

    def test_changed_profile(self):
        self.raises(CANONICAL, self.read, tampered('minimal = ["rustc", "cargo", "rust-std"]', 'minimal = ["rustc"]'))

    def test_renames_entry(self):
        self.raises(CANONICAL, self.read, tampered("[renames]\n", '[renames]\nrls = { to = "rustc" }\n'))

    def test_changed_available(self):
        header = self.target_header("rustc")
        self.raises(CANONICAL, self.read, tampered(header, header.replace("available = true", "available = false")))

    def test_reordered_components(self):
        old = f'components = [{{ pkg = "rustc", target = "{PRODUCER}" }}, {{ pkg = "cargo", target = "{PRODUCER}" }}'
        new = f'components = [{{ pkg = "cargo", target = "{PRODUCER}" }}, {{ pkg = "rustc", target = "{PRODUCER}" }}'
        self.raises(CANONICAL, self.read, tampered(old, new))

    def test_altered_extensions(self):
        old = f'extensions = [{{ pkg = "rust-std", target = "{GUEST}" }}]'
        self.raises(CANONICAL, self.read, tampered(old, "extensions = []"))

    def test_formatting_change(self):
        self.raises(CANONICAL, self.read, tampered('manifest-version = "2"', "manifest-version = '2'"))

    def test_check_components_also_rejects_xz_url(self):
        line = f'url = "{PREFIX}/rustc-{VERSION}-{PRODUCER}.tar.gz"\n'
        text = tampered(line, line + f'xz_url = "{EVIL}"\nxz_hash = "{"a" * 64}"\n')
        directory = write_components(self.tmp / "components")
        self.raises(CANONICAL, stage.check_components, directory, dist_members(manifest=text), PLAN)


class ParserLimitTests(Case):
    def test_deeply_nested_provenance_json(self):
        data = b"[" * 200000
        self.assertLess(len(data), stage.MAX_MEMBER_BYTES)
        self.raises("provenance is not valid JSON", stage.read_dist_tar, self.tar_of(dist_members(prov=data)), PLAN)

    def test_deeply_nested_manifest_arrays(self):
        text = "a = " + "[" * 200000
        self.assertLess(len(text), stage.MAX_MEMBER_BYTES)
        self.raises("dated manifest is not valid TOML", stage.read_dist_tar, self.tar_of(dist_members(manifest=text)), PLAN)

    def test_manifest_integer_beyond_the_digit_limit(self):
        text = "a = " + "1" * 5000 + "\n"
        self.raises("dated manifest is not valid TOML", stage.read_dist_tar, self.tar_of(dist_members(manifest=text)), PLAN)

    def test_provenance_integer_beyond_the_digit_limit(self):
        data = b'{"a": ' + b"1" * 5000 + b"}"
        self.raises("provenance is not valid JSON", stage.read_dist_tar, self.tar_of(dist_members(prov=data)), PLAN)

    def test_deeply_nested_manifest_inline_tables(self):
        text = "a = " + "{ b = " * 100000
        self.assertLess(len(text), stage.MAX_MEMBER_BYTES)
        self.raises("dated manifest is not valid TOML", stage.read_dist_tar, self.tar_of(dist_members(manifest=text)), PLAN)

    def test_provenance_nan_and_infinity_are_refused(self):
        """json.loads accepts them, and apply would publish them as non-strict JSON."""
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                data = json.dumps(provenance(), indent=2, sort_keys=True).replace('"version": "0.0.1"', f'"version": {constant}', 1)
                self.assertIn(constant, data)
                self.raises("provenance is not valid JSON", stage.read_dist_tar, self.tar_of(dist_members(prov=(data + "\n").encode())), PLAN)

    def test_provenance_with_many_keys_is_parsed_in_linear_time(self):
        # 60k keys: the linear check takes well under a second; the old quadratic one (keys.count
        # per key) took over 140 s here. 10 s leaves room for a slow shared runner.
        many = {f"k{i}": 1 for i in range(60000)}
        start = time.perf_counter()
        self.assertEqual(len(stage.parse_provenance(json.dumps(many).encode())), 60000)
        self.assertLess(time.perf_counter() - start, 10.0)
        duplicated = (json.dumps(many)[:-1] + ', "k59999": 2}').encode()
        start = time.perf_counter()
        self.raises("duplicate keys ['k59999']", stage.parse_provenance, duplicated)
        self.assertLess(time.perf_counter() - start, 10.0)

    def test_provenance_overflowing_floats_are_refused(self):
        """1e999 parses to inf without calling parse_constant; push would then crash at allow_nan=False."""
        base = json.dumps(provenance(), indent=2, sort_keys=True)
        top = base.replace('"date": "', '"overflow": 1e999,\n  "date": "', 1)
        nested_value = provenance()
        first = next(iter(nested_value["archives"]))
        nested_value["archives"][first]["version"] = 7.5
        nested = json.dumps(nested_value, indent=2, sort_keys=True).replace("7.5", "-1e999", 1)
        for label, data in (("top level", top), ("nested in archives", nested)):
            with self.subTest(where=label):
                self.assertIn("1e999", data)
                self.raises("provenance is not valid JSON", stage.read_dist_tar, self.tar_of(dist_members(prov=(data + "\n").encode())), PLAN)

    def test_manifest_with_thousands_of_targets_is_refused_before_rendering(self):
        """render_manifest is quadratic in hosts x guests: unknown targets must be refused first, quickly."""
        label = f"{VERSION} (yurt {REVISION})"
        lines = ['manifest-version = "2"', f'date = "{DATE}"', "", "[pkg.rust]", f'version = "{label}"']
        targets = {"rustc": [f"t{i}" for i in range(2000)], "cargo": [], "rust-std": [f"g{i}" for i in range(2000)]}
        for package in PACKAGES:
            lines += ["", f"[pkg.{package}]", f'version = "{label}"']
            for target in [PRODUCER, *targets[package]]:
                lines += [f"[pkg.{package}.target.{target}]", 'url = "u"', 'hash = "h"']
        text = "\n".join(lines) + "\n"
        self.assertLess(len(text), stage.MAX_MEMBER_BYTES)
        start = time.perf_counter()
        self.raises("unknown target", stage.read_dist_tar, self.tar_of(dist_members(manifest=text)), PLAN)
        self.assertLess(time.perf_counter() - start, 10.0)


INJECTED = "x\n::error::injected\n::add-mask::secret"


class EscapingTests(Case):
    def assert_one_line(self, message):
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)

    def check_raises(self, text, directory=None):
        directory = directory or write_components(self.tmp / "components")
        with self.assertRaises(stage.StageError) as caught:
            stage.check_components(directory, dist_members(manifest=text), PLAN)
        self.assert_one_line(str(caught.exception))
        return str(caught.exception)

    def test_package_key_with_newline(self):
        text = manifest_text(component_entries()) + '\n[pkg."x\\n::error::injected"]\nversion = "v"\n'
        self.assertIn("unknown package 'x\\n::error::injected'", self.check_raises(text))

    def test_target_key_with_newline(self):
        text = manifest_text(component_entries()) + '\n[pkg.rust-std.target."x\\n::error::injected"]\nurl = "u"\nhash = "h"\n'
        self.assertIn("unknown target 'x\\n::error::injected'", self.check_raises(text))

    def test_rust_target_key_with_newline(self):
        text = manifest_text(component_entries()) + '\n[pkg.rust.target."x\\n::error::injected"]\navailable = true\n'
        self.assertIn("unknown target 'x\\n::error::injected' for rust", self.check_raises(text))

    def test_component_file_name_with_newline(self):
        directory = write_components(self.tmp / "components")
        (directory / INJECTED).write_bytes(b"x")
        self.assertIn("unexpected components", self.check_raises(manifest_text(component_entries()), directory))

    def test_component_link_name_with_newline(self):
        directory = write_components(self.tmp / "components")
        (directory / INJECTED).symlink_to(directory / f"cargo-{VERSION}-{PRODUCER}.tar.gz")
        self.assertIn("is not a regular file", self.check_raises(manifest_text(component_entries()), directory))

    def test_member_name_with_newline(self):
        tar = self.tar_of([*dist_members().items(), (f"dist/{INJECTED}", b"x")])
        with self.assertRaises(stage.StageError) as caught:
            stage.read_dist_tar(tar, PLAN)
        self.assert_one_line(str(caught.exception))

    def test_producer_host_message_with_newline_host(self):
        entries = component_entries()
        for package in PACKAGES:
            entries[package] = {INJECTED if t == PRODUCER else t: d for t, d in entries[package].items()}
        escaped = INJECTED.replace("\n", "\\n")
        text = manifest_text(entries).replace(f".{INJECTED}]", f'."{escaped}"]').replace(INJECTED, escaped)
        with self.assertRaises(stage.StageError) as caught:
            stage.read_dist_tar(self.tar_of(dist_members(manifest=text)), PLAN)
        self.assertIn("producer host", str(caught.exception))
        self.assert_one_line(str(caught.exception))

    def test_cli_stderr_carries_no_workflow_command(self):
        staging = self.staging()
        components = write_components(self.tmp / "components")
        (components / INJECTED).write_bytes(b"x")
        env = {
            "PATH": os.environ.get("PATH", ""), "PLAN_RELEASE_TAG": RELEASE, "PLAN_VERSION": VERSION, "PLAN_REVISION": REVISION,
            "PLAN_DATE": DATE, "PLAN_PRODUCER_HOST": PRODUCER, "PLAN_TOOLING_SHA": TOOLING_SHA, "PLAN_TAG": TAG,
            "OUTPUT_FILE": str(self.tmp / "output"),
        }
        done = subprocess.run([sys.executable, "-I", str(TOOL), "validate-components", "--dist-dir", str(staging), "--dir", str(components)],
                              env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 1, done.stderr)
        lines = done.stderr.splitlines()
        self.assertEqual(len(lines), 1, done.stderr)
        self.assertTrue(lines[0].startswith("rustup_dist_stage: "), done.stderr)
        self.assertIn("unexpected components", lines[0])

    def test_main_neutralises_control_characters(self):
        stderr = io.StringIO()
        with mock.patch.object(stage, "run", side_effect=stage.StageError("a\nb\r\x1b[31m c")), contextlib.redirect_stderr(stderr):
            self.assertEqual(stage.main(["plan", "--tooling-sha", "s"]), 1)
        out = stderr.getvalue()
        self.assertEqual(out.count("\n"), 1)
        self.assertTrue(out.endswith("\n"))
        for char in ("\r", "\x1b", " "):
            self.assertNotIn(char, out)


if __name__ == "__main__":
    unittest.main()
