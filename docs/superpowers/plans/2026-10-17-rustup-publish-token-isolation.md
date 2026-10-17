# Rustup Publish Token Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the rustup-dist publisher into five jobs so code from a caller-chosen `tooling_ref` never shares a VM with a write credential (issue #13).

**Architecture:** A trusted `plan` job fixes every identity value; `emit` (tooling, read-only token) produces two artifacts; `publish` (job-scoped `GITHUB_TOKEN`) validates them as data and creates the release; `check` (tooling, read-only token) runs the URL checker; `push` (environment-held PAT) revalidates and commits `dist/`. Validation lives in one stdlib Python module; the shell script becomes thin phase subcommands.

**Tech Stack:** bash, Python 3.12+ stdlib (`tarfile`, `tomllib`, `json`, `hashlib`), GitHub Actions, `unittest`, PyYAML (static workflow test only), shellcheck, actionlint.

**Spec:** `docs/superpowers/specs/2026-10-17-rustup-publish-token-isolation-design.md` (approved at dc35720). Read it first; this plan only records the decisions it leaves open.

## Global Constraints

- Tooling code (emitter, checker) runs only in `emit` and `check`; those jobs hold only `YURT_RUST_READ_TOKEN`. `publish` and `push` run only code from `trusted/` (checkout at `github.sha`), never anything from an artifact.
- `RUSTUP_DIST_PUBLISH_TOKEN` is referenced by `push` only, as an environment secret (`rustup-publish`). `publish` uses `secrets.GITHUB_TOKEN` with `permissions: contents: write`.
- Identity (yurt-rust release tag, version, revision, date, `rustup-<version>-<date>` tag, producer host, tooling SHA) comes only from `plan` outputs. Artifact ids come from `emit` (untrusted: all digits, current run only). Digests come from `publish`.
- Every `needs.*.outputs.*` and `inputs.*` reaches scripts through `env:`; never inside `run:`.
- Python run as `python3 -I`. No caches in `publish`/`push`. `persist-credentials: false` on every checkout. Actions pinned by commit SHA. No `pull_request` trigger.
- `publish` and `push` refuse unless `GITHUB_RUN_ATTEMPT == 1`.
- The dated manifest version string is exactly `"<X.Y.Z> (yurt <A.B.C>)"`. Provenance keys bound to the plan: `release_tag`, `rust_version`, `revision`, `date`, `producer_host`.
- Red test committed before each implementation (separate commits). KISS. Commit trailers: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_017qLbE3rYSik3N8AiPzLByi`. No pushes, dispatches, releases or merges.

## Review Focus

1. A hostile `dist.tar` (traversal, absolute, duplicate, link, device, oversized, extra or missing members) must be rejected before any extraction, release mutation or push.
2. Valid-looking artifacts with the wrong identity (version, revision, date, tag, producer host) must be rejected, not published under a different tag.
3. A newer or same-date publication landing between `publish` and `push` must make `push` fail closed, never overwrite.
4. Component tarballs that are missing, extra, unknown-target or whose bytes differ from the manifest hash must be rejected in `publish`.
5. A rerun of one privileged job, or a missing/mismatched digest between `publish` and `push`, must fail closed.

---

### Task 1: Validation module `tools/rustup_dist_stage.py`

**Files:**
- Create: `tools/rustup_dist_stage.py`, `tests/test_rustup_dist_stage.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Plan(release_tag: str, version: str, revision: str, date: str, producer_host: str, tooling_sha: str, tag: str)`; `tag` is `rustup-<version>-<date>`.
  - `make_plan(release: str, date: str, producer_host: str, tooling_sha: str, today: str) -> Plan`: same input rules as `publish_rustup_dist.sh` today (release shape, real calendar date, not after `today`, three producer hosts, 40-hex SHA); raises `StageError`.
  - `check_date_rules(site: Path, plan: Plan) -> None`: `site/dist/<date>/` absent and `<date>` strictly later than the newest `site/dist/*` dated dir (any version).
  - `read_dist_tar(path: Path, plan: Plan) -> dict[str, bytes]`: container checks without extracting (exactly the five expected member paths, regular files, no duplicates/absolute/`..`, per-member and total caps), then content checks (moving == dated bytes; sidecars match; `tomllib` date and version label equal the plan; provenance keys bound to the plan). Returns path → bytes.
  - `check_components(dir: Path, dist: dict[str, bytes], plan: Plan) -> dict[str, str]`: files in `dir` are exactly the basenames of the manifest URLs, URLs under `https://github.com/YurtOS/yurt-packages/releases/download/<tag>/`, known packages and targets, sha256 equals manifest hash, all regular files (`lstat`); returns name → sha256.
  - `digest_manifest(tar: Path, dist: dict[str, bytes]) -> dict[str, str]`: sha256 of `dist.tar` and of each member; `verify_digests(expected, actual)` raises on missing or mismatched.
  - `apply_dist(site: Path, dist: dict[str, bytes], plan: Plan) -> list[str]`: writes exactly the five files (mode 0644) into a checkout, sets provenance `tooling_sha` from the plan, returns the written relative paths.
  - CLI: `rustup_dist_stage.py plan|validate-dist|validate-components|apply ...` with `KEY=VALUE` output for `plan`.
- Consumes: the emitter's output shape (read `scripts/emit_rustup_dist.py` in `YurtOS/yurt-rust` `origin/main` for the exact five paths and the provenance key set).

- [ ] **Step 1:** Write failing tests in `tests/test_rustup_dist_stage.py`: one happy-path fixture builder (valid five-member `dist.tar`, matching components), then a rejection test per Review Focus 1, 2 and 4 (each with the exact error substring): traversal, absolute, duplicate, symlink, device, oversized, extra, missing members; wrong version, revision, date, tag, producer host in manifest and in provenance; moving ≠ dated; sidecar mismatch; missing, extra, unknown-target, hash-mismatched component; `dist/<date>` exists; date not newer than the newest existing dated dir; future or non-calendar date; bad producer host; non-40-hex SHA. Run `python3 -I -m unittest tests.test_rustup_dist_stage`: all fail (module absent). Commit: `test: failing tests for the staged artifact validator`.
- [ ] **Step 2:** Implement the module as specified. Run the tests: all pass. Commit: `feat: validate staged rustup dist artifacts as data`.

### Task 2: Phase subcommands in `tools/publish_rustup_dist.sh`

**Files:**
- Modify: `tools/publish_rustup_dist.sh`, `tests/test_publish_rustup_dist.py`

**Interfaces:**
- Consumes: Task 1 CLI and functions.
- Produces: `publish_rustup_dist.sh <plan|emit|publish|push>`, env only (names listed here are the contract for Task 3):
  - `plan`: inputs `RUST_RELEASE PUBLISH_DATE PRODUCER_HOST TOOLING_REF`, read-token `gh`/`git ls-remote` for the SHA; prints `release_tag= version= revision= date= producer_host= tooling_sha= tag=` lines to stdout.
  - `emit`: plan values as `PLAN_*` env, read token `GH_TOKEN`, `YURT_RUST_TOOLING_DIR`, `YURT_SITE_DIR` (anonymous checkout), `OUT_DIR`; downloads the release, checks its manifest equals the plan, runs the emitter, writes `OUT_DIR/dist/dist.tar` and `OUT_DIR/components/*`.
  - `publish`: `PLAN_*`, `DIST_DIR`, `COMPONENTS_DIR`, `YURT_SITE_DIR` (fresh `main`), `GH_TOKEN` = `GITHUB_TOKEN`; validates, re-checks date rules and release eligibility (existing rules: published manifest refuses; release without manifest is recreated; `--latest=false`), creates the release, uploads components, prints digests as `digest:<name>=<sha256>` lines.
  - `push`: `PLAN_*`, `DIST_DIR`, `DIGESTS` (the `publish` output), `YURT_SITE_DIR`, `PUSH_TOKEN`; verifies digests, then up to 3 attempts of fetch → reset to `origin/main` → `check_date_rules` → `apply_dist` → assert staged paths are exactly the five → commit → push with the token only in the push's environment.
  - `publish` and `push` exit nonzero unless `GITHUB_RUN_ATTEMPT=1`. The check step is a direct call to `$TOOLING/scripts/verify_rustup_dist_urls.py`, not a phase.

- [ ] **Step 1:** Rework the harness so each phase runs in its own temp dir/process with its own env (a helper `run_phase(name, **env)`; shared fake `gh`, emitter and checker stay). Add failing tests mapped from the 17 existing ones: invalid inputs, future date, empty date → `plan`; tag/revision vs release manifest, `WORK_DIR` handling, scratch removal, startup crash, read-token-only → `emit`; published manifest refuses, no manifest + release recreated, upload failure, stale checkout does not delete, release mutation after the date-rule recheck → `publish`; push race retried (re-reset and recheck, not rebase), reused checkout leaves no modified tracked file, token only in the push environment and never stored, records `tooling_sha`, checker failure means `push` is never run in the sequence helper → `push`/sequence. Add new failing tests for Review Focus 3 and 5: a newer or same-date publication lands between `publish` and `push` (push fails closed, nothing pushed); `GITHUB_RUN_ATTEMPT=2` refuses in `publish` and `push`; missing and mismatched digest; successful `push` with no components directory; no write token in `emit`'s environment; no read token in `publish`/`push`. Run `python3 -I -m unittest tests.test_publish_rustup_dist`: the new and reworked tests fail. Commit: `test: failing phase tests for the five-job publisher`.
- [ ] **Step 2:** Implement the four phases in the script on top of the Task 1 module (keep `gh_cli`, the trap/reset logic, `--latest=false`, the bounded retry). Run the tests and `shellcheck tools/publish_rustup_dist.sh`: pass. Commit: `feat: split the rustup dist publisher into plan, emit, publish and push phases`.

### Task 3: Five-job workflow, static test, CI and README

**Files:**
- Modify: `.github/workflows/publish-rustup-dist.yml`, `.github/workflows/ci.yml`, `README.md`
- Create: `tests/test_publish_workflow.py`

**Interfaces:**
- Consumes: Task 2 phase contract (env names).
- Produces: workflow jobs `plan`, `emit`, `publish`, `check`, `push` with the exact `needs` lists from the spec; artifacts `rustup-dist` (single `dist.tar`) and `rustup-components` (compression level 0), each downloaded into its own empty directory by id; `emit` outputs the two artifact ids; `publish` outputs `digests`; `push` has `environment: rustup-publish`.

- [ ] **Step 1:** Write `tests/test_publish_workflow.py` (PyYAML) asserting every static property in the spec's Tests section (job chain and `needs` lists; secrets per job; `push` environment; minimal per-job `permissions`; no `pull_request`; `persist-credentials: false` everywhere; distinct `trusted/` and `site/` paths; actions pinned by SHA with no `run-id`/`github-token`; no cache step in `publish`/`push`; no `needs.*`/`inputs.*` expression inside any `run:`; `rustup-publish` only referenced by `push`; concurrency group `publish-package`). It fails against the current single-job workflow. Commit: `test: failing static test for the five-job publish workflow`.
- [ ] **Step 2:** Rewrite the workflow to the design; add `pip install pyyaml` and the new test files to the CI job (`ci.yml`), plus shellcheck/actionlint for the new script and workflow. Update README: the two tokens, the `rustup-publish` environment setup (branch `main` only, no tag rules, delete a same-named repo secret), the first-dispatch checklist from the spec (org cap on `GITHUB_TOKEN` write; read token readable from any branch, accepted), rerun policy (new full dispatch), and that `PACKAGE_PUBLISH_TOKEN` is untouched. Run all tests, `actionlint`, `shellcheck`: pass. Commit: `feat: run the rustup dist publisher as five isolated jobs`.

### Task 4: Whole-suite verification

- [ ] Run every test file, `shellcheck`, `actionlint` on the final tree; read the final workflow against the spec's job table; record anything only a hosted run can prove (secret exposure per job, environment gating, artifact transfer by id, `run_attempt` refusal, real Pages trigger) in the PR body. No dispatch.
