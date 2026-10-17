# yurt-packages

Signed Yurt package repository metadata and release artifacts.

This repository is the package repository consumed by `pkg update`.
Generated files must match the `yurt-pkg` client contract:

```text
index.json
index.json.bundle
packages/<name>.json
artifacts/<name>/<version>/<name>-<version>-<build>.yurtpkg
artifacts/<name>/<version>/<name>-<version>-<build>.yurtpkg.bundle
```

Ports live in [`YurtOS/yurt-ports`](https://github.com/YurtOS/yurt-ports).
That repository owns build recipes, patches, and port scripts. CI for this
repository builds those ports, publishes package artifacts, regenerates
`packages/*.json` and `index.json`, and signs `index.json`.

Publishing is owned by the manual `Publish Package` GitHub Actions workflow in
this repository. The workflow checks out the source-light port from
`YurtOS/yurt-ports`, builds the selected source repository, runs
`yurt-repo-ci publish-local --reject-existing`, commits the generated metadata,
and pushes the repository update. Re-publishing the same package
`version`/`build` is expected to fail before repository files are modified.

For local end-to-end loops, `yurt-repo-ci publish-local` is the CI stand-in. It
copies a built `.yurtpkg` into `artifacts/`, regenerates package metadata and
`index.json`, and writes placeholder bundles so `pkg --features test-fixtures`
can exercise the same repository layout for both `update` and `install`.
Real Sigstore bundle generation remains CI-owned.

## Rustup dist

This repository also serves the static dist server that stock `rustup` uses to
install the patched Yurt Rust toolchain (issue
[YurtOS/yurt-rust#66](https://github.com/YurtOS/yurt-rust/issues/66)).

- Component tarballs (`<package>-<X.Y.Z>-<target>.tar.gz`) are assets of the
  release `rustup-<X.Y.Z>-<date>` in this repository. They are never committed.
- Manifests are committed under `dist/` and served from GitHub Pages at
  `<pages root>/dist/`:

  ```text
  dist/channel-rust-<X.Y.Z>.toml(.sha256)            moving manifest for the channel
  dist/<date>/channel-rust-<X.Y.Z>.toml(.sha256)     immutable, pinnable as <X.Y.Z>-<date>
  dist/<date>/yurt-release-manifest.json             provenance, including tooling_sha
  ```

To publish, run the manual `Publish Rustup Dist` workflow with:

| Input | Meaning |
| --- | --- |
| `rust_release` | yurt-rust release tag, `rust-vX.Y.Z-A.B.C` |
| `tooling_ref` | yurt-rust ref with the emitter and checker (default `main`; independent of the release tag) |
| `date` | `YYYY-MM-DD`, empty means today (UTC) |
| `producer_host` | host whose archive supplies the guest `std` (default `x86_64-unknown-linux-gnu`) |

The workflow runs five jobs, each on a fresh VM, in the order
`plan → emit → publish → check → push`. The phases are in
`tools/publish_rustup_dist.sh` (`plan`, `emit`, `publish`, `push`), and the
validation is in `tools/rustup_dist_stage.py`.

| Job | What it does | Credential |
| --- | --- | --- |
| `plan` | validates the inputs, resolves an empty date to today (UTC) once, resolves `tooling_ref` to a commit SHA | `YURT_RUST_READ_TOKEN` |
| `emit` | runs the yurt-rust emitter at the planned SHA and uploads two artifacts: `rustup-dist` (one `dist.tar` with the five manifest files) and `rustup-components` (the tarballs) | `YURT_RUST_READ_TOKEN` |
| `publish` | validates both artifacts as data, rechecks eligibility on a fresh `main`, creates the release and uploads the components | the job's `GITHUB_TOKEN` (`contents: write`) |
| `check` | runs the yurt-rust URL checker: every manifest URL must resolve to its declared sha256 | `YURT_RUST_READ_TOKEN` |
| `push` | verifies the digests from `publish`, then commits the five files onto a fresh `main` and pushes | `RUSTUP_DIST_PUBLISH_TOKEN` (environment `rustup-publish`) |

Code from `tooling_ref` (the emitter and the checker) runs only in `emit` and
`check`, which hold only the read-only token. `publish` and `push` run only this
repository's scripts at the dispatched commit. They take every identity value
(version, revision, date, tag, producer host, tooling SHA) from `plan`, never
from an artifact.

A failed upload or check publishes no manifest. If the release
`rustup-<X.Y.Z>-<date>` exists and a manifest for that date is already
published, the run refuses. The date must also be strictly later than any
existing dated directory under `dist/`, so a same-day republish is not possible;
use a later date (dates in the future are rejected). A release left over from a
failed run (no published manifest) is deleted and recreated.

Runs are serialized with `Publish Package` by their shared `publish-package`
concurrency group. Deleting a left-over release is safe only because of this
group. A run now holds the group longer, which can replace a pending
`Publish Package` run (a group keeps one pending run); if that happens, dispatch
that workflow again.

`push` never rebases. Each of its (at most three) attempts resets to a fresh
`main`, rechecks the date rules and stages exactly the five files. A publication
that lands in the meantime makes the run fail instead of being merged over.

**Reruns.** After any failure, dispatch a new full run. `publish` and `push`
refuse unless `github.run_attempt` is 1, so "Re-run failed jobs" fails in those
jobs. A rerun could otherwise skip validation or the URL check. A failed run can
leave a partial release, uploaded assets without manifests, or (if `push` timed
out after it landed) a published manifest. The next dispatch handles the first
two by recreating the release. For the third it refuses: check `main`.
Artifacts are kept for one day.

Until [YurtOS/yurt-rust#72](https://github.com/YurtOS/yurt-rust/pull/72) merges,
the default `tooling_ref` of `main` does not contain the emitter and the run
fails; pass the PR's branch or SHA as `tooling_ref` until then.

### Setup and access

- `YURT_RUST_READ_TOKEN` (repository secret): a fine-grained PAT for
  `YurtOS/yurt-rust` only, with Contents: read. It is used for the tooling
  checkout, `gh release download` and the SHA lookup, by `plan`, `emit` and
  `check` only.
- `RUSTUP_DIST_PUBLISH_TOKEN`: a fine-grained PAT for `YurtOS/yurt-packages`
  only, with Contents: read and write and no Workflows permission. Store it as a
  secret of the `rustup-publish` environment, not as a repository secret. Only
  `push` reads it. It is a PAT because its push to `main` is what triggers
  `pages.yml`; a push made with the default `GITHUB_TOKEN` would not.
- Create the `rustup-publish` environment under Settings > Environments:
  - set deployment branches to "Selected branches" with `main` only, and add no
    tag rules;
  - add `RUSTUP_DIST_PUBLISH_TOKEN` as an environment secret;
  - delete any repository secret of the same name, which any workflow on any
    branch could read.
- Release creation and asset upload in `publish` use that job's `GITHUB_TOKEN`
  (`contents: write`), which expires with the job.
- `PACKAGE_PUBLISH_TOKEN` is untouched: `Publish Package` still uses it, and this
  workflow no longer does.
- First-dispatch checklist (needs the maintainer's go; nothing here has run on a
  hosted runner yet):
  - The organization may need to approve the two fine-grained PATs.
  - An organization or repository policy that limits `GITHUB_TOKEN` to read makes
    `publish` fail: a job's `contents: write` cannot exceed that cap. Allow
    read and write workflow permissions for this repository.
  - `YURT_RUST_READ_TOKEN` is a repository secret, so a workflow on any branch can
    read it. This is accepted: it is read-only, on the repository whose tooling
    the dispatcher already chooses.
  - Before dispatching, run the tooling smoke test locally against a
    `yurt-rust` checkout at the `tooling_ref` you will use:
    `YURT_RUST_TOOLING_DIR=<checkout> python3 -m unittest discover -s tests -p 'test_publish_workflow.py'`
    (needs PyYAML). It runs the real emitter and checker `--help`. CI cannot
    read `yurt-rust`, so there it skips.
  - Only a hosted run proves these: which secrets each job sees, the
    environment's branch gating, artifact transfer by id, the `run_attempt`
    refusal and the real Pages trigger.
- GitHub Pages must be enabled for this repository with source "GitHub Actions"
  before `pages.yml` can deploy. For the `YurtOS` organization and this
  repository the default Pages root, which is what users pass as `--dist-root`,
  is `https://yurtos.github.io/yurt-packages`. If the organization uses a custom
  Pages domain, the root is that domain plus `/yurt-packages` instead; confirm it
  under Settings > Pages.
- The acceptance workflow in `yurt-rust` uploads about 1 GB of artifacts per run
  (see its README).
- Privacy: yurt-rust is private, but publishing through stock rustup makes the
  patched toolchain binaries public release assets of `YurtOS/yurt-packages` and
  publishes yurt-rust's git SHA, release tag and asset names, sizes and hashes in
  `dist/<date>/yurt-release-manifest.json`; this was decided by hosting the dist
  under `yurt-packages`.

Clients use `yurt-rustup-init.sh` from the `yurt-rust` repository. It reads its
pins file `scripts/rustup-init.pins` relative to its own location, so run it
from a `yurt-rust` checkout; do not `curl | bash` the script alone.
