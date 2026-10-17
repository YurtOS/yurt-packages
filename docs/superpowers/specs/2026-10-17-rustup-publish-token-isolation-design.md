# Isolate the publish credentials from `tooling_ref` code (issue #13)

## Problem

`publish-rustup-dist.yml` runs the emitter and URL checker, which come from a
caller-chosen `tooling_ref` of the private `YurtOS/yurt-rust`, in the same job
as `tools/publish_rustup_dist.sh`, which holds the write token. Review of #12
reproduced two ways for that code to reach the token: rewriting the running
publish script (bash reads a script incrementally) and planting git config or
hooks in the site checkout before the push. Steps of one job share a VM, a user
and a filesystem, so no care inside one job closes this.

## Contract

- Code from `tooling_ref` (emitter, checker) never runs in a job that holds a
  write credential, and nothing a write-credential job executes or trusts comes
  from it without validation as data.
- The only long-lived write credential, `RUSTUP_DIST_PUBLISH_TOKEN`, can touch
  only `YurtOS/yurt-packages` and is reachable by exactly one job (`push`),
  from an environment restricted to `main`. The credential that reads the
  private `YurtOS/yurt-rust` is read-only.
- Not in the contract: integrity of the *content* the emitter produces. Whoever
  dispatches chooses the ref and is trusted for the dist content (the manifests
  and tarballs are only as good as the tooling). The fix confines a bad ref to
  that content, inside a fixed set of paths, and keeps it away from the
  credentials and from the rest of the repo. Repository workflow authors remain
  trusted: anyone who can edit workflows on `main` can do anything.

## Severity

Major in principle (credential theft by a chosen ref); low likelihood today
(dispatch needs write access and the tooling comes from the org's private
repo). Requested by the user as a redesign.

## Design

Five jobs on fresh VMs, in the chain `plan → emit → publish → check → push`,
each with `needs:` on the previous one and ordinary success gating. Scripts and
workflow come from `main` (`if: github.ref == 'refs/heads/main'`; that guard is
convenience only, the environment restriction below is the boundary). Workflow
level `permissions: {}`; each job declares what it needs. No `pull_request`
trigger. No caches in any job that holds a write credential.

| Job | Runs | Credentials |
|---|---|---|
| `plan` | trusted code only. Validates the inputs, resolves an empty date to today (UTC) **once**, derives `tag`, `version`, `revision`, `release_tag`, `date`, `producer_host`, resolves `tooling_ref` to a commit SHA (`gh api`). Outputs all of these. | `YURT_RUST_READ_TOKEN` (for the SHA lookup only) |
| `emit` | checks out `tooling` at the planned SHA; downloads the yurt-rust release; confirms its manifest version/revision equal the plan; runs the emitter against an anonymous checkout of `dist/`; uploads two artifacts | `YURT_RUST_READ_TOKEN` |
| `publish` | trusted scripts at `github.sha` in an isolated directory; downloads the artifacts by id into an isolated staging dir, **validates them as data**, rechecks eligibility on fresh `main`, creates the release `rustup-<ver>-<date>` and uploads the tarballs; outputs the sha256 of every validated file | job-scoped `GITHUB_TOKEN` (`contents: write`) |
| `check` | checks out `tooling` at the **planned** SHA, runs the URL checker against the `rustup-dist` artifact and the public asset URLs; never uploads | `YURT_RUST_READ_TOKEN` |
| `push` | trusted scripts in the isolated directory; downloads the small artifact by id, verifies the sha256s from `publish`, rebuilds the change on a fresh `main` checkout, commits and pushes with a bounded retry | `RUSTUP_DIST_PUBLISH_TOKEN` (environment `rustup-publish`, branch `main` only) |

The trusted scripts are checked out to a directory that is never the target of
the push's checkout, rebase or reset (`$RUNNER_TEMP/trusted`), so a rebase onto
a changed `main` cannot rewrite the script that is running.

### Artifacts (two, so `check` and `push` never fetch the ~1 GB of tarballs)

- `rustup-dist`, small. Exactly these paths, nothing else:
  `dist/channel-rust-<ver>.toml`, `dist/channel-rust-<ver>.toml.sha256`,
  `dist/<date>/channel-rust-<ver>.toml`, `dist/<date>/channel-rust-<ver>.toml.sha256`,
  `dist/<date>/yurt-release-manifest.json`.
- `rustup-components`, large, compression level 0 (the tarballs are already
  compressed). Only files named `<rustc|cargo|rust-std>-<ver>-<target>.tar.gz`
  for the planned version. The emitter writes no component sidecars.

No `meta.json`: every identity value (version, revision, date, tag, producer
host, tooling SHA) comes from `plan` outputs, never from the artifact.

### Validation (one shared function, used by `publish` and `push`)

The artifact is extracted into an empty staging directory outside every trusted
checkout; `download-artifact` is pinned by commit SHA and addressed by artifact
id (an output of `emit`). Then:

- each expected path exists as a **regular file** (`lstat`, not through links),
  within a size cap, and no other path exists anywhere in the tree (links,
  devices, directories beyond the expected ones, duplicate normalized or
  absolute paths, are all rejected);
- component names match the planned version exactly; tarballs are never
  unpacked in a privileged job;
- the moving manifest and its sidecar are byte-identical to the dated copies;
- the dated manifest's channel version equals the planned version; the sidecars
  match the manifests;
