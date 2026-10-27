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
this repository. It runs two jobs. `build` checks out the source-light port
from `YurtOS/yurt-ports`, builds it, and uploads only the `.yurtpkg` (with its
sha256). `publish`, on a fresh runner, checks that archive against the port's
manifest at the same yurt-ports commit, runs
`yurt-repo-ci publish-local --reject-existing`, commits the archive and its
`packages/<name>.json` (nothing else may be staged), and pushes. Re-publishing the same package
`version`/`build` is expected to fail before repository files are modified.
It never commits `index.json` or a bundle: `Release` does that (below).

Main may move while a package builds (a merged pull request, a `Release`
run). The commit step then rebases its commit onto the new main and pushes
again, up to five times. It refuses instead, without pushing, when main changed
the package's own files since the run's checkout: `packages/<name>.json`,
anything under `artifacts/<name>/` other than the signature bundles of releases
already there, or the bundle of the archive it adds. Such a change is another
publish of the same package, or its removal; dispatch the publish again on the
new main if it should still go out.

**Publish when the version changes.** Archives are committed to git, so every
publish grows the repository for good (the Python set adds about 23 MB). Before
anything is built, the workflow's "Publish only a new version" step
(`tools/publish_policy.py`) compares the port's `yurt-pack.toml` with
`packages/<name>.json`:

- A `version`/`build` that is already published is refused, whatever the new
  archive's bytes: a release is never replaced, and `Release` never re-signs
  one. An identical rebuild has nothing to add.
- Another `build` of a published `version` is refused unless the dispatch sets
  `allow_new_build`. Set it for a packaging fix that has to ship without a
  version change, and for the Python set, whose shared `yurt_N` build moves
  without a CPython version change. Bump the port's `build` in yurt-ports
  first.
- A release that pkg would not rank above the newest published one is refused
  unless the dispatch sets `allow_downgrade`. pkg picks the highest semver
  `version`, then the highest `N` of a `yurt_N` build (any other build, such as
  `1`, counts as 0), and skips yanked releases. So openssl 3.5.8 is refused
  while 4.0.0 is published, and so is `0.1.1-yurt_0` next to a published
  `0.1.1-2`. A downgrade publish adds an archive that `pkg install` never
  picks on its own; set the input only when that is the point (to pin
  `name@version` explicitly, for example). A port still on a plain numeric
  build (`1`, `2`) ranks 0 against every published build of its version, so a
  rebuild of that version needs both inputs; moving it to `yurt_N` avoids that.
- A new `version`, or a package not published yet, goes ahead.

**Tokens.** `build` reads the private repositories with `PACKAGE_READ_TOKEN`,
a repository secret: a fine-grained token with Contents: read on yurt-ports,
yurt-pkg, yurtos-kernel, yurt-toolchain and yurt-rust (the guest SDK
installer fetches both releases), yurt-node, yurt-go and yurt-clang. `publish` reads yurt-ports and yurt-pkg with it, and
only its last step, the push, gets `PACKAGE_PUBLISH_TOKEN`. Neither job's
`GITHUB_TOKEN` can write. Every checkout keeps no credential
(`persist-credentials: false`).

**Dry run.** `dry_run: true` builds, checks and commits as a real publish
does, prints the commit, and stops before the push: the package never reaches
main. A dry run may be dispatched from any branch, which is how a change to
this workflow is tried before it merges. `Release` follows only runs on main,
dry or not: one after a dry run on main finds nothing new to sign, but still
commits a renewed index to main. Dispatch dry runs from a branch to avoid
that.

Each package builds from the yurt-ports port of the same name, at yurt-ports
main's head or at the `ports_sha` input (a commit in main's history). A real
publish runs from this repository's main only. The packages this workflow
gained with the slim playground need yurt-ports#265 on main: python,
jupyter-kernel, python-seal, numpy's and pyzmq's skipped rebuild, and node's and
gemini-cli's `yurt_N` builds (published before it, they would go out as
`22.23.3-3` and `0.64.0-1`, which pkg ranks equal to a later `yurt_0`). Every
package publishes on its own: no image or playground release is involved, and
a sandbox picks up a new build with `pkg update` / `pkg upgrade`. Notes:

