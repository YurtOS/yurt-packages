"""Tests for the plan, emit, publish and push phases of tools/publish_rustup_dist.sh.

Each phase runs in its own process and temp directory with only the environment its workflow job
gets. A gh shim, a local bare Pages remote and stub tooling stand in for GitHub. The stub emitter
writes the real emitter's layout, and the stub checker records what it was given. The scripts run
from a `trusted/` copy, distinct from every `site/` checkout.
"""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
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


_load_support("support_publish")
from support_publish import (  # noqa: E402
    AUTH_HEADER,
    COMPONENTS,
    DATE,
    FIVE,
    GITHUB,
    Harness,
    MOVING,
    PREFIX,
    PRODUCER,
    PROVENANCE,
    PUBLISHED,
    PUSH,
    READ,
    RELEASE,
    REVISION,
    SECRETS,
    TAG,
    VERSION,
    git,
    pack,
    stage,
    tar_members,
)


class FixtureTests(Harness):
    def test_stub_emitter_output_passes_the_validator(self):
        """Fixture sanity: the stub emitter's layout is what the real emitter writes and the validator accepts."""
        dist_dir, components_dir = self.make_artifacts()
        dist = stage.read_dist_dir(dist_dir, self.plan)
        self.assertEqual(sorted(stage.check_components(components_dir, dist, self.plan)), COMPONENTS)

    def test_stub_emitter_fails_in_isolated_mode(self):
        done = subprocess.run(["python3", "-I", str(self.tooling / "scripts" / "emit_rustup_dist.py"), "--help"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("sdk_hosts", done.stderr)


class PhaseDispatchTests(Harness):
    def test_unknown_or_missing_phase_is_refused(self):
        for name in ("deploy", "", "check"):
            with self.subTest(name=name):
                result = self.run_phase(name, self.plan_inputs())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("usage", result.stderr)
                self.assertEqual(result.calls(), [])


class PlanPhaseTests(Harness):
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
                    result = self.run_phase("plan", self.plan_inputs(**{name: value}))
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(result.calls(), [])
                    self.assertEqual(result.outputs(), {})
        result = self.run_phase("plan", self.plan_inputs(RUST_RELEASE=None))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.calls(), [])
        self.assert_no_release_mutation()
        self.assert_nothing_pushed()

    def test_future_date_rejected_and_empty_date_means_today_utc(self):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).strftime("%F")
        result = self.run_phase("plan", self.plan_inputs(PUBLISH_DATE=future))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PUBLISH_DATE", result.stderr)
        self.assertEqual(result.calls(), [])
        self.assertEqual(result.outputs(), {})
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%F")
        result = self.run_phase("plan", self.plan_inputs(PUBLISH_DATE=""))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.outputs()["date"], today)
        self.assertEqual(result.outputs()["tag"], f"rustup-{VERSION}-{today}")

    def test_plan_outputs_the_identity_and_resolves_the_tooling_sha_with_the_read_token(self):
        result = self.run_phase("plan", self.plan_inputs())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.outputs(), {
            "release_tag": RELEASE, "version": VERSION, "revision": REVISION, "date": DATE,
            "producer_host": PRODUCER, "tooling_sha": self.tooling_sha, "tag": TAG,
        })
        self.assertEqual(result.calls(), ["api repos/YurtOS/yurt-rust/commits/main --jq .sha"])
        self.assertEqual(result.tokens(), {f"{READ}||"})

    def test_plan_requires_a_40_hex_tooling_sha(self):
        for sha in ("", "abc", "A" * 40, "a" * 39, "a" * 41, "g" * 40, "a" * 40 + " x"):
            with self.subTest(sha=sha):
                result = self.run_phase("plan", self.plan_inputs(FAKE_SHA=sha))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("TOOLING_REF", result.stderr)
                self.assertEqual(result.outputs(), {})
        result = self.run_phase("plan", self.plan_inputs(FAKE_API_FAIL="1"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.outputs(), {})


class EmitPhaseTests(Harness):
    def test_emit_writes_a_dist_tar_of_exactly_the_five_members_and_the_components(self):
        result = self.emit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(os.listdir(result.out)), ["components", "dist"])
        self.assertEqual(os.listdir(result.out / "dist"), ["dist.tar"])
        with tarfile.open(result.out / "dist" / "dist.tar", "r:") as tar:
            members = tar.getmembers()
        self.assertEqual(sorted(m.name for m in members), sorted(FIVE))
        self.assertTrue(all(m.isreg() for m in members))
        dist = stage.read_dist_dir(result.out / "dist", self.plan)
        self.assertEqual(sorted(stage.check_components(result.out / "components", dist, self.plan)), COMPONENTS)
        self.assertEqual(len(result.calls()), 1)
        self.assertTrue(result.calls()[0].startswith(f"release download {RELEASE} -R YurtOS/yurt-rust -D "))
        emitted = (self.logs / "emit.log").read_text()
        self.assertIn(f"--base-url {PREFIX}", emitted)
        self.assertIn(f"--producer-host {PRODUCER}", emitted)
        self.assertIn(f"--date {DATE}", emitted)
        self.assertIn(f"--components {result.out / 'components'}", emitted)
        # The emitter wrote into the anonymous checkout; emit leaves it as it found it.
        self.assertEqual(git("status", "--porcelain", "--ignored", cwd=result.env["YURT_SITE_DIR"]), "")
        self.assert_no_release_mutation()
        self.assert_nothing_pushed()

    def test_emit_holds_no_write_token_and_the_emitter_sees_no_token(self):
        result = self.emit(GITHUB_TOKEN=GITHUB, PUSH_TOKEN=PUSH, GH_ENTERPRISE_TOKEN=GITHUB)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.tokens(), {f"{READ}||"})
        self.assert_no_secret_in(self.logs / "emit.env")
        self.assertEqual((self.logs / "emit.leak").read_text(), "")

    def test_tag_revision_must_match_release_manifest(self):
        for override in ({"FAKE_REVISION": "0.0.2"}, {"FAKE_VERSION": "1.99.1"}, {"FAKE_TAG": "rust-v1.99.0-0.0.2"}):
            with self.subTest(override=override):
                result = self.emit(**override)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("revision", result.stderr)
                self.assertEqual(result.verbs(), ["release download"])
                self.assertFalse((self.logs / "emit.log").exists())
                self.assertFalse((result.out / "dist" / "dist.tar").exists())
        self.assert_no_release_mutation()
        self.assert_nothing_pushed()

    def test_emit_refuses_plan_values_that_do_not_derive_from_the_inputs(self):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)).strftime("%F")
        cases = {
            "tag": {"PLAN_TAG": f"rustup-{VERSION}-2026-10-02"},
            "version": {"PLAN_VERSION": "1.99.1"},
            "revision": {"PLAN_REVISION": "0.0.2"},
            "empty date": {"PLAN_DATE": "", "PLAN_TAG": f"rustup-{VERSION}-"},
            "future date": {"PLAN_DATE": future, "PLAN_TAG": f"rustup-{VERSION}-{future}"},
            "host": {"PLAN_PRODUCER_HOST": "x86_64-apple-darwin"},
            "missing": {"PLAN_RELEASE_TAG": None},
        }
        for name, override in cases.items():
            with self.subTest(case=name):
                result = self.emit(self.plan_values(**override))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PLAN_", result.stderr)
                self.assertEqual(result.calls(), [])
        self.assert_nothing_pushed()

    def test_emit_requires_the_tooling_checkout_at_the_planned_sha(self):
        result = self.emit(self.plan_values(PLAN_TOOLING_SHA="1" * 40))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PLAN_TOOLING_SHA", result.stderr)
        self.assertEqual(result.calls(), [])

    def test_emit_self_check_rejects_malformed_provenance_json(self):
        result = self.emit(FAKE_EMIT_MODE="bad-provenance")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance is not valid JSON", result.stderr)
        self.assertFalse((result.out / "dist" / "dist.tar").exists())
        self.assertEqual(git("status", "--porcelain", "--ignored", cwd=result.env["YURT_SITE_DIR"]), "")
        self.assert_no_release_mutation()
        self.assert_nothing_pushed()

    def test_emitter_failure_leaves_no_dist_tar(self):
        result = self.emit(FAKE_EMIT_MODE="fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failing as asked", result.stderr)
        self.assertFalse((result.out / "dist" / "dist.tar").exists())

    def test_work_dir_must_be_empty_or_absent(self):
        busy = self.root / "busy"
        busy.mkdir()
        (busy / "stale").write_text("x")
        result = self.emit(WORK_DIR=str(busy))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WORK_DIR", result.stderr)
        self.assertEqual(result.calls(), [])
        kept = self.root / "scratch"
        result = self.emit(WORK_DIR=str(kept))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((kept / "release" / "release-manifest.json").exists())

    def test_out_dir_must_be_empty_or_absent(self):
        busy = self.root / "busy-out"
        busy.mkdir()
        (busy / "stale").write_text("x")
        result = self.emit(OUT_DIR=str(busy))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("OUT_DIR", result.stderr)
        self.assertEqual(result.calls(), [])

    def test_unset_work_dir_scratch_is_removed(self):
        result = self.emit(WORK_DIR=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_startup_crash_does_not_look_like_a_refusal(self):
        result = self.emit(YURT_RUST_TOOLING_DIR=str(self.root / "missing"))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.calls(), [])
        self.assertNotIn("new date", result.stderr)


