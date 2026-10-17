# Isolate the publish token from `tooling_ref` code (issue #13)

## Problem

`publish-rustup-dist.yml` runs the emitter and URL checker, which come from a
caller-chosen `tooling_ref` of the private `YurtOS/yurt-rust`, in the same job
as `tools/publish_rustup_dist.sh`, which holds the write token. Review of #12
reproduced two ways for that code to reach the token: rewriting the running
publish script (bash reads a script incrementally) and planting git config or
hooks in the site checkout before the push. A third (`/proc/$PPID/environ`) was
not reproduced. Steps of one job share a VM, a user and a filesystem, so no
amount of care inside one job closes this.

## Contract

- Code from `tooling_ref` (emitter, checker) never runs in a job that holds a
  write credential, and never runs in a job whose filesystem a write-credential
  job later trusts without validation.
- The write token can touch only `YurtOS/yurt-packages`. The credential used to
  read the private `YurtOS/yurt-rust` is read-only.
- Not in the contract: content integrity of what `tooling_ref` emits. Whoever
  dispatches chooses the ref and is trusted to the extent of the emitted dist
  content. The fix limits what a bad ref can do to *the dist namespace*, and
  keeps it away from the token and from everything else in the repo.

## Severity

Major in principle (credential theft by a chosen ref), low likelihood today
(dispatch needs write access; the tooling comes from the org's private repo).
The user asked for the redesign.

## Design

Four jobs, each on a fresh VM, glued by one workflow artifact. The scripts and
workflow always come from `main` of this repo (`if: github.ref == refs/heads/main`),
never from `tooling_ref`.

| Job | Runs | Credentials |
|---|---|---|
| `emit` | validates inputs; downloads the yurt-rust release; runs the emitter from `tooling_ref` against an anonymous checkout of the current `dist/`; uploads artifact `rustup-stage` | `YURT_RUST_READ_TOKEN` (Contents: read on yurt-rust only) |
| `publish` | downloads the artifact, **validates it as data**, checks eligibility on fresh state, creates the release `rustup-<ver>-<date>` and uploads the component tarballs | `RUSTUP_DIST_PUBLISH_TOKEN` (Contents: read/write on yurt-packages only) |
| `check` | checks out the tooling (read token), runs the URL checker against the artifact's `dist/` and the now-public asset URLs | `YURT_RUST_READ_TOKEN` only |
| `push` | downloads the artifact, **validates it again**, copies only the allowed paths into a fresh `main` checkout, commits, pushes with a bounded rebase-and-retry | `RUSTUP_DIST_PUBLISH_TOKEN` |

`publish` and `push` run only code from `main`. They treat the artifact as
untrusted data and never execute anything from it. `emit` and `check` hold only
the read-only token, which can read the same private repo the tooling came from.

### Artifact (`rustup-stage`)

```
meta.json            version, revision, tag, date, producer_host, tooling_sha
components/*.tar.gz  component tarballs (+ their .sha256 sidecars if the emitter writes them)
dist/                the emitter's output tree (moving manifest + sidecar, dated dir)
```

### Validation in `publish` and `push` (shared function in the script)

- `meta.json` fields match the same regexes the script already applies to the
  inputs; the tag is recomputed from them, not trusted.
- `components/` contains only regular files named
  `<rustc|cargo|rust-std>-<version>-<target>.tar.gz` (and sidecars) for the
  declared version; no symlinks, no subdirectories.
- Against a fresh `origin/main`, the artifact's `dist/` may add or replace only:
  `dist/channel-rust-<version>.toml`, its `.sha256`, and files under the new
  `dist/<date>/` directory, which must not already exist. Every other path
  (including any existing dated directory) differing from `main` rejects the
  artifact. Checked by diffing the tree, not by trusting the emitter.
- The token never enters a process that reads untrusted *code*; it does parse
  untrusted JSON, with `python3 -I`.

### What does not change

The script's input validation, the release naming and retry rules (an existing
release with no published manifest is recreated; a published manifest refuses),
`--latest=false`, the future-date rule, the bounded push retry, and the shared
`publish-package` concurrency group (set on the workflow, so all four jobs
serialize with the other publisher).

### Tokens (created by the user)

- `YURT_RUST_READ_TOKEN`: fine-grained PAT, `YurtOS/yurt-rust` only, Contents: read.
- `RUSTUP_DIST_PUBLISH_TOKEN`: fine-grained PAT, `YurtOS/yurt-packages` only,
  Contents: read/write. A PAT, not `GITHUB_TOKEN`, because a `GITHUB_TOKEN`
  push does not trigger `pages.yml`.
- `PACKAGE_PUBLISH_TOKEN` is no longer used by the rustup workflow and is
  untouched: `publish-package.yml` still uses it.

## Smallest viable fix and alternatives

| Option | Size | Meets the contract? |
|---|---|---|
| Separate *steps* in one job, token only in the last | ~40 lines | No: same VM and user; the tooling can leave a background process or rewrite files later steps run. |
| Stage `dist/` outside the checkout, copy the script out | ~30 lines | No: closes only two of the routes. |
| **Four jobs, artifact validated as data (chosen)** | ~150–250 production lines + tests | Yes. |
| Stand-alone service / GitHub App minting tokens | far more | Overkill. |

The chosen option is more than 3x the smallest; the smaller options fail the
contract for the reason in the right-hand column.

## Tests

The script becomes phase subcommands (`emit`, `publish`, `push`; `check` is a
direct call to the tooling checker). The test harness runs the phases in
separate temp directories and processes, as separate jobs would, and asserts:

- no write token in the environment or filesystem of `emit` and `check`;
- the read-only token is absent from `publish` and `push`;
- the existing behaviours (all 17 current tests) still hold across the phases;
- artifact validation rejects: path traversal and absolute paths, symlinks,
  stray files, a changed existing dated manifest, a `dist/<date>` that already
  exists on `main`, a component with a wrong name or version, a tag/date mismatch
  with `meta.json`.

## Risks

- **Unrun end to end.** Hosted publication is not exercised; this change is
  tested with shims and a local bare remote like #12. First real dispatch needs
  the user's go and consumes the new secrets.
- Fine-grained PAT approval: the org may need to approve both tokens.
- `check` runs tooling code with the read token. Acceptable: the token can only
  read the repo the tooling came from.
- Artifact upload/download between jobs adds GitHub-hosted artifact storage use
  (components are ~1 GB for the newest release; artifacts count toward the
  retention quota). Use a short retention (1 day).

## Non-goals

Integrity of emitted content beyond the path allowlist; branch protection on
`main`; GitHub App or OIDC; the in-Yurt rustup port (#68).