- **The Python set** (`python`, `numpy`, `pyzmq`, `jupyter-kernel`,
  `python-seal`) is the exception. `python`'s interpreter has NumPy's and
  pyzmq's C extensions linked in, the `numpy` and `pyzmq` packages hold only
  the Python files that go with them, and `python-seal` (the playground's
  Suspend/Resume interpreter) runs `python`'s standard library. So publish all
  five from one yurt-ports commit (the same `ports_sha`), with the one `yurt_N`
  build they share in yurt-ports, which marks the set; the playground installs
  the newest build the first four have, and a suspended notebook resumes only
  on the set it was suspended with.
  - The `python`, `numpy` and `pyzmq` runs build the cpython chain first, with yurt-ports'
    `scripts/port-cache.sh`, cache off: zlib, bzip2, xz, OpenSSL, libzmq,
    SQLite and the interpreter the extensions compile against.
  - `python` then builds pyzmq, numpy and the interpreter with them linked in,
    and packs it.
  - `numpy` and `pyzmq` build themselves.
  - `python-seal` builds the C-only sealable interpreter (`cpython-seal`), whose
    build fails if the kernel's checkpoint admission would refuse it.
- `jupyter-kernel` fetches the pinned, public `jupyter-payload-v0.0.1`
  release of this repository; nothing is built.
- `node` stages a release of the private YurtOS/yurt-node, which its port
  fetches with `gh` using `PACKAGE_READ_TOKEN`. The node binary is the one the
  yurt-node release built, with the guest SDK named in its notes.
- `perl` cross-builds with perl-cross, which needs a Linux runner (it does).
- `sudo` is not offered: the image's `sudo` is what `sudo pkg install` runs
  under, and replacing it from a package is not a supported upgrade.

For local end-to-end loops, `yurt-repo-ci publish-local` is the CI stand-in. It
copies a built `.yurtpkg` into `artifacts/`, regenerates package metadata and
`index.json`, and writes placeholder bundles so `pkg --features test-fixtures`
can exercise the same repository layout for both `update` and `install`.
Those placeholders are never committed.

## Signing

`pkg` verifies `index.json` and every archive against a Sigstore bundle next to
it (`<file>.bundle`), offline, with the public-good trusted root the image
ships. Images pin the signer in `/etc/yurt-pkg/trusted-repos.toml`:

```toml
signing_subject = "https://github.com/YurtOS/yurt-packages/.github/workflows/release.yml@refs/heads/main"
signing_issuer  = "https://token.actions.githubusercontent.com"
```

Only `.github/workflows/release.yml` (`Release`), running on `main`, can produce
that identity. It signs keyless with the job's GitHub Actions OIDC token (a
Fulcio certificate and a Rekor log entry) through `cosign sign-blob --bundle`,
which writes a Sigstore bundle v0.3
(`application/vnd.dev.sigstore.bundle.v0.3+json`) with a message signature
over the raw file bytes, the format the `pkg` verifier reads.

Trust in that identity is trust in write access to `main`: `Release` signs
what `main` holds. `tools/repo_signing.py` limits that to releases nobody signed
before. It reads every commit, on any line of history, that holds a validly
signed `index.json` (so CI and `Release` check out the full history):

- the current index may not have a lower `index_version` than any of them;
- every release (name, version, build) any of them ever listed was signed. Its
  record must never change, its url must not move to another release (moving
  an archive means publishing a new build), its
  bundle must stay valid, and it is never signed again, even after it was
  dropped from the metadata and re-added;
- only releases no signed index ever listed may be unsigned ("pending"), and
  only those get signed.

History bundles are verified against the current trusted root. If Sigstore
retires a key from it, old bundles signed under that key stop verifying and drop
out of the history, together with the protection of releases that only they
listed. Any release a later signed index also listed keeps its protection.

