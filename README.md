# yurt-packages

Signed Yurt package repository metadata and release artifacts.

This repository is the package repository consumed by `pkg update`.
Generated files must match the `yurt-pkg` client contract:

```text
index.json
index.json.bundle
packages/<name>.json
artifacts/<name>/<version>/<name>-<version>-<build>.yurtpkg
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
`index.json`, and writes a placeholder `index.json.bundle` so
`pkg --features test-fixtures update` can exercise the same repository layout.
Real Sigstore bundle generation remains CI-owned.