class PublishPhaseTests(Harness):
    def test_publish_validates_creates_the_release_uploads_and_outputs_digests(self):
        dist_dir, components_dir = self.make_artifacts()
        result = self.publish(dist_dir, components_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.verbs(), ["release view", "release create", "release upload"])
        create = result.calls()[1]
        self.assertTrue(create.startswith(f"release create {TAG} -R YurtOS/yurt-packages "), create)
        self.assertIn("--latest=false", create)
        self.assertIn(self.tooling_sha, create)
        self.assertEqual(sorted(os.listdir(self.state / f"{TAG}.assets")), COMPONENTS)
        for name in COMPONENTS:
            self.assertEqual((self.state / f"{TAG}.assets" / name).read_bytes(), (components_dir / name).read_bytes())
        self.assertEqual(sorted(result.outputs()), ["digests"])
        self.assertEqual(json.loads(result.outputs()["digests"]), json.loads(self.digests_for(dist_dir, components_dir)))
        self.assertEqual(result.tokens(), {f"{GITHUB}||"})
        self.assert_nothing_pushed()

    def test_published_manifest_and_existing_release_refuses(self):
        head = self.land_manifest()
        (self.state / TAG).touch()
        self.assert_refused(self.publish(), "new date", head)
        self.assertTrue((self.state / TAG).exists())

    def test_published_manifest_without_release_never_recreates_assets(self):
        head = self.land_manifest()
        self.assert_refused(self.publish(), "new date", head)
        self.assertFalse((self.state / TAG).exists())

    def test_existing_release_without_manifest_is_deleted_and_recreated(self):
        (self.state / TAG).touch()
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.verbs(), ["release view", "release delete", "release create", "release upload"])
        self.assertIn("digests", result.outputs())

    def test_stale_checkout_sees_manifest_landed_mid_run_and_does_not_delete(self):
        (self.state / TAG).touch()
        dist_dir, components_dir = self.make_artifacts()
        site = self.fresh_site()
        head = self.land_manifest()  # lands after the job's checkout
        self.assert_refused(self.publish(dist_dir, components_dir, YURT_SITE_DIR=str(site)), "new date", head)
        self.assertTrue((self.state / TAG).exists())

    def test_release_is_mutated_only_after_the_date_rule_recheck(self):
        (self.state / TAG).touch()
        dist_dir, components_dir = self.make_artifacts()
        site = self.fresh_site()
        head = self.land({"dist/2026-10-05/channel-rust-1.99.1.toml": "newer\n"}, "newer publication")
        self.assert_refused(self.publish(dist_dir, components_dir, YURT_SITE_DIR=str(site)), "later than", head)
        self.assertTrue((self.state / TAG).exists())

    def test_upload_failure_outputs_no_digests(self):
        result = self.publish(FAKE_UPLOAD_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.verbs()[-1:], ["release upload"])
        self.assertEqual(result.outputs(), {})
        self.assert_nothing_pushed()

    def test_publish_refuses_a_rerun_attempt(self):
        for attempt in ("2", "", None, "1 "):
            with self.subTest(attempt=attempt):
                self.assert_refused(self.publish(GITHUB_RUN_ATTEMPT=attempt), "GITHUB_RUN_ATTEMPT")

    def test_publish_outer_check_requires_exactly_one_regular_dist_tar(self):
        def neighbour_file(d):
            (d / "extra").write_text("x")

        def neighbour_dir(d):
            (d / "dist").mkdir()

        def symlinked(d):
            (d / "dist.tar").rename(self.root / "elsewhere.tar")
            (d / "dist.tar").symlink_to(self.root / "elsewhere.tar")

        def directory(d):
            (d / "dist.tar").unlink()
            (d / "dist.tar").mkdir()

        def missing(d):
            (d / "dist.tar").unlink()

        dist_dir, components_dir = self.make_artifacts()
        for change in (neighbour_file, neighbour_dir, symlinked, directory, missing):
            with self.subTest(case=change.__name__):
                staged = self.staged(dist_dir)
                change(staged)
                self.assert_refused(self.publish(staged, self.staged(components_dir)), "dist.tar")

    def test_publish_rejects_malformed_provenance_json(self):
        dist_dir, components_dir = self.make_artifacts()
        members = tar_members(dist_dir / "dist.tar")
        members[PROVENANCE] = b'{"release_tag": '
        pack(dist_dir, dist_dir / "dist.tar", list(members.items()))
        self.assert_refused(self.publish(dist_dir, components_dir), "provenance is not valid JSON")

    def test_publish_rejects_hostile_dist_tar_members(self):
        dist_dir, components_dir = self.make_artifacts()
        good = list(tar_members(dist_dir / "dist.tar").items())

        def as_symlink(info):
            info.type, info.linkname, info.size = tarfile.SYMTYPE, "/etc/passwd", 0

        cases = {
            "traversal": ([*good, ("dist/../evil", b"x")], ".."),
            "absolute": ([*good, ("/tmp/evil", b"x")], "absolute"),
            "symlink": ([*good[:-1], (good[-1][0], b"", as_symlink)], "not a regular file"),
            "extra": ([*good, ("dist/extra.toml", b"x")], "unexpected member"),
            "missing": (good[:-1], "missing members"),
            "duplicate": ([*good, good[0]], "duplicate member"),
        }
        for name, (members, message) in cases.items():
            with self.subTest(case=name):
                staged = self.staged(dist_dir)
                pack(staged, staged / "dist.tar", members)
                self.assert_refused(self.publish(staged, self.staged(components_dir)), message)

    def test_publish_rejects_artifacts_with_the_wrong_identity(self):
        dist_dir, components_dir = self.make_artifacts()
        other_revision = self.plan_values(PLAN_RELEASE_TAG=f"rust-v{VERSION}-0.0.2", PLAN_REVISION="0.0.2")
        other_date = self.plan_values(PLAN_DATE="2026-10-02", PLAN_TAG=f"rustup-{VERSION}-2026-10-02")
        other_host = self.plan_values(PLAN_PRODUCER_HOST="aarch64-apple-darwin")
        for name, plan, message in (("revision", other_revision, "manifest version"), ("date", other_date, "unexpected member"),
                                    ("host", other_host, "does not match the plan")):
            with self.subTest(case=name):
                self.assert_refused(self.publish(self.staged(dist_dir), self.staged(components_dir), plan=plan), message)

    def test_publish_rejects_components_that_do_not_match_the_manifest(self):
        dist_dir, components_dir = self.make_artifacts()

        def extra(d):
            (d / f"rustc-{VERSION}-x86_64-apple-darwin.tar.gz").write_bytes(b"x")

        def missing(d):
            (d / COMPONENTS[0]).unlink()

        def swapped(d):
            (d / COMPONENTS[0]).write_bytes(b"other bytes")

        for change, message in ((extra, "unexpected components"), (missing, "missing components"), (swapped, "does not match the manifest hash")):
            with self.subTest(case=change.__name__):
                staged = self.staged(components_dir)
                change(staged)
                self.assert_refused(self.publish(self.staged(dist_dir), staged), message)


