"""Command-line tests for tools/rustup_dist_stage.py: each subcommand's contract as the phases call it."""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
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
    MOVING,
    PLAN,
    PRODUCER,
    PROVENANCE,
    RELEASE,
    REVISION,
    TAG,
    TOOL,
    TOOLING_SHA,
    VERSION,
    dist_members,
    stage,
    write_components,
    write_tar,
)


class CliTests(Case):
    def plan_env(self, **overrides):
        env = {
            "PLAN_RELEASE_TAG": RELEASE, "PLAN_VERSION": VERSION, "PLAN_REVISION": REVISION, "PLAN_DATE": DATE,
            "PLAN_PRODUCER_HOST": PRODUCER, "PLAN_TOOLING_SHA": TOOLING_SHA, "PLAN_TAG": TAG,
            "OUTPUT_FILE": str(self.tmp / "output"),
        }
        env.update(overrides)
        return env

    def main(self, argv, env):
        stderr = io.StringIO()
        stdout = io.StringIO()
        clean = {k: v for k, v in os.environ.items() if not k.startswith("PLAN_") and k not in ("OUTPUT_FILE", "DIGESTS")}
        with mock.patch.dict(os.environ, {**clean, **env}, clear=True), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            try:
                code = stage.main(argv)
            except SystemExit as error:  # argparse refusing the command line
                code = error.code
        return code, stdout.getvalue(), stderr.getvalue()

    def outputs(self):
        lines = (self.tmp / "output").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines)

    def inputs(self, **overrides):
        """The plan job's inputs, as the workflow passes them (environment only)."""
        env = {"RUST_RELEASE": RELEASE, "PUBLISH_DATE": DATE, "PRODUCER_HOST": PRODUCER, "OUTPUT_FILE": str(self.tmp / "output")}
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def test_plan_appends_outputs(self):
        (self.tmp / "output").write_text("earlier=kept\n")
        code, _, err = self.main(["plan", "--tooling-sha", TOOLING_SHA], self.inputs())
        self.assertEqual(code, 0, err)
        self.assertEqual(self.outputs(), {
            "earlier": "kept", "release_tag": RELEASE, "version": VERSION, "revision": REVISION, "date": DATE,
            "producer_host": PRODUCER, "tooling_sha": TOOLING_SHA, "tag": TAG,
        })

    def test_plan_empty_date_is_today_utc(self):
        code, _, err = self.main(["plan", "--tooling-sha", TOOLING_SHA], self.inputs(PUBLISH_DATE=""))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.outputs()["date"], datetime.datetime.now(datetime.timezone.utc).date().isoformat())

    def test_plan_rejects_bad_input(self):
        code, _, err = self.main(["plan", "--tooling-sha", "abc"], self.inputs())
        self.assertEqual(code, 1)
        self.assertIn("is not a 40-hex commit SHA", err)
        self.assertFalse((self.tmp / "output").exists())

    def test_check_inputs_names_the_bad_variable_and_writes_nothing(self):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).date().isoformat()
        bad = {
            "RUST_RELEASE": [None, "", "rust-v1.99-0.0.1", "rust-v1.99.0-0.0.1; id", "rust-v1.99.0-0.0.1\n"],
            "PUBLISH_DATE": ["2026-02-30", "20261020", future],
            "PRODUCER_HOST": [None, "x86_64-apple-darwin", "x86_64-unknown-linux-gnu x"],
        }
        code, out, err = self.main(["check-inputs"], self.inputs())
        self.assertEqual((code, out), (0, ""), err)
        for name, values in bad.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    code, out, err = self.main(["check-inputs"], self.inputs(**{name: value}))
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn(name, err)
                    self.assertFalse((self.tmp / "output").exists())

    def test_check_plan(self):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).date().isoformat()
        code, out, err = self.main(["check-plan"], self.plan_env())
        self.assertEqual((code, out), (0, ""), err)
        for override in ({"PLAN_RELEASE_TAG": ""}, {"PLAN_DATE": future, "PLAN_TAG": f"rustup-{VERSION}-{future}"},
                         {"PLAN_PRODUCER_HOST": "x86_64-apple-darwin"}, {"PLAN_VERSION": "1.99.1"}):
            with self.subTest(override=override):
                code, _, err = self.main(["check-plan"], self.plan_env(**override))
                self.assertEqual(code, 1)
                self.assertIn("PLAN_", err)

    def test_check_release(self):
        release = {"release": {"tag": RELEASE, "rust_version": VERSION, "revision": REVISION}, "assets": []}
        good = json.dumps(release)
        cases = {
            "wrong tag": good.replace(RELEASE, "rust-v1.99.0-0.0.2"),
            "wrong version": good.replace(f'"{VERSION}"', '"1.99.1"'),
            "wrong revision": good.replace(f'"{REVISION}"', '"0.0.2"'),
            "not json": good[:-1],
            "not an object": "[1]",
            "no release": json.dumps({"assets": []}),
            "release not an object": json.dumps({"release": "x"}),
            "duplicate key": good[:-1] + f', "release": {json.dumps(release["release"])}}}',
            "NaN": good.replace('"assets": []', '"assets": NaN'),
            "overflowing float": good.replace('"assets": []', '"assets": 1e999'),
            "oversized": json.dumps({**release, "pad": "x" * stage.MAX_MEMBER_BYTES}),
        }
        path = self.tmp / "release-manifest.json"
        path.write_text(good)
        code, out, err = self.main(["check-release", "--manifest", str(path)], self.plan_env())
        self.assertEqual((code, out), (0, ""), err)
        for name, text in cases.items():
            with self.subTest(case=name):
                path.write_text(text)
                code, _, err = self.main(["check-release", "--manifest", str(path)], self.plan_env())
                self.assertEqual(code, 1)
                self.assertIn("release manifest", err)
                self.assertIn("revision", err)
        code, _, err = self.main(["check-release", "--manifest", str(self.tmp / "missing.json")], self.plan_env())
        self.assertEqual(code, 1)

    def emitted_site(self, members=None):
        """A site as the emitter leaves it: the five files under site/dist."""
        site = self.tmp / "emitted"
        for name, data in (members or dist_members()).items():
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_bytes(data)
        return site

    def test_pack_writes_exactly_the_five_members_and_self_checks(self):
        site, out = self.emitted_site(), self.tmp / "out" / "dist.tar"
        out.parent.mkdir()
        code, stdout, err = self.main(["pack", "--site", str(site), "--out", str(out)], self.plan_env())
        self.assertEqual((code, stdout), (0, ""), err)
        with tarfile.open(out, "r:") as tar:
            self.assertEqual(tar.format, tarfile.PAX_FORMAT)
            members = tar.getmembers()
            self.assertEqual([m.name for m in members], list(FIVE))
            self.assertEqual({(m.type, m.mode, m.mtime) for m in members}, {(tarfile.REGTYPE, 0o644, 0)})
        self.assertEqual(stage.read_dist_tar(out, PLAN), dist_members())
        code, _, err = self.main(["pack", "--site", str(site), "--out", str(out)], self.plan_env())
        self.assertEqual(code, 1, "an existing output is never overwritten")

    def test_extract_writes_only_the_five_files(self):
        staging, out = self.staging(), self.tmp / "extracted"
        code, stdout, err = self.main(["extract", "--dir", str(staging), "--out", str(out)], self.plan_env())
        self.assertEqual((code, stdout), (0, ""), err)
        found = sorted(str(p.relative_to(out)) for p in out.rglob("*") if not p.is_dir())
        self.assertEqual(found, sorted(FIVE))
        for name, data in dist_members().items():
            self.assertFalse((out / name).is_symlink())
            self.assertEqual((out / name).read_bytes(), data)
            self.assertEqual(stat.S_IMODE((out / name).stat().st_mode), 0o644)

    def test_extract_refuses_before_writing_anything(self):
        def hostile(_):
            directory = self.tmp / "hostile"
            directory.mkdir()
            write_tar(directory / "dist.tar", [*dist_members().items(), ("dist/../../escaped", b"x")])
            return directory

        def busy(out):
            out.mkdir()
            (out / "stale").write_text("x")

        cases = {
            "traversal member": (hostile, None, "member path contains .."),
            "output not empty": (lambda _: self.staging(), busy, "must be an empty directory or absent"),
            "output is a symlink": (lambda _: self.staging(), lambda out: out.symlink_to(self.tmp), "must be an empty directory or absent"),
        }
        for name, (source, prepare, message) in cases.items():
            with self.subTest(case=name):
                subprocess.run(["rm", "-rf", str(self.tmp / "staging"), str(self.tmp / "hostile"), str(self.tmp / "extracted")], check=True)
                out = self.tmp / "extracted"
                if prepare:
                    prepare(out)
                code, _, err = self.main(["extract", "--dir", str(source(out)), "--out", str(out)], self.plan_env())
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertFalse((self.tmp.parent / "escaped").exists())
                self.assertFalse(os.path.lexists(out / "dist"))

    def test_pack_refuses_what_the_emitter_did_not_write_as_regular_files(self):
        def link_file(site):
            (site / MOVING).unlink()
            (site / MOVING).symlink_to(site / DATED)

        def link_dir(site):
            real = self.tmp / "elsewhere"
            (site / "dist" / DATE).rename(real)
            (site / "dist" / DATE).symlink_to(real)

        def directory(site):
            (site / PROVENANCE).unlink()
            (site / PROVENANCE).mkdir()

        cases = {
            "missing file": (lambda site: (site / DATED_SHA).unlink(), "regular file"),
            "symlinked file": (link_file, "regular file"),
            "symlinked dated dir": (link_dir, "regular file"),
            "directory": (directory, "regular file"),
            "bad provenance": (lambda site: (site / PROVENANCE).write_bytes(b'{"release_tag": '), "provenance is not valid JSON"),
        }
        for name, (change, message) in cases.items():
            with self.subTest(case=name):
                subprocess.run(["rm", "-rf", str(self.tmp / "emitted"), str(self.tmp / "elsewhere"), str(self.tmp / "out")], check=True)
                site, out = self.emitted_site(), self.tmp / "out" / "dist.tar"
                out.parent.mkdir()
                change(site)
                code, _, err = self.main(["pack", "--site", str(site), "--out", str(out)], self.plan_env())
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertFalse(out.exists(), "nothing is left behind on a refusal")

    def test_inconsistent_plan_env_is_rejected(self):
        for key, value in (("PLAN_TAG", "rustup-1.99.0-2026-09-30"), ("PLAN_VERSION", "1.98.0"), ("PLAN_REVISION", "0.0.2"), ("PLAN_DATE", "")):
            with self.subTest(key=key):
                subprocess.run(["rm", "-rf", str(self.tmp / "staging"), str(self.tmp / "site")], check=True)
                code, _, err = self.main(["validate-dist", "--dir", str(self.staging())], self.plan_env(**{key: value}))
                self.assertEqual(code, 1)
                self.assertIn("PLAN_", err)

    def test_validate_dist(self):
        code, _, err = self.main(["validate-dist", "--dir", str(self.staging())], self.plan_env())
        self.assertEqual(code, 0, err)

    def test_validate_dist_takes_no_site(self):
        """The date rules are check-dates' and apply's: validate-dist has no site to skip them with."""
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            stage.main(["validate-dist", "--dir", str(self.staging()), "--site", str(self.site())])

    def test_check_dates(self):
        cases = {
            "older dated dir": (self.site("2026-09-01"), None),
            "no dist yet": (self.tmp / "bare", None),
            "same date": (self.tmp / "same", "already exists"),
            "newer dated dir": (self.tmp / "newer", "must be later than the newest dated dir"),
            "missing site": (self.tmp / "missing", "is not a directory"),
        }
        cases["no dist yet"][0].mkdir()
        (self.tmp / "same" / "dist" / DATE).mkdir(parents=True)
        (self.tmp / "newer" / "dist" / "2026-10-05").mkdir(parents=True)
        for name, (site, message) in cases.items():
            with self.subTest(case=name):
                code, out, err = self.main(["check-dates", "--site", str(site)], self.plan_env())
                self.assertEqual((code, out), (0 if message is None else 1, ""), err)
                if message:
                    self.assertIn(message, err)

    def test_validate_dist_rejects_hostile_tar(self):
        directory = self.tmp / "staging"
        directory.mkdir()
        write_tar(directory / "dist.tar", [*dist_members().items(), ("dist/../x", b"x")])
        code, _, err = self.main(["validate-dist", "--dir", str(directory)], self.plan_env())
        self.assertEqual(code, 1)
        self.assertIn("rustup_dist_stage: ", err)
        self.assertIn("member path contains ..", err)

    def test_validate_components_writes_digests(self):
        staging = self.staging()
        components = write_components(self.tmp / "components")
        code, _, err = self.main(["validate-components", "--dist-dir", str(staging), "--dir", str(components)], self.plan_env())
        self.assertEqual(code, 0, err)
        raw = self.outputs()["digests"]
        self.assertNotIn(" ", raw)
        expected = stage.compute_digests(staging / "dist.tar", dist_members(), stage.check_components(components, dist_members(), PLAN))
        self.assertEqual(json.loads(raw), expected)

    def test_validate_components_rejects_mismatch(self):
        staging = self.staging()
        components = write_components(self.tmp / "components")
        (components / f"cargo-{VERSION}-{PRODUCER}.tar.gz").write_bytes(b"other")
        code, _, err = self.main(["validate-components", "--dist-dir", str(staging), "--dir", str(components)], self.plan_env())
        self.assertEqual(code, 1)
        self.assertIn("does not match the manifest hash", err)
        self.assertFalse((self.tmp / "output").exists())

    def digests_for(self, staging):
        return json.dumps(stage.compute_digests(staging / "dist.tar", dist_members(), {"x": "1" * 64}), separators=(",", ":"))

    def test_apply(self):
        staging, site = self.staging(), self.site("2026-09-01")
        code, out, err = self.main(["apply", "--dist-dir", str(staging), "--site", str(site)], self.plan_env(DIGESTS=self.digests_for(staging)))
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(out.split()), sorted(FIVE))
        self.assertEqual(json.loads((site / PROVENANCE).read_text())["tooling_sha"], TOOLING_SHA)

    def test_apply_requires_digests(self):
        staging, site = self.staging(), self.site()
        for digests in (None, "", "not json", "{}"):
            with self.subTest(digests=digests):
                env = self.plan_env() if digests is None else self.plan_env(DIGESTS=digests)
                code, _, err = self.main(["apply", "--dist-dir", str(staging), "--site", str(site)], env)
                self.assertEqual(code, 1)
                self.assertIn("digest", err)
                self.assertFalse((site / "dist" / DATE).exists())

    def test_apply_rejects_mismatched_digest(self):
        staging, site = self.staging(), self.site()
        digests = json.loads(self.digests_for(staging))
        digests["members"][MOVING] = "0" * 64
        code, _, err = self.main(["apply", "--dist-dir", str(staging), "--site", str(site)], self.plan_env(DIGESTS=json.dumps(digests)))
        self.assertEqual(code, 1)
        self.assertIn(f"digest mismatch for {MOVING}", err)
        self.assertFalse((site / "dist" / DATE).exists())

    def test_apply_runs_date_rules_before_writing_anything(self):
        for dated, message in ((DATE, "already exists"), ("2026-10-05", "must be later than")):
            with self.subTest(dated=dated):
                subprocess.run(["rm", "-rf", str(self.tmp / "staging"), str(self.tmp / "site")], check=True)
                staging, site = self.staging(), self.site(dated)
                code, _, err = self.main(["apply", "--dist-dir", str(staging), "--site", str(site)], self.plan_env(DIGESTS=self.digests_for(staging)))
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(sorted(p.name for p in (site / "dist").iterdir()), [dated])

    def test_runs_isolated_as_a_script(self):
        done = subprocess.run(
            [sys.executable, "-I", str(TOOL), "plan", "--tooling-sha", TOOLING_SHA],
            env={"PATH": os.environ.get("PATH", ""), **self.inputs()}, capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.outputs()["tag"], TAG)
