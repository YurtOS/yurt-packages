"""Static checks of .github/workflows/release.yml, and of how publish-package.yml hands over to it.

Clients pin the signer `release.yml@refs/heads/main` (issuer GitHub Actions), so the file
name, the main-only gate and the permissions are part of the contract. What only a hosted
run proves (the Fulcio certificate's identity, the push) is listed in the README.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError as error:  # fail loudly: the static checks must never be skipped
    raise ImportError("PyYAML is required for the workflow tests: pip install pyyaml==6.0.2") from error

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
PUBLISH = WORKFLOWS / "publish-package.yml"
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.wf = load(RELEASE)
        self.job = self.wf["jobs"]["sign"]
        self.steps = self.job["steps"]

    def run_text(self) -> str:
        return "\n".join(step.get("run", "") for step in self.steps)

    def test_only_job_is_sign(self) -> None:
        self.assertEqual(list(self.wf["jobs"]), ["sign"])

    def test_triggers(self) -> None:
        on = self.wf[True]  # PyYAML reads `on:` as True
        self.assertEqual(
            on,
            {
                "workflow_dispatch": None,
                "workflow_run": {"workflows": ["Publish Package"], "types": ["completed"], "branches": ["main"]},
                # Weekly: the index expires 7 days after signing, and clients reject it 30 days later.
                "schedule": [{"cron": "17 5 * * 1"}],
            },
        )
        names = {load(path)["name"] for path in WORKFLOWS.glob("*.yml")}
        self.assertIn("Publish Package", names)

    def test_job_runs_only_on_main_of_this_repository(self) -> None:
        condition = " ".join(self.job["if"].split())
        for clause in [
            "github.repository == 'YurtOS/yurt-packages'",
            "github.ref == 'refs/heads/main'",
            "github.event.workflow_run.conclusion == 'success'",
            "github.event.workflow_run.head_branch == 'main'",
            "github.event.workflow_run.head_repository.full_name == 'YurtOS/yurt-packages'",
            "github.event_name == 'schedule'",
        ]:
            self.assertIn(clause, condition)
        checkout = self.steps[0]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertEqual(checkout["with"], {"ref": "main", "fetch-depth": 0})

    def test_minimum_permissions(self) -> None:
        self.assertEqual(self.wf["permissions"], {})
        self.assertEqual(self.job["permissions"], {"contents": "write", "id-token": "write"})

    def test_no_secrets(self) -> None:
        self.assertNotIn("secrets.", RELEASE.read_text())

    def test_actions_are_pinned_by_sha(self) -> None:
        uses = [step["uses"] for step in self.steps if "uses" in step]
        self.assertGreaterEqual(len(uses), 3)
        for value in uses:
            self.assertRegex(value, PINNED)

    def test_cosign_version_is_pinned(self) -> None:
        installer = next(step for step in self.steps if step.get("uses", "").startswith("sigstore/cosign-installer@"))
        self.assertRegex(installer["with"]["cosign-release"], r"^v\d+\.\d+\.\d+$")

    def test_own_concurrency_group(self) -> None:
        # Sharing publish-package's group would let a waiting Release replace a waiting publish.
        self.assertEqual(self.wf["concurrency"], {"group": "release", "cancel-in-progress": False})
        self.assertEqual(load(PUBLISH)["concurrency"]["group"], "publish-package")

    def test_push_refuses_a_main_that_moved_under_signed_paths(self) -> None:
        text = self.run_text()
        order = [
            'echo "START=$(git rev-parse HEAD)"',
            "git commit",
            "git fetch origin main",
            'git diff --name-only "$START" origin/main -- index.json \'*.bundle\' packages artifacts',
            "exit 1",
            "git rebase origin/main",
            "git push origin HEAD:main",
        ]
        tail = text[text.index("git commit") - 1 :]
        self.assertIn(order[0], text)
        positions = [tail.index(item) for item in order[1:]]
        self.assertEqual(positions, sorted(positions))

    def test_step_order(self) -> None:
        text = self.run_text()
        order = [
            "repo_signing.py plan --legacy tools/legacy_placeholder_bundles.txt",
            "repo_signing.py regenerate-index --legacy tools/legacy_placeholder_bundles.txt",
            'cosign sign-blob --yes --bundle "${archive}.bundle" "$archive"',
            "cosign sign-blob --yes --bundle index.json.bundle index.json",
            "repo_signing.py check --strict --cosign cosign",
            "repo_signing.py prune-legacy",
            "git push origin HEAD:main",
        ]
        positions = [text.index(item) for item in order]
        self.assertEqual(positions, sorted(positions))

    def test_plan_and_regenerate_verify_with_cosign(self) -> None:
        for command in ["repo_signing.py plan", "repo_signing.py regenerate-index"]:
            step = next(step for step in self.steps if command in step.get("run", ""))
            self.assertIn("--cosign cosign", step["run"])

    def test_strict_check_has_no_legacy_allowance(self) -> None:
        check = next(step for step in self.steps if "repo_signing.py check" in step.get("run", ""))
        self.assertNotIn("--legacy", check["run"])
        self.assertIn("--strict", check["run"])

    def test_no_expression_reaches_a_shell(self) -> None:
        for step in self.steps:
            self.assertNotIn("${{", step.get("run", ""), step.get("name"))


class CiWorkflowTest(unittest.TestCase):
    def test_checkouts_fetch_the_full_history(self) -> None:
        # repo_signing.py anchors on the signed index in history and refuses a shallow clone.
        jobs = load(WORKFLOWS / "ci.yml")["jobs"]
        for name in ["rustup-dist-publish", "bundles"]:
            checkout = next(step for step in jobs[name]["steps"] if step.get("uses", "").startswith("actions/checkout@"))
            self.assertEqual(checkout.get("with", {}).get("fetch-depth"), 0, name)


class PublishPackageHandoverTest(unittest.TestCase):
    def setUp(self) -> None:
        steps = load(PUBLISH)["jobs"]["publish"]["steps"]
        self.commit = next(step for step in steps if step.get("name") == "Commit repository update")["run"]

    def test_never_stages_a_bundle_or_the_index(self) -> None:
        adds = [line.strip() for line in self.commit.splitlines() if line.strip().startswith("git add")]
        self.assertEqual(adds, ["git add -- artifacts packages ':(exclude)*.bundle'"])
        self.assertIn("grep -E '\\.bundle$|^index\\.json$'", self.commit)

    def test_guard_runs_before_the_commit(self) -> None:
        self.assertLess(self.commit.index("refusing to commit a bundle"), self.commit.index("git commit"))


class PublishPackageBuildEnvTest(unittest.TestCase):
    """The guest SDK's cargo-yurt parses `cargo tree` output and fails on ANSI color codes.

    dtolnay/rust-toolchain exports CARGO_TERM_COLOR=always unless the variable is already set,
    so the job must set it to never before that step runs, and no step may turn it back on.
    """

    def setUp(self) -> None:
        self.job = load(PUBLISH)["jobs"]["publish"]

    def test_cargo_color_is_off_for_the_whole_job(self) -> None:
        self.assertEqual(self.job.get("env", {}).get("CARGO_TERM_COLOR"), "never")
        for step in self.job["steps"]:
            self.assertNotIn("CARGO_TERM_COLOR", step.get("env", {}), step.get("name"))
            self.assertNotIn("CARGO_TERM_COLOR", step.get("run", ""), step.get("name"))



class PublishPackagePortsTest(unittest.TestCase):
    """Which yurt-ports directory and which dependency stages each package builds from."""

    def setUp(self) -> None:
        self.wf = load(PUBLISH)
        self.steps = self.wf["jobs"]["publish"]["steps"]

    def step(self, name: str) -> dict:
        return next(step for step in self.steps if step.get("name") == name)

    def test_python_is_built_by_the_cpython_port(self) -> None:
        resolve = self.step("Resolve the port")["run"]
        self.assertIn("python) echo \"dir=cpython\"", resolve)
        self.assertEqual(self.steps.index(self.step("Resolve the port")), 0)
        for name in ["Reject duplicate package version", "Build package artifact", "Publish package metadata"]:
            step = self.step(name)
            self.assertEqual(step["env"]["PORT"], "${{ steps.port.outputs.dir }}", name)
            self.assertNotIn("inputs.package }}/", step["run"], name)

    def test_the_cpython_chain_is_built_before_its_packages(self) -> None:
        deps = self.step("Build dependency stages")
        for package in ["python", "numpy", "pyzmq", "perl", "curl", "git"]:
            self.assertIn(f"inputs.package == '{package}'", deps["if"])
        run = deps["run"]
        self.assertIn('chain="zlib bzip2 xz openssl libzmq sqlite cpython-base"', run)
        # python's interpreter links numpy's and pyzmq's extensions in.
        self.assertIn('python) ports="$chain pyzmq numpy" ;;', run)
        # numpy and pyzmq build here, against cpython-base (their own
        # build.sh outside port-cache.sh would look for cpython's full
        # stage), and the package step only packs them.
        self.assertIn('numpy) ports="$chain numpy" ;;', run)
        self.assertIn('pyzmq) ports="$chain pyzmq" ;;', run)
        self.assertEqual(deps["env"]["YURT_PORT_CACHE"], "off")
        self.assertEqual(
            self.step("Build package artifact")["env"]["YURT_PACKAGE_SKIP_BUILD"],
            "${{ (inputs.package == 'numpy' || inputs.package == 'pyzmq') && '1' || '0' }}",
        )

    def test_no_checkout_leaves_the_publish_token_in_a_clone(self) -> None:
        for step in self.steps:
            uses = step.get("uses", "")
            with_ = step.get("with", {})
            if uses.startswith("actions/checkout@") and "token" in with_:
                self.assertIs(with_.get("persist-credentials"), False, step.get("name"))
        commit = self.step("Commit repository update")
        self.assertEqual(commit["env"]["PUSH_TOKEN"], "${{ secrets.PACKAGE_PUBLISH_TOKEN }}")
        # The header is built after `set +x`, masked, and given to this push only.
        run = commit["run"]
        self.assertLess(run.index("set +x"), run.index("auth=$("))
        self.assertIn('echo "::add-mask::$auth"', run)
        self.assertIn('git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $auth" push origin HEAD:main', run)

    def test_only_node_gets_a_token_for_its_build(self) -> None:
        env = self.step("Build package artifact")["env"]
        self.assertEqual(env["GH_TOKEN"], "${{ inputs.package == 'node' && secrets.PACKAGE_PUBLISH_TOKEN || '' }}")

    def test_new_packages_are_choices_and_sudo_is_not(self) -> None:
        # PyYAML reads the `on:` key as True.
        trigger = self.wf.get("on", self.wf.get(True))
        options = trigger["workflow_dispatch"]["inputs"]["package"]["options"]
        for package in ["python", "numpy", "pyzmq", "jupyter-kernel", "node", "gemini-cli", "perl", "make", "zstd", "pkgconf"]:
            self.assertIn(package, options)
        # The image's bootstrap: an install of sudo would replace the sudo it runs under.
        self.assertNotIn("sudo", options)


if __name__ == "__main__":
    unittest.main()