class PushPhaseTests(Harness):
    def probe(self):
        return self.fresh_site()

    def test_push_commits_exactly_the_five_files_and_records_tooling_sha(self):
        result = self.push()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.calls(), [])
        probe = self.probe()
        changed = git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", cwd=probe).splitlines()
        self.assertEqual(sorted(changed), sorted(FIVE))
        self.assertEqual(git("log", "-1", "--format=%s", cwd=probe), f"chore: publish rustup dist {VERSION} {DATE}")
        modes = {line.split()[0] for line in git("ls-tree", "-r", "HEAD", "--", *FIVE, cwd=probe).splitlines()}
        self.assertEqual(modes, {"100644"})
        provenance = json.loads((probe / PROVENANCE).read_text())
        self.assertEqual(provenance["tooling_sha"], self.tooling_sha)
        self.assertEqual(provenance["release_tag"], RELEASE)

    def test_push_succeeds_without_a_components_directory(self):
        dist_dir, components_dir = self.make_artifacts()
        digests = self.digests_for(dist_dir, components_dir)
        shutil.rmtree(components_dir)
        result = self.push(self.staged(dist_dir), digests, COMPONENTS_DIR=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.probe() / PUBLISHED).exists())

    def test_push_race_is_reset_rechecked_and_retried(self):
        site = self.fresh_site()
        race = self.race_on_first_push(site, {"unrelated": "y\n"})
        result = self.push(YURT_SITE_DIR=str(site))
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = self.probe()
        self.assertTrue((probe / "unrelated").exists())
        self.assertTrue((probe / PUBLISHED).exists())
        self.assertEqual(git("rev-parse", "HEAD^", cwd=probe), race)
        self.assertNotIn("rebase", result.stderr)

    def assert_landing_before_push_fails_closed(self, path, message):
        site = self.fresh_site()  # the push job's checkout predates the landing
        head = self.land({path: "another publication\n"})
        self.assert_refused(self.push(YURT_SITE_DIR=str(site)), message, head)
        self.assertFalse((self.probe() / MOVING).exists())

    def test_same_date_publication_between_publish_and_push_fails_closed(self):
        self.assert_landing_before_push_fails_closed(f"dist/{DATE}/channel-rust-1.98.0.toml", "new date")

    def test_newer_publication_between_publish_and_push_fails_closed(self):
        self.assert_landing_before_push_fails_closed("dist/2026-10-05/channel-rust-1.99.1.toml", "later than")

    def test_newer_publication_during_push_fails_closed_on_the_retry(self):
        """A rebase would merge over the newer publication; the reset and date-rule recheck refuses it."""
        site = self.fresh_site()
        race = self.race_on_first_push(site, {"dist/2026-10-05/channel-rust-1.99.1.toml": "newer\n"})
        result = self.push(YURT_SITE_DIR=str(site))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("later than", result.stderr)
        self.assertEqual(self.remote_head(), race)
        self.assertFalse((self.probe() / MOVING).exists())

    def test_changed_tools_on_main_during_push_do_not_alter_the_running_copy(self):
        marker = self.root / "hijacked"
        evil = {
            "tools/publish_rustup_dist.sh": f"#!/usr/bin/env bash\ntouch {marker}\n",
            "tools/rustup_dist_stage.py": f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n",
        }
        before = {p.name: p.read_bytes() for p in self.trusted.iterdir()}
        site = self.fresh_site()
        self.race_on_first_push(site, evil)
        result = self.push(YURT_SITE_DIR=str(site))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((site / "tools" / "rustup_dist_stage.py").exists())  # the site really holds the changed copy
        self.assertFalse(marker.exists())
        self.assertEqual({p.name: p.read_bytes() for p in self.trusted.iterdir()}, before)
        self.assertTrue((self.probe() / PUBLISHED).exists())

    def test_changed_tools_on_main_before_push_are_never_run(self):
        marker = self.root / "hijacked"
        self.land({
            "tools/publish_rustup_dist.sh": f"#!/usr/bin/env bash\ntouch {marker}\n",
            "tools/rustup_dist_stage.py": f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n",
        }, "change the tools on main")
        result = self.push()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertTrue((self.probe() / PUBLISHED).exists())

    def test_push_never_runs_hooks_of_the_site_checkout(self):
        """The push carries the PAT in its environment: no hook of the checkout may run around it."""
        marker = self.root / "hooked"
        site = self.fresh_site()
        for name in ("pre-push", "pre-commit", "commit-msg", "post-commit", "post-checkout", "reference-transaction"):
            hook = site / ".git" / "hooks" / name
            hook.write_text(f'#!/bin/sh\necho "{name} ${{GIT_CONFIG_VALUE_0:-}}" >> {marker}\nexit 0\n')
            hook.chmod(0o755)
        result = self.push(YURT_SITE_DIR=str(site))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
        self.assertTrue((self.probe() / PUBLISHED).exists())

    def test_push_refuses_a_site_whose_origin_is_not_yurt_packages(self):
        for origin in ("https://github.com/evil/yurt-packages", "/elsewhere/yurt-packages.git"):
            with self.subTest(origin=origin):
                site = self.fresh_site(origin=origin)
                self.assert_refused(self.push(YURT_SITE_DIR=str(site)), "origin")

    def test_push_refuses_a_rerun_attempt(self):
        for attempt in ("2", "", None):
            with self.subTest(attempt=attempt):
                self.assert_refused(self.push(GITHUB_RUN_ATTEMPT=attempt), "GITHUB_RUN_ATTEMPT")

    def test_push_refuses_a_missing_digest(self):
        for digests in ("", None):
            with self.subTest(digests=digests):
                dist_dir, components_dir = self.make_artifacts()
                self.assert_refused(self.push(dist_dir, digests), "DIGESTS")

    def test_push_refuses_a_mismatched_digest(self):
        dist_dir, components_dir = self.make_artifacts()
        good = json.loads(self.digests_for(dist_dir, components_dir))
        member = dict(good, members=dict(good["members"], **{MOVING: "0" * 64}))
        whole = dict(good, **{"dist.tar": "0" * 64})
        no_members = {"dist.tar": good["dist.tar"]}
        cases = {
            "member": (json.dumps(member), f"digest mismatch for {MOVING}"),
            "dist.tar": (json.dumps(whole), "digest mismatch for dist.tar"),
            "no members": (json.dumps(no_members), "missing digest for members"),
            "not json": ("{", "DIGESTS is not valid"),
        }
        for name, (digests, message) in cases.items():
            with self.subTest(case=name):
                self.assert_refused(self.push(self.staged(dist_dir), digests), message)
        with self.subTest(case="re-packed dist.tar, same members"):
            staged = self.staged(dist_dir)

            def later(info):
                info.mtime = 12345

            pack(staged, staged / "dist.tar", [(n, d, later) for n, d in tar_members(dist_dir / "dist.tar").items()])
            self.assert_refused(self.push(staged, json.dumps(good)), "digest mismatch for dist.tar")

    def test_push_refuses_a_hostile_dist_tar_even_with_matching_names(self):
        dist_dir, components_dir = self.make_artifacts()
        digests = self.digests_for(dist_dir, components_dir)
        staged = self.staged(dist_dir)
        pack(staged, staged / "dist.tar", [*tar_members(dist_dir / "dist.tar").items(), ("dist/../evil", b"x")])
        self.assert_refused(self.push(staged, digests), "..")

    def test_push_refuses_when_the_staged_change_is_not_exactly_the_five_paths(self):
        head = self.land({".gitignore": "*.sha256\n"}, "ignore sidecars")
        result = self.push()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("five", result.stderr)
        self.assert_nothing_pushed(head)
        self.assertEqual(git("status", "--porcelain", cwd=result.env["YURT_SITE_DIR"]), "")

    def test_failed_run_leaves_no_modified_tracked_file_in_a_reused_checkout(self):
        old = {MOVING: "old\n", f"dist/2026-01-01/channel-rust-{VERSION}.toml": "old\n"}
        head = self.land(old, "earlier moving manifest")
        site = self.fresh_site()
        (self.root / "reject").touch()
        result = self.push(YURT_SITE_DIR=str(site))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("after 3 attempts", result.stderr)
        self.assertEqual(self.remote_head(), head)
        self.assertEqual(git("status", "--porcelain", "--ignored", cwd=site), "")
        self.assertEqual((site / MOVING).read_text(), "old\n")
        self.assertEqual(git("rev-parse", "HEAD", cwd=site), head)

    def test_token_is_never_stored_in_the_site_checkout_and_authenticates_only_the_push(self):
        site = self.fresh_site()
        result = self.push(YURT_SITE_DIR=str(site), GH_TOKEN=READ, GITHUB_TOKEN=GITHUB)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.calls(), [])
        needles = [s.encode() for s in SECRETS] + [AUTH_HEADER.split()[-1].encode()]
        stored = [f for f in (site / ".git").rglob("*") if f.is_file() and any(n in f.read_bytes() for n in needles)]
        self.assertEqual(stored, [])
        # GIT_CONFIG_COUNT is empty here because git strips it (local_repo_env) before running the
        # local receive-pack hook, not because it was unset. This proves the key and value reach
        # git, not that the header authenticates an https push (hosted-only).
        self.assertEqual(self.header_pushes(), [f"|http.https://github.com/.extraheader|{AUTH_HEADER}"])
        self.assertNotIn(PUSH, result.stderr + result.stdout)

    def test_no_read_token_reaches_gh_in_publish_or_push(self):
        dist_dir, components_dir = self.make_artifacts()
        published = self.publish(self.staged(dist_dir), self.staged(components_dir), GITHUB_TOKEN=READ, YURT_RUST_READ_TOKEN=READ)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(published.tokens(), {f"{GITHUB}||"})
        pushed = self.push(self.staged(dist_dir), published.outputs()["digests"], GH_TOKEN=READ, YURT_RUST_READ_TOKEN=READ)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        self.assertEqual(pushed.calls(), [])
        # Empty GIT_CONFIG_COUNT: git strips it on local transports (see the test above); the
        # https authentication itself is hosted-only.
        self.assertEqual(self.header_pushes(), [f"|http.https://github.com/.extraheader|{AUTH_HEADER}"])


