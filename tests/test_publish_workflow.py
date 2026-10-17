"""Static checks of .github/workflows/publish-rustup-dist.yml (the five-job publisher).

The workflow is parsed with PyYAML. A missing PyYAML is a failure, not a skip: CI installs
pyyaml==6.0.2. PyYAML reads the `on:` key as the boolean True (YAML 1.1), so triggers are
looked up under True.

These checks cover what can be proven without a hosted run: the job chain, which job sees
which secret, permissions, checkout and artifact wiring, and that no expression reaches a
shell through `run:`. What only a hosted run proves is listed in the README.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

try:
    import yaml
except ImportError as error:  # fail loudly: the static checks must never be skipped
    raise ImportError("PyYAML is required for the workflow tests: pip install pyyaml==6.0.2") from error

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "publish-rustup-dist.yml"

JOBS = ["plan", "emit", "publish", "check", "push"]
NEEDS = {
    "plan": [],
    "emit": ["plan"],
    "publish": ["plan", "emit"],
    "check": ["plan", "emit", "publish"],
    "push": ["plan", "emit", "publish", "check"],
}
PERMISSIONS = {
    "plan": {},
    "emit": {"contents": "read"},
    "publish": {"contents": "write"},
    "check": {},
    "push": {"contents": "read"},
}
SECRETS = {
    "plan": {"YURT_RUST_READ_TOKEN"},
    "emit": {"YURT_RUST_READ_TOKEN"},
    "publish": {"GITHUB_TOKEN"},
    "check": {"YURT_RUST_READ_TOKEN"},
    "push": {"RUSTUP_DIST_PUBLISH_TOKEN"},
}
PLAN_OUTPUTS = {"release_tag", "version", "revision", "date", "producer_host", "tooling_sha", "tag"}
EMIT_OUTPUTS = {"dist_artifact_id", "components_artifact_id"}
# Which downloads each job makes: emit output -> artifact.
DOWNLOADS = {
    "publish": {"dist_artifact_id", "components_artifact_id"},
    "check": {"dist_artifact_id"},
    "push": {"dist_artifact_id"},
}
MAIN_GUARD = "github.ref == 'refs/heads/main'"
SHA_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
SCRIPT = '"$GITHUB_WORKSPACE/trusted/tools/publish_rustup_dist.sh"'


def load() -> dict:
    with WORKFLOW.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def as_list(value) -> list:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


# GitHub expressions: `${{ ... }}` (may span lines), and every `if:` value, which is an expression
# even without the braces. Context names are case-insensitive there, so the scan is too.
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
CONTEXT = re.compile(r"(?<![\w.$-])(secrets|github)\b", re.I)
MEMBER = re.compile(r"\s*\.\s*([A-Za-z_][A-Za-z0-9_-]*)")


def expressions(text: str, is_if: bool = False) -> list[str]:
    found = EXPRESSION.findall(text)
    if is_if and "${{" not in text:
        found.append(text)
    return found


def context_refs(expression: str) -> tuple[list[str], list[str]]:
    """The secret names an expression reads, and any reference that is not a plain `secrets.NAME`
    (bare `secrets`, `secrets[...]`, `secrets.*`, `toJSON(secrets)`) or that reads the job token
    (`github.token`, `github[...]`, bare `github`)."""
    names, bad = [], []
    for match in CONTEXT.finditer(expression):
        word = match.group(1)
        member = MEMBER.match(expression, match.end())
        if member is None:
            bad.append(f"{word} used other than as {word}.NAME")
        elif word.lower() == "secrets":
            names.append(member.group(1))
        elif member.group(1).lower() == "token":
            bad.append("github.token")
    return names, bad


def walk_strings(node, key=None):
    """(key, string) for every string in a parsed YAML node; keys are strings too."""
    if isinstance(node, dict):
        for child_key, value in node.items():
            if isinstance(child_key, str):
                yield None, child_key
            yield from walk_strings(value, child_key)
    elif isinstance(node, list):
        for value in node:
            yield from walk_strings(value, key)
    elif isinstance(node, str):
        yield key, node


def refs_in(node) -> tuple[list[str], list[str]]:
    names, bad = [], []
    for key, value in walk_strings(node):
        for expression in expressions(value, is_if=key == "if"):
            found, wrong = context_refs(expression)
            names += found
            bad += wrong
    return names, bad


def secrets_in(node) -> set[str]:
    return set(refs_in(node)[0])


def strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def secret_violations(text: str) -> list[str]:
    """Every way the workflow text could hand a job a secret (or the job token) it must not see.

    Both the raw text without comments and the parsed structure are scanned: the parsed scan sees
    through anchors, merge keys, escapes and block scalars; the raw scan sees what the parse would
    fold away."""
    allowed = set().union(*SECRETS.values())
    bad = []
    raw_names, raw_bad = [], []
    for expression in expressions(strip_comments(text)):
        found, wrong = context_refs(expression)
        raw_names += found
        raw_bad += wrong
    bad += [f"raw: {item}" for item in raw_bad]
    bad += [f"raw: secret {name} not allowed anywhere" for name in sorted(set(raw_names) - allowed)]
    if raw_names.count("GITHUB_TOKEN") != 1:
        bad.append("raw: secrets.GITHUB_TOKEN must appear exactly once")

    wf = yaml.safe_load(text)
    jobs = wf.get("jobs") or {}
    outside_names, outside_bad = refs_in({key: value for key, value in wf.items() if key != "jobs"})
    if outside_names or outside_bad:
        bad.append("a secret or token reference outside jobs")
    parsed_names = list(outside_names)
    for name, job in jobs.items():
        names, wrong = refs_in(job)
        parsed_names += names
        bad += [f"{name}: {item}" for item in wrong]
        extra = set(names) - SECRETS.get(name, set())
        if extra:
            bad.append(f"{name}: secrets {sorted(extra)} not allowed")
    # The job token is allowed in one place: the publish script step's GH_TOKEN.
    if parsed_names.count("GITHUB_TOKEN") != 1:
        bad.append("secrets.GITHUB_TOKEN must be referenced exactly once")
    holders = [
        step
        for step in (jobs.get("publish") or {}).get("steps") or []
        if (step.get("env") or {}).get("GH_TOKEN") == "${{ secrets.GITHUB_TOKEN }}" and SCRIPT in step.get("run", "")
    ]
    if len(holders) != 1:
        bad.append("secrets.GITHUB_TOKEN is not the publish script step's GH_TOKEN")
    if "PACKAGE_PUBLISH_TOKEN" in text:
        bad.append("PACKAGE_PUBLISH_TOKEN is referenced")
    return bad


def uses_name(step: dict) -> str:
    return step.get("uses", "").split("@", 1)[0]


class WorkflowStructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = load()
        self.jobs = self.wf.get("jobs") or {}

    def job(self, name: str) -> dict:
        self.assertIn(name, self.jobs, f"job {name} is missing")
        return self.jobs[name]

    def steps(self, name: str) -> list[dict]:
        return self.job(name).get("steps") or []

    def all_steps(self):
        for name, job in self.jobs.items():
            for step in job.get("steps") or []:
                yield name, step

    def test_on_key_is_parsed_as_true_and_only_dispatch_triggers(self) -> None:
        self.assertNotIn("on", self.wf)
        self.assertIn(True, self.wf)
        self.assertEqual(set(self.wf[True]), {"workflow_dispatch"})

    def test_workflow_permissions_are_empty(self) -> None:
        self.assertEqual(self.wf.get("permissions"), {})

    def test_concurrency_group_is_shared_with_publish_package(self) -> None:
        # publish's release delete is safe only because this group serializes whole runs.
        self.assertEqual(self.wf.get("concurrency"), {"group": "publish-package", "cancel-in-progress": False})

    def test_exactly_the_five_jobs_with_the_exact_needs(self) -> None:
        self.assertEqual(sorted(self.jobs), sorted(JOBS))
        for name in JOBS:
            with self.subTest(job=name):
                self.assertEqual(as_list(self.job(name).get("needs")), NEEDS[name])

    def test_every_job_runs_only_on_main(self) -> None:
        for name in JOBS:
            with self.subTest(job=name):
                self.assertEqual(self.job(name).get("if"), MAIN_GUARD)

    def test_per_job_permissions_are_exact(self) -> None:
        for name in JOBS:
            with self.subTest(job=name):
                self.assertIn("permissions", self.job(name))
                self.assertEqual(self.job(name)["permissions"], PERMISSIONS[name])

    def test_secrets_per_job_are_exact(self) -> None:
        for name in JOBS:
            with self.subTest(job=name):
                self.assertEqual(secrets_in(self.job(name)), SECRETS[name])
        outside = {key: value for key, value in self.wf.items() if key != "jobs"}
        self.assertEqual(secrets_in(outside), set())
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("PACKAGE_PUBLISH_TOKEN", text)
        self.assertEqual(secret_violations(text), [])

    def test_secrets_are_step_scoped_never_job_env(self) -> None:
        for name in JOBS:
            with self.subTest(job=name):
                self.assertEqual(secrets_in(self.job(name).get("env") or {}), set())

    def test_push_token_reaches_only_the_push_script_step(self) -> None:
        holders = [
            (name, step) for name, step in self.all_steps() if "RUSTUP_DIST_PUBLISH_TOKEN" in secrets_in(step)
        ]
        self.assertEqual(len(holders), 1)
        name, step = holders[0]
        self.assertEqual(name, "push")
        self.assertEqual(step.get("env", {}).get("PUSH_TOKEN"), "${{ secrets.RUSTUP_DIST_PUBLISH_TOKEN }}")
        self.assertEqual(secrets_in({k: v for k, v in step.items() if k != "env"}), set())
        self.assertEqual(step["run"].split(), [SCRIPT, "push"])

    def test_push_alone_uses_the_rustup_publish_environment(self) -> None:
        for name in JOBS:
            with self.subTest(job=name):
                environment = self.job(name).get("environment")
                if isinstance(environment, dict):
                    environment = environment.get("name")
                self.assertEqual(environment, "rustup-publish" if name == "push" else None)
                if name != "push":
                    self.assertNotIn("rustup-publish", json.dumps(self.job(name)))

    def test_every_action_is_pinned_by_sha_with_the_tag_in_a_comment(self) -> None:
        steps = list(self.all_steps())
        self.assertTrue(steps)
        for name, step in steps:
            if "uses" in step:
                with self.subTest(job=name, uses=step["uses"]):
                    self.assertRegex(step["uses"], SHA_PIN)
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*(-\s+)?uses:", line):
                with self.subTest(line=line.strip()):
                    self.assertRegex(line, r"uses: \S+@[0-9a-f]{40} # v\d+(\.\d+)*$")

    def test_every_checkout_persists_no_credentials(self) -> None:
        checkouts = [(n, s) for n, s in self.all_steps() if uses_name(s) == "actions/checkout"]
        self.assertTrue(checkouts)
        for name, step in checkouts:
            with self.subTest(job=name, path=step.get("with", {}).get("path")):
                self.assertIs(step.get("with", {}).get("persist-credentials"), False)

    def checkouts(self, name: str) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for step in self.steps(name):
            if uses_name(step) == "actions/checkout":
                options = step.get("with") or {}
                path = options.get("path")
                self.assertIsNotNone(path, f"{name}: a checkout without a path")
                self.assertNotIn(path, found, f"{name}: two checkouts into {path}")
                found[path] = options
        return found

    def test_checkout_layout_per_job(self) -> None:
        trusted = {"ref": "${{ github.sha }}", "path": "trusted", "persist-credentials": False}
        site = {"ref": "main", "path": "site", "persist-credentials": False}
        tooling = {
            "repository": "YurtOS/yurt-rust",
            "ref": "${{ needs.plan.outputs.tooling_sha }}",
            "path": "tooling",
            "token": "${{ secrets.YURT_RUST_READ_TOKEN }}",
            "persist-credentials": False,
        }
        expected = {
            "plan": {"trusted": trusted},
            "emit": {"trusted": trusted, "site": site, "tooling": tooling},
            "publish": {"trusted": trusted, "site": site},
            "check": {"tooling": tooling},
            "push": {"trusted": trusted, "site": site},
        }
        for name in JOBS:
            with self.subTest(job=name):
                self.assertEqual(self.checkouts(name), expected[name])

    def test_checkout_paths_are_plain_names_under_the_workspace(self) -> None:
        # trusted/ and site/ (and tooling/) are siblings: no nesting, no absolute or .. paths.
        for name, step in self.all_steps():
            if uses_name(step) == "actions/checkout":
                path = step["with"]["path"]
                with self.subTest(job=name, path=path):
                    self.assertRegex(path, r"^[a-z]+$")  # a plain name under the workspace

    def test_no_expression_inside_any_run(self) -> None:
        runs = [(n, s) for n, s in self.all_steps() if "run" in s]
        self.assertTrue(runs)
        for name, step in runs:
            with self.subTest(job=name, step=step.get("name")):
                self.assertNotIn("${{", step["run"])

    def test_no_script_runs_from_the_site_or_tooling_checkout_in_privileged_jobs(self) -> None:
        for name, step in self.all_steps():
            if "run" in step:
                with self.subTest(job=name, step=step.get("name")):
                    self.assertNotIn("site/tools", step["run"])
        for name in ("publish", "push"):
            for step in self.steps(name):
                if "run" in step:
                    with self.subTest(job=name, step=step.get("name")):
                        self.assertNotIn("tooling", step["run"])

    def test_phases_run_the_trusted_script(self) -> None:
        for name in ("plan", "emit", "publish", "push"):
            with self.subTest(job=name):
                runs = [s["run"] for s in self.steps(name) if SCRIPT in s.get("run", "")]
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0].split()[-1], name)
        self.assertFalse([s for s in self.steps("check") if SCRIPT in s.get("run", "")])

    def test_check_runs_the_tooling_checker_on_the_planned_values(self) -> None:
        runs = [s for s in self.steps("check") if "verify_rustup_dist_urls.py" in s.get("run", "")]
        self.assertEqual(len(runs), 1)
        run = runs[0]["run"]
        self.assertNotRegex(run, r"python3?\s+-I")
        for flag in ('--channel "$PLAN_VERSION"', '--date "$PLAN_DATE"', "--dist "):
            self.assertIn(flag, run)

    def test_no_cache_in_privileged_jobs(self) -> None:
        for name in ("publish", "push"):
            for step in self.steps(name):
                with self.subTest(job=name, step=step.get("name") or step.get("uses")):
                    self.assertNotIn("cache", uses_name(step))
                    self.assertNotIn("cache", step.get("with") or {})

    def test_job_outputs(self) -> None:
        plan = self.job("plan").get("outputs") or {}
        self.assertEqual(set(plan), PLAN_OUTPUTS)
        for key, value in plan.items():
            self.assertEqual(value, f"${{{{ steps.plan.outputs.{key} }}}}")
        self.assertEqual(set(self.job("emit").get("outputs") or {}), EMIT_OUTPUTS)
        self.assertEqual(self.job("publish").get("outputs"), {"digests": "${{ steps.publish.outputs.digests }}"})
        self.assertFalse(self.job("check").get("outputs"))
        self.assertFalse(self.job("push").get("outputs"))

    def test_identity_comes_only_from_plan_and_data_by_trust_level(self) -> None:
        text = json.dumps(self.jobs)
        refs = set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", text))
        self.assertTrue(refs)
        for job, key in refs:
            with self.subTest(ref=f"{job}.{key}"):
                allowed = {"plan": PLAN_OUTPUTS, "emit": EMIT_OUTPUTS, "publish": {"digests"}}.get(job, set())
                self.assertIn(key, allowed)
        self.assertNotIn("needs.publish", json.dumps(self.job("check")))
        self.assertIn("needs.publish.outputs.digests", json.dumps(self.job("push")))
        for name in ("emit", "publish", "check", "push"):
            env = self.job(name).get("env") or {}
            with self.subTest(job=name):
                for key in PLAN_OUTPUTS:
                    self.assertEqual(env.get(f"PLAN_{key.upper()}"), f"${{{{ needs.plan.outputs.{key} }}}}")

    def test_uploads(self) -> None:
        uploads = {
            s["with"]["name"]: s
            for s in self.steps("emit")
            if uses_name(s) == "actions/upload-artifact" and "with" in s
        }
        self.assertEqual(set(uploads), {"rustup-dist", "rustup-components"})
        for name, step in uploads.items():
            with self.subTest(artifact=name):
                options = step["with"]
                self.assertEqual(options.get("retention-days"), 1)
                self.assertEqual(options.get("if-no-files-found"), "error")
                self.assertTrue(options["path"].startswith("${{ runner.temp }}/"))
        self.assertEqual(uploads["rustup-dist"]["with"]["path"], "${{ runner.temp }}/rustup-out/dist/dist.tar")
        self.assertEqual(uploads["rustup-components"]["with"]["path"], "${{ runner.temp }}/rustup-out/components/")
        self.assertEqual(uploads["rustup-components"]["with"].get("compression-level"), 0)
        outputs = self.job("emit").get("outputs") or {}
        self.assertEqual(
            outputs.get("dist_artifact_id"), f"${{{{ steps.{uploads['rustup-dist'].get('id')}.outputs.artifact-id }}}}"
        )
        self.assertEqual(
            outputs.get("components_artifact_id"),
            f"${{{{ steps.{uploads['rustup-components'].get('id')}.outputs.artifact-id }}}}",
        )
        for name in ("plan", "publish", "check", "push"):
            with self.subTest(job=name):
                self.assertFalse([s for s in self.steps(name) if uses_name(s) == "actions/upload-artifact"])

    def test_downloads_by_validated_id_into_their_own_directories(self) -> None:
        for name in JOBS:
            steps = self.steps(name)
            downloads = [i for i, s in enumerate(steps) if uses_name(s) == "actions/download-artifact"]
            with self.subTest(job=name):
                self.assertEqual(len(downloads), len(DOWNLOADS.get(name, ())))
            seen_ids, seen_paths = set(), set()
            for index in downloads:
                options = steps[index].get("with") or {}
                with self.subTest(job=name, step=index):
                    self.assertEqual(set(options), {"artifact-ids", "path"})
                    path = options["path"]
                    self.assertRegex(path, r"^\$\{\{ runner\.temp \}\}/[a-z-]+$")
                    self.assertNotIn(path, seen_paths)
                    seen_paths.add(path)
                    # The id comes from an earlier step of this job that checked it is all digits.
                    match = re.fullmatch(r"\$\{\{ steps\.([a-z0-9_-]+)\.outputs\.([a-z_]+) \}\}", options["artifact-ids"])
                    self.assertIsNotNone(match, options["artifact-ids"])
                    check_id, key = match.groups()
                    checks = [s for s in steps[:index] if s.get("id") == check_id]
                    self.assertEqual(len(checks), 1)
                    check = checks[0]
                    self.assertIn(key, EMIT_OUTPUTS)
                    self.assertIn(f"${{{{ needs.emit.outputs.{key} }}}}", (check.get("env") or {}).values())
                    self.assertIn("^[0-9]+$", check["run"])
                    seen_ids.add(key)
            with self.subTest(job=name):
                self.assertEqual(seen_ids, DOWNLOADS.get(name, set()))

    def test_downloads_stay_outside_the_checkouts(self) -> None:
        for name in JOBS:
            for step in self.steps(name):
                if uses_name(step) == "actions/download-artifact":
                    path = (step.get("with") or {}).get("path", "")
                    with self.subTest(job=name, path=path):
                        self.assertNotIn("github.workspace", path)
                        self.assertNotRegex(path, r"(trusted|site|tooling)")

    def script_step(self, name: str) -> dict:
        found = [s for s in self.steps(name) if SCRIPT in s.get("run", "")]
        self.assertEqual(len(found), 1, f"{name}: expected one step running the trusted script")
        return found[0]

    def test_privileged_phases_get_artifact_dirs_and_plan_values(self) -> None:
        def script_env(name: str) -> dict:
            return self.script_step(name).get("env") or {}

        publish = script_env("publish")
        self.assertEqual(publish.get("GH_TOKEN"), "${{ secrets.GITHUB_TOKEN }}")
        self.assertEqual(publish.get("YURT_SITE_DIR"), "${{ github.workspace }}/site")
        push = script_env("push")
        self.assertEqual(push.get("DIGESTS"), "${{ needs.publish.outputs.digests }}")
        self.assertEqual(push.get("YURT_SITE_DIR"), "${{ github.workspace }}/site")
        self.assertNotIn("GH_TOKEN", push)
        self.assertNotIn("COMPONENTS_DIR", push)
        downloaded = {
            name: {(s.get("with") or {}).get("path") for s in self.steps(name) if uses_name(s) == "actions/download-artifact"}
            for name in ("publish", "push")
        }
        self.assertEqual({publish.get("DIST_DIR"), publish.get("COMPONENTS_DIR")}, downloaded["publish"])
        self.assertEqual({push.get("DIST_DIR")}, downloaded["push"])
        emit = script_env("emit")
        self.assertEqual(emit.get("GH_TOKEN"), "${{ secrets.YURT_RUST_READ_TOKEN }}")
        self.assertEqual(emit.get("YURT_RUST_TOOLING_DIR"), "${{ github.workspace }}/tooling")
        self.assertEqual(emit.get("YURT_SITE_DIR"), "${{ github.workspace }}/site")
        self.assertEqual(emit.get("OUT_DIR"), "${{ runner.temp }}/rustup-out")
        plan = script_env("plan")
        self.assertEqual(plan.get("GH_TOKEN"), "${{ secrets.YURT_RUST_READ_TOKEN }}")
        self.assertEqual(
            {k: plan.get(k) for k in ("RUST_RELEASE", "PUBLISH_DATE", "PRODUCER_HOST", "TOOLING_REF")},
            {
                "RUST_RELEASE": "${{ inputs.rust_release }}",
                "PUBLISH_DATE": "${{ inputs.date }}",
                "PRODUCER_HOST": "${{ inputs.producer_host }}",
                "TOOLING_REF": "${{ inputs.tooling_ref }}",
            },
        )
        plan_step = self.script_step("plan")
        self.assertEqual(plan_step.get("id"), "plan")
        self.assertIn('OUTPUT_FILE="$GITHUB_OUTPUT"', plan_step["run"])
        publish_step = self.script_step("publish")
        self.assertEqual(publish_step.get("id"), "publish")
        self.assertIn('OUTPUT_FILE="$GITHUB_OUTPUT"', publish_step["run"])

    def test_inputs_reach_only_the_plan_job(self) -> None:
        for name in JOBS:
            with self.subTest(job=name):
                found = re.findall(r"inputs\.[a-z_]+", json.dumps(self.job(name)))
                if name == "plan":
                    self.assertTrue(found)
                else:
                    self.assertEqual(found, [])


EMIT_ENV = "          WORK_DIR: ${{ runner.temp }}/rustup-work\n"
PUSH_ENV = "          PUSH_TOKEN: ${{ secrets.RUSTUP_DIST_PUBLISH_TOKEN }}\n"
EMIT_RUN = '        run: |\n          "$GITHUB_WORKSPACE/trusted/tools/publish_rustup_dist.sh" emit\n'
PLAN_CHECKOUT = "          path: trusted\n          persist-credentials: false\n"
PUBLISH_SITE = "          ref: main\n          path: site\n          persist-credentials: false\n\n      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0\n        with:\n          python-version: \"3.13\"\n\n      - name: Check the artifact ids\n"


def add(anchor: str, lines: str) -> tuple[str, str]:
    return anchor, anchor + lines


# Each mutation is a list of (old, new) replacements of the first occurrence in the workflow text.
SECRET_MUTATIONS = {
    "toJSON(secrets) in emit": [add(EMIT_ENV, "          ALL: ${{ toJSON(secrets) }}\n")],
    "secrets['X'] in emit": [add(EMIT_ENV, "          ALL: ${{ secrets['YURT_RUST_READ_TOKEN'] }}\n")],
    'secrets["X"] in emit': [add(EMIT_ENV, "          ALL: '${{ secrets[\"RUSTUP_DIST_PUBLISH_TOKEN\"] }}'\n")],
    "join(secrets) in emit": [add(EMIT_ENV, "          ALL: ${{ join(secrets, ',') }}\n")],
    "secrets.* in emit": [add(EMIT_ENV, "          ALL: ${{ toJSON(secrets.*) }}\n")],
    "mixed-case Secrets.X in emit": [add(EMIT_ENV, "          ALL: ${{ Secrets.RUSTUP_DIST_PUBLISH_TOKEN }}\n")],
    "multi-line expression in emit": [
        add(EMIT_ENV, "          ALL: >-\n            ${{ toJSON(\n            secrets) }}\n")
    ],
    "escaped dollar in emit": [add(EMIT_ENV, '          ALL: "\\x24{{ toJSON(secrets) }}"\n')],
    "comment-looking line in a block scalar": [add(EMIT_ENV, "          ALL: |\n            # ${{ toJSON(secrets) }}\n")],
    "anchor and alias carry the read token to push": [
        (EMIT_ENV, EMIT_ENV + "          ALL: &tok ${{ secrets.YURT_RUST_READ_TOKEN }}\n"),
        add(PUSH_ENV, "          ALL: *tok\n"),
    ],
    "merge key carries emit's step env to push": [
        ("      - name: Emit the dist and the components\n        env:\n", "      - name: Emit the dist and the components\n        env: &emitenv\n"),
        (PUSH_ENV, PUSH_ENV + "          <<: *emitenv\n"),
    ],
    "step if on secrets": [(EMIT_RUN, "        if: contains(toJSON(secrets), 'x')\n" + EMIT_RUN)],
    "github.token in emit": [add(EMIT_ENV, "          T: ${{ github.token }}\n")],
    "GITHUB.TOKEN in emit": [add(EMIT_ENV, "          T: ${{ GITHUB.TOKEN }}\n")],
    "github['token'] in emit": [add(EMIT_ENV, "          T: ${{ github['token'] }}\n")],
    "toJSON(github) in emit": [add(EMIT_ENV, "          T: ${{ toJSON(github) }}\n")],
    "secrets.GITHUB_TOKEN in push": [add(PUSH_ENV, "          T: ${{ secrets.GITHUB_TOKEN }}\n")],
    "github.token on the plan checkout": [(PLAN_CHECKOUT, "          path: trusted\n          token: ${{ github.token }}\n          persist-credentials: false\n")],
    "secrets.GITHUB_TOKEN on a publish checkout": [
        (PUBLISH_SITE, PUBLISH_SITE.replace("          path: site\n", "          path: site\n          token: ${{ secrets.GITHUB_TOKEN }}\n", 1))
    ],
}


class SecretReferenceTest(unittest.TestCase):
    def test_the_workflow_has_no_secret_violation(self) -> None:
        self.assertEqual(secret_violations(WORKFLOW.read_text(encoding="utf-8")), [])

    def test_every_secret_leak_form_is_caught(self) -> None:
        source = WORKFLOW.read_text(encoding="utf-8")
        for label, replacements in SECRET_MUTATIONS.items():
            with self.subTest(mutation=label):
                text = source
                for old, new in replacements:
                    self.assertIn(old, text, "mutation anchor not found")
                    text = text.replace(old, new, 1)
                yaml.safe_load(text)  # still a valid YAML document
                self.assertNotEqual(secret_violations(text), [])


TOOLING_DIR = os.environ.get("YURT_RUST_TOOLING_DIR", "")


@unittest.skipUnless(
    TOOLING_DIR and (Path(TOOLING_DIR) / "scripts" / "emit_rustup_dist.py").is_file(),
    "set YURT_RUST_TOOLING_DIR to a yurt-rust checkout to run the tooling smoke test",
)
class ToolingSmokeTest(unittest.TestCase):
    """The real emitter and checker start without -I (they import their siblings) and accept
    the flags that tools/publish_rustup_dist.sh and the check job pass. CI cannot read the
    private yurt-rust, so this runs only locally; it is on the first-dispatch checklist."""

    def help_text(self, script: str) -> str:
        done = subprocess.run(
            [sys.executable, str(Path(TOOLING_DIR) / "scripts" / script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_emitter_accepts_the_flags_emit_passes(self) -> None:
        text = self.help_text("emit_rustup_dist.py")
        for flag in ("--release-dir", "--site", "--components", "--date", "--producer-host", "--base-url"):
            self.assertIn(flag, text)

    def test_checker_accepts_the_flags_check_passes(self) -> None:
        text = self.help_text("verify_rustup_dist_urls.py")
        for flag in ("--dist", "--channel", "--date"):
            self.assertIn(flag, text)


if __name__ == "__main__":
    unittest.main()
