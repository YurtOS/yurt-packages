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

All logic is in `tools/publish_rustup_dist.sh`. It uploads the components, checks
that every manifest URL resolves to its declared sha256, and only then commits
the manifests. A failed upload or check publishes no manifest. If the release
`rustup-<X.Y.Z>-<date>` exists and a manifest for that date is already
published, the run refuses; use a new date. A release left over from a failed
run (no published manifest) is deleted and recreated. Runs are serialized by the
`rustup-dist-publish` concurrency group.

### Setup and access

- `PACKAGE_PUBLISH_TOKEN` (repository secret) needs read access to the private
  `YurtOS/yurt-rust` (contents and releases: the tooling checkout and
  `gh release download`) and write access to `YurtOS/yurt-packages` (contents and
  releases: committing manifests, creating releases and uploading assets).
  Its push to `main` is what triggers `pages.yml`; a push made with the default
  `GITHUB_TOKEN` would not.
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