class SiteCheckoutTests(Harness):
    def test_a_site_dir_inside_another_checkout_is_refused_and_never_reset(self):
        """Every phase that resets or cleans YURT_SITE_DIR refuses a directory that is not a checkout's top."""
        inner = self.other / "sub"
        inner.mkdir()
        (inner / "keep").write_text("untracked\n")
        (self.other / "README").write_text("modified, not committed\n")
        for name, run in (("emit", self.emit), ("publish", self.publish), ("push", self.push)):
            with self.subTest(phase=name):
                result = run(YURT_SITE_DIR=str(inner))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("YURT_SITE_DIR", result.stderr)
                self.assertEqual(result.calls(), [])
                self.assertEqual((self.other / "README").read_text(), "modified, not committed\n")
                self.assertTrue((inner / "keep").exists())
        self.assert_no_release_mutation()
        self.assert_nothing_pushed()


class SequenceTests(Harness):
    """The five jobs in order, each gated on the previous ones, as the workflow's needs lists do."""

    def run_check(self, dist_dir: Path, plan: dict, **env):
        """As the check job: extract through the trusted validator, then run the tooling checker."""
        out = self.unique("checked")
        subprocess.run(["python3", "-I", str(self.trusted / "rustup_dist_stage.py"), "extract", "--dir", str(self.staged(dist_dir)),
                        "--out", str(out)], env=dict(self.base_env(), **plan), check=True)
        command = ["python3", str(self.tooling / "scripts" / "verify_rustup_dist_urls.py"), "--dist", str(out / "dist"),
                   "--channel", plan["PLAN_VERSION"], "--date", plan["PLAN_DATE"]]
        return subprocess.run(command, env=dict(self.base_env(), **env), capture_output=True, text=True)

    def run_sequence(self, emit_env=None, publish_env=None, check_env=None):
        ran = {"plan": self.run_phase("plan", self.plan_inputs())}
        if ran["plan"].returncode:
            return ran
        plan = {f"PLAN_{key.upper()}": value for key, value in ran["plan"].outputs().items()}
        ran["emit"] = self.emit(plan, **(emit_env or {}))
        if ran["emit"].returncode:
            return ran
        dist, components = ran["emit"].out / "dist", ran["emit"].out / "components"
        ran["publish"] = self.publish(self.staged(dist), self.staged(components), plan=plan, **(publish_env or {}))
        if ran["publish"].returncode:
            return ran
        ran["check"] = self.run_check(dist, plan, **(check_env or {}))
        if ran["check"].returncode:
            return ran
        ran["push"] = self.push(self.staged(dist), ran["publish"].outputs()["digests"], plan=plan)
        return ran

    def test_publishes_and_records_tooling_sha(self):
        ran = self.run_sequence()
        self.assertEqual(list(ran), ["plan", "emit", "publish", "check", "push"])
        self.assertEqual(ran["push"].returncode, 0, ran["push"].stderr)
        self.assertEqual(ran["plan"].verbs(), ["api repos/YurtOS/yurt-rust/commits/main"])
        self.assertEqual(ran["emit"].verbs(), ["release download"])
        self.assertIn(f"release download {RELEASE} -R YurtOS/yurt-rust", ran["emit"].calls()[0])
        self.assertEqual(ran["publish"].verbs(), ["release view", "release create", "release upload"])
        self.assertIn(self.tooling_sha, ran["publish"].calls()[1])
        self.assertIn("--latest=false", ran["publish"].calls()[1])
        emitted = (self.logs / "emit.log").read_text()
        self.assertIn(f"--base-url {PREFIX}", emitted)
        self.assertIn(f"--producer-host {PRODUCER}", emitted)
        self.assertIn(f"--channel {VERSION} --date {DATE}", (self.logs / "check.log").read_text())
        self.assertEqual((self.logs / "check.files").read_text().splitlines(), sorted(FIVE))
        probe = self.fresh_site()
        provenance = json.loads((probe / PROVENANCE).read_text())
        self.assertEqual(provenance["tooling_sha"], self.tooling_sha)
        self.assertTrue((probe / MOVING).exists())
        self.assertEqual(sorted(os.listdir(self.state / f"{TAG}.assets")), COMPONENTS)

    def test_token_reaches_gh_only_not_tooling(self):
        ran = self.run_sequence()
        self.assertEqual(list(ran), ["plan", "emit", "publish", "check", "push"])
        self.assertEqual(ran["push"].returncode, 0, ran["push"].stderr)
        self.assertEqual(ran["plan"].tokens(), {f"{READ}||"})
        self.assertEqual(ran["emit"].tokens(), {f"{READ}||"})
        self.assertEqual(ran["publish"].tokens(), {f"{GITHUB}||"})
        self.assertEqual(ran["push"].calls(), [])
        self.assert_no_secret_in(self.logs / "emit.env")
        self.assertEqual((self.logs / "emit.leak").read_text(), "")

    def test_checker_failure_commits_no_manifest(self):
        ran = self.run_sequence(check_env={"FAKE_CHECKER_FAIL": "1"})
        self.assertEqual(list(ran), ["plan", "emit", "publish", "check"])
        self.assertIn("release upload", ran["publish"].verbs())
        self.assertTrue((self.logs / "check.log").exists())
        self.assert_nothing_pushed()

    def test_upload_failure_commits_no_manifest_and_skips_checker(self):
        ran = self.run_sequence(publish_env={"FAKE_UPLOAD_FAIL": "1"})
        self.assertEqual(list(ran), ["plan", "emit", "publish"])
        self.assertNotEqual(ran["publish"].returncode, 0)
        self.assertEqual(ran["publish"].verbs()[-1:], ["release upload"])
        self.assertFalse((self.logs / "check.log").exists())
        self.assert_nothing_pushed()


if __name__ == "__main__":
    unittest.main()