- the provenance JSON parses with `python3 -I`; the trusted script sets its
  `tooling_sha` from the `plan` output (the emitter's value is overwritten);
- date rules, enforced by trusted code, not left to the emitter: `dist/<date>/`
  does not exist on fresh `main`, and `<date>` is strictly later than the newest
  existing dated directory (any version); the date is a real calendar date,
  not in the future; the producer host is one of the three.

`push` does not rebase the artifact's tree onto `main`. For every attempt it
fetches `main`, resets a fresh checkout to it, **rechecks the date rules**,
copies in exactly the validated five files (mode 100644, nothing followed),
asserts the staged change is exactly those paths, commits and pushes; on a
rejected push it repeats (bounded, three attempts). A newer publication that
lands between phases therefore fails closed instead of being merged over.
After `publish`, `push` also verifies the sha256 of each file against the
digests `publish` validated, so nothing between the two jobs can swap content.

### Failure and rerun policy

- After any failure the policy is a **new full dispatch**. `publish` and `push`
  fail closed unless `github.run_attempt == 1`, so rerunning a single job (which
  could skip eligibility, validation or the URL check) is refused.
- State left by failures: an upload failure leaves a partial release; a `check`
  failure leaves uploaded assets; a `push` failure leaves assets without
  manifests. All are handled by the existing rule on the next dispatch with the
  same or a later date: a release with no published manifest is recreated; once
  a manifest is published, its assets are never deleted or overwritten.
- Ambiguous push success (push timed out but landed) is resolved by the next
  dispatch seeing the published manifest and refusing; the operator checks
  `main`.
- Artifacts have one-day retention; an expired artifact only matters to a
  rerun, which is refused anyway.
- Concurrency: the workflow-level `publish-package` group is kept, so all five
  jobs serialize with the other publisher. A run now holds the group longer,
  which can displace a pending `Publish Package` run (GitHub keeps only one
  pending run per group); accepted, rerun that workflow if it happens.

### Tokens

- `YURT_RUST_READ_TOKEN`: fine-grained PAT, `YurtOS/yurt-rust` only, Contents:
  read. Repo secret. Used by `plan`, `emit`, `check` and nothing that holds a
  write credential.
- `RUSTUP_DIST_PUBLISH_TOKEN`: fine-grained PAT, `YurtOS/yurt-packages` only,
  Contents: read/write, no Workflows permission. Stored as a secret of the
  `rustup-publish` environment (deployment branches: `main` only), **not** a
  repo secret, so a workflow on another branch cannot read it. Referenced only
  by `push`. A PAT rather than `GITHUB_TOKEN` because a `GITHUB_TOKEN` push does
  not trigger `pages.yml`.
- Release creation and asset upload in `publish` use a job-scoped
  `GITHUB_TOKEN` with `contents: write`; it expires with the job.
- `PACKAGE_PUBLISH_TOKEN` is untouched (`publish-package.yml` uses it).
- The README's token and Pages sections are updated.

## Smallest viable fix and alternatives

| Option | Size | Meets the contract? |
|---|---|---|
| Separate *steps* in one job, token only in the last | ~40 lines | No: same VM and user. |
| Stage `dist/` outside the checkout, copy the script out | ~30 lines | No: closes two routes of several. |
| All publishing under `GITHUB_TOKEN`, no PAT | ~smaller | No: its push does not trigger Pages (could be made to via `workflow_call` into `pages.yml`; a larger change to a working workflow, deferred). |
| **Five jobs, validated artifacts (chosen)** | ~200 script lines + ~120 workflow lines + tests, added | Yes. |
| GitHub App / OIDC | far more | Overkill. |

The chosen option is more than 3x the smallest; the smaller ones fail the
contract for the reason in the right-hand column. Estimates are provisional.

## Tests

The script becomes phase subcommands (`plan`, `emit`, `publish`, `push`; `check`
is a direct call to the tooling checker). Red first, then green:

- **Static workflow test** (parses `publish-rustup-dist.yml`): job chain and
  `needs`; which job references which secret (`RUSTUP_DIST_PUBLISH_TOKEN` only
  in `push`, the read token never in `publish`/`push`); `push` declares
  environment `rustup-publish`; per-job `permissions` minimal; no
  `pull_request` trigger; `persist-credentials: false` on every checkout; the
  trusted scripts directory is distinct from the site checkout; `download-artifact`
  and other actions pinned; no cache step in `publish`/`push`.
- **Phase harness**: the phases run in separate temp dirs and processes. All 17
  existing behaviours are carried over (adapted per phase; list in the plan).
- **Hostile-artifact fixtures** for `validate`: path traversal, absolute and
  duplicate normalized paths, symlinks and special files, extra files, wrong
  component names or version, mismatched moving/dated manifests, an existing
  `dist/<date>`, an older-than-newest date, provenance not JSON. Each is rejected
  before any release mutation or push.
- **Between-phase races**: a same-date or newer-date publication lands after
  `publish` and before `push`; `push` fails closed. A changed script on `main`
  during `push` does not alter the running script.
- **Only a hosted run can prove**: secret exposure per job, environment branch
  gating, artifact transfer by id, `run_attempt` refusal, real Pages trigger.
  The first dispatch needs the user's go.

## Risks

- Unrun end to end, like #12; tested with shims and a local bare remote.
- Org may need to approve the two PATs; the user must create the
  `rustup-publish` environment and move `RUSTUP_DIST_PUBLISH_TOKEN` into it.
- `check` runs tooling code with the read-only token: acceptable, it can only
  read the repo the tooling came from.
- Transfer: the ~1 GB components move once, from `emit` to `publish`; storage
  is covered by one-day retention. The README's 1 GB figure is for the
  acceptance workflow, not this artifact.

## Non-goals

Integrity of emitted content beyond the path/shape allowlist; branch protection
on `main`; GitHub App or OIDC; routing the Pages deploy through `workflow_call`;
the in-Yurt rustup port (#68).