That history is the anchor, so protect `main` against force-pushes and
deletion (a branch protection rule or ruleset that still lets
`github-actions[bot]` push): a force-pushed or orphan `main` drops the signed
history, and with it the rollback and re-sign protection for the next run.
Clients still refuse an `index_version` below the one they cached.

Each run:

1. plans, on the committed tree, which archives to sign: pending releases, and
   on the first run the legacy archives. Any check problem stops the run;
2. regenerates `index.json` from `packages/*.json`. Before that, it verifies the
   current `index.json` against its bundle with cosign; only before the first
   signed index may that bundle be the placeholder. The new index gets
   `index_version` above the anchor's, `generated_at` now, and `expires_at` 7
   days later. Clients accept an index until 30 days past `expires_at`, and
   never one whose `index_version` is not higher than the one they cached;
3. signs the planned archives and `index.json`;
4. runs `tools/repo_signing.py check --strict --cosign cosign`, which runs
   `cosign verify-blob` with the pinned identity on every bundle. Only a
   cosign must be v3.0.6, and once per run it must verify the in-repo goreleaser
   fixture (`tests/fixtures/sigstore/`). That health check fetches the trusted
   root over TUF into a fresh per-run `TUF_ROOT`. Every later verify runs offline
   against that one `trusted_root.json` (`--trusted-root`). cosign exits 1 for
   every error, so after the health check an exit 1 can only be about the bundle,
   and it counts as invalid (a malformed bundle in history is skipped). A network
   failure can only fail the health check. These abort the run: a failed health
   check, no fetched `trusted_root.json`, a wrong version, a crash, a timeout or
   a missing binary;
5. drops the signed entries from `tools/legacy_placeholder_bundles.txt` and
   commits. It then fetches `main`: if another writer has changed the index, a
   bundle, `packages/` or `artifacts/` since the checkout, it fails (the next run
   signs the new state). Otherwise it rebases and pushes with the job's
   `GITHUB_TOKEN`.

It runs when a `Publish Package` run on `main` succeeds, by manual dispatch on
`main`, and weekly (Mondays 05:17 UTC). The weekly run renews `expires_at`:
every client rejects an index more than 37 days old. It has its own concurrency
group, `release`.

GitHub disables scheduled workflows in a public repository "when no repository
activity has occurred in 60 days"
([docs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)).
The docs do not define activity or say whether the commits `Release` pushes
itself count, so don't rely on them. If the repository has been quiet, check
`gh workflow view release.yml` and run `gh workflow enable release.yml`. A
disabled schedule lets the index expire.

CI (`bundles` job) runs `tools/repo_signing.py check` with `cosign verify-blob`
on every pull request and push. Every committed bundle must be valid. The only
unsigned archives allowed are:

- pending releases, between a `Publish Package` commit and the `Release` run
  after it. A placeholder is never allowed;
- before the first signed index only, the 12 archives in
  `tools/legacy_placeholder_bundles.txt`. These were committed before `Release`
  existed, and each is pinned to its sha256. Once a signed index exists, the
  list must be empty. Never add an entry.

Until `Release` re-signs, `pkg update` can fail for a package whose
`packages/<name>.json` a publish just changed: its hash no longer matches the
signed index, and clients fail closed.

The checker requires the Rekor v1 log entry (`hashedrekord` 0.0.1) that cosign
v3.0.6 writes on public-good today. If Sigstore moves signing to Rekor v2,
`Release` fails at its check: prove the new bundle against the `pkg` verifier
before relaxing it.

Setup: no secret. `Release` needs `contents: write` and `id-token: write` for
its `GITHUB_TOKEN`, which the workflow requests itself; the repository or the
organization must allow workflows to request them, and nothing (branch
protection or a ruleset) may block `github-actions[bot]` pushing to `main`.
A push made with `GITHUB_TOKEN` does not trigger other workflows, so CI does
not run on `Release` commits; the strict check in `Release` stands in for it.

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
`tools/publish_rustup_dist.sh` (`plan`, `emit`, `publish`, `push`), which does
only tokens, `gh`, `git` and process boundaries. Every rule (inputs, plan,
release manifest, packing, artifact validation, date rules) is in
`tools/rustup_dist_stage.py`.

| Job | What it does | Credential |
| --- | --- | --- |
| `plan` | validates the inputs, resolves an empty date to today (UTC) once, resolves `tooling_ref` to a commit SHA | `YURT_RUST_READ_TOKEN` |
| `emit` | runs the yurt-rust emitter at the planned SHA and uploads two artifacts: `rustup-dist` (one `dist.tar` with the five manifest files) and `rustup-components` (the tarballs) | `YURT_RUST_READ_TOKEN` |
| `publish` | validates both artifacts as data, rechecks eligibility on a fresh `main`, creates the release and uploads the components | the job's `GITHUB_TOKEN` (`contents: write`) |
| `check` | extracts `dist.tar` through the validator (only the five expected files), then runs the yurt-rust URL checker: every manifest URL must resolve to its declared sha256 | `YURT_RUST_READ_TOKEN` |
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

### Security model (what the isolation covers)

The five-job split keeps code from `tooling_ref` away from
`RUSTUP_DIST_PUBLISH_TOKEN`, and the isolation contract covers that token only.
`push` is the only job that can read it. `push` restores no cache, and it
accepts only the artifact whose digests `publish` computed.

It does **not** cover `PACKAGE_PUBLISH_TOKEN`:
- Tooling code in `emit` and `check` runs on the runner as the runner user.
  It can act with that job's Actions runtime token, and a dispatch on `main`
  writes cache entries scoped to `main`.
- `publish-package.yml`'s `build` job restores and saves its own cache
  (`Swatinem/rust-cache`, key `publish-package-build`), with read-only
  credentials. Its `publish` job, the only one that sees
  `PACKAGE_PUBLISH_TOKEN`, restores no cache.

Also accepted: tooling code can reach the read-only `YURT_RUST_READ_TOKEN` in
the jobs it runs in.

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
  - **First, before any dispatch:** under Settings > Environments, confirm that:
    - `rustup-publish` exists, with deployment branches set to `main` only;
    - it holds `RUSTUP_DIST_PUBLISH_TOKEN`;
    - **no** repository secret named `RUSTUP_DIST_PUBLISH_TOKEN` remains.

    This matters because a dispatch creates a missing environment automatically,
    with no branch rule. `push` would then read the repository secret, which a
    workflow on any branch can also read.
  - The organization may need to approve the two fine-grained PATs.
  - The repository's or the organization's Actions settings may limit what
    `GITHUB_TOKEN` can do. If `publish` fails with a 403 on `release create`,
    check those settings. Confirm this at the first dispatch.
  - `YURT_RUST_READ_TOKEN` is a repository secret, so a workflow on any branch can
    read it. This is accepted: it is read-only, on the repository whose tooling
    the dispatcher already chooses.
  - Before dispatching, run the tooling smoke test locally against a
    `yurt-rust` checkout at the `tooling_ref` you will use:
    `YURT_RUST_TOOLING_DIR=<checkout> python3 -m unittest discover -s tests -p 'test_publish_workflow.py'`
    (needs PyYAML). It runs the real emitter and checker `--help`, and compares
    the emitter's `write_manifest` byte for byte with the validator's vendored
    `render_manifest`. CI cannot read `yurt-rust`, so there it skips.
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
- Artifacts are public while they exist. This repository is public, so any
  signed-in GitHub user can download the artifacts that `emit` uploads (the
  re-split release archives, about 1 GB, kept for one day). This holds after a
  failed or unintended run too. Only dispatch for a release that is meant to be
  published.
- A missing `PUSH_TOKEN` (none in the environment and no repository secret of
  the same name) is detected only in `push`. By then `publish` has already
  created the release. The next dispatch recreates a release that has no
  published manifest.

Clients use `yurt-rustup-init.sh` from the `yurt-rust` repository. It reads its
pins file `scripts/rustup-init.pins` relative to its own location, so run it
from a `yurt-rust` checkout; do not `curl | bash` the script alone.
