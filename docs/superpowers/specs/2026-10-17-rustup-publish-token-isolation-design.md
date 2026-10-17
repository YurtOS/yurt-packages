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
- The long-lived write credential this workflow uses, `RUSTUP_DIST_PUBLISH_TOKEN`,
  can touch only `YurtOS/yurt-packages` and is reachable by exactly one job
  (`push`), from an environment restricted to `main`. The credential that reads
  the private `YurtOS/yurt-rust` is read-only.
- The contract covers `RUSTUP_DIST_PUBLISH_TOKEN` only. It does **not** cover
  `PACKAGE_PUBLISH_TOKEN`, the repository secret that `publish-package.yml`
  uses. Tooling code in `emit`/`check` can write `main`-scoped Actions cache
  entries, and `publish-package.yml` restores such a cache (`Swatinem/rust-cache`)
  in the job that holds that token. This is not a regression (before this change
  the tooling held the token directly). A follow-up issue removes that path.
- Not in the contract: integrity of the *content* the emitter produces. Whoever
  dispatches chooses the ref and is trusted for the dist content (the manifests
  and tarballs are only as good as the tooling). The fix confines a bad ref to
  that content, inside a fixed set of paths, and keeps it away from
  `RUSTUP_DIST_PUBLISH_TOKEN` and the job-scoped write `GITHUB_TOKEN` (but not
  from `PACKAGE_PUBLISH_TOKEN`, see above). Repository workflow authors remain
  trusted: anyone who can edit workflows on `main` can do anything.

## Severity

Major in principle (credential theft by a chosen ref); low likelihood today
(dispatch needs write access and the tooling comes from the org's private
repo). Requested by the user as a redesign.

## Design

Five jobs on fresh VMs, in the order `plan → emit → publish → check → push`.
`needs` exposes only direct dependencies, so each job lists every upstream job
whose outputs it reads, and ordinary success gating applies:
`emit: [plan]`, `publish: [plan, emit]`, `check: [plan, emit, publish]`,
`push: [plan, emit, publish, check]`. Data flows by trust level:

- identity (version, revision, date, tag, producer host, tooling SHA) comes
  **only from `plan`** outputs, never relayed through `emit` or `check`;
- artifact ids come from `emit`, which runs tooling code and so is **untrusted**:
  they must be all digits, and artifacts are downloaded without `run-id` or
  `github-token`, so only artifacts of the current run resolve;
- content digests come from `publish`, which runs only trusted code.

Every `needs.*.outputs` value and every input reaches a script through `env:`,
never interpolated inside `run:`. Scripts and workflow come from `main`
(`if: github.ref == 'refs/heads/main'`; that guard is convenience only, the
environment restriction below is the boundary). Workflow-level
`permissions: {}`; each job declares what it needs. No `pull_request` trigger.
No caches in any job that holds a write credential.

| Job | Runs | Credentials |
|---|---|---|
| `plan` | trusted code only. Validates the inputs, resolves an empty date to today (UTC) **once**, derives `tag`, `version`, `revision`, `release_tag`, `date`, `producer_host`, resolves `tooling_ref` to a commit SHA (`gh api`). Outputs all of these. | `YURT_RUST_READ_TOKEN` (for the SHA lookup only) |
| `emit` | checks out `tooling` at the planned SHA; downloads the yurt-rust release; confirms its manifest version/revision equal the plan; runs the emitter against an anonymous checkout of `dist/`; uploads two artifacts | `YURT_RUST_READ_TOKEN` |
| `publish` | trusted scripts at `github.sha` in an isolated directory; downloads the artifacts by id into an isolated staging dir, **validates them as data**, rechecks eligibility on fresh `main`, creates the release `rustup-<ver>-<date>` and uploads the tarballs; outputs the sha256 of every validated file | job-scoped `GITHUB_TOKEN` (`contents: write`) |
| `check` | checks out `tooling` at the **planned** SHA, runs the URL checker against the `rustup-dist` artifact and the public asset URLs; anything it uploads is never consumed | `YURT_RUST_READ_TOKEN` |
| `push` | trusted scripts in the isolated directory; downloads the small artifact by id, verifies the sha256s from `publish`, rebuilds the change on a fresh `main` checkout, commits and pushes with a bounded retry | `RUSTUP_DIST_PUBLISH_TOKEN` (environment `rustup-publish`, branch `main` only) |

The trusted scripts are checked out at `github.sha` into `trusted/`, a sibling
of `site/` under `$GITHUB_WORKSPACE` (`actions/checkout` cannot write outside
the workspace). Only `site/` is ever fetched, reset or committed; `trusted/` is
never touched, so a reset to a changed `main` cannot rewrite the script that is
running.

### Artifacts (two, so `check` and `push` never fetch the ~1 GB of tarballs)

- `rustup-dist`, small: **one file**, `dist.tar`, an uncompressed tar of exactly
  these five paths (these are what `scripts/emit_rustup_dist.py` writes on
  success; it deletes nothing):
  `dist/channel-rust-<ver>.toml`, `dist/channel-rust-<ver>.toml.sha256`,
  `dist/<date>/channel-rust-<ver>.toml`, `dist/<date>/channel-rust-<ver>.toml.sha256`,
  `dist/<date>/yurt-release-manifest.json`.
  Shipping a tar means the entries are inspected by our own code **before
  anything is extracted** (see Validation); the artifact zip layer must yield
  exactly that one regular file.
- `rustup-components`, large, compression level 0 (the tarballs are already
  compressed). The emitter writes `rustc`, `cargo` and `rust-std` tarballs for
  each host of the release plus `rust-std-<ver>-wasm32-yurt-linux-musl` for the
  producer host, and no component sidecars. Their content is bound to the
  manifest by hash (Validation), not trusted by name.

No `meta.json`: every identity value comes from `plan` outputs. A job named
`check` may upload nothing the pipeline consumes; its uploads, if any, are
never read.

### Validation (one shared function, used by `publish` and `push`)

`publish` runs the container and content checks on both artifacts. `push`
downloads only `rustup-dist` and runs the dist checks; it is bound to the
tarballs indirectly: `publish`'s digest pins `dist.tar` (as a whole, plus each
of its five members), and the manifest inside pins the component hashes that
`publish` verified. Each artifact id is downloaded into its own empty staging
directory (one id per step), so the "exactly the expected files" check does
not depend on download-artifact's folder layout. The dated manifest's version
string is compared against the exact value the emitter writes,
`"<X.Y.Z> (yurt <A.B.C>)"`, built from the plan's version and revision (which
also binds the revision); a bare `X.Y.Z` comparison would reject valid
manifests.

`download-artifact` is pinned by commit SHA and addressed by artifact id into
an empty staging directory outside `trusted/` and `site/`. Two layers:

1. **Container.** The staging directory must contain exactly the expected
   top-level regular file(s) (`dist.tar`; the named component files) and
   nothing else (`lstat`, no links, devices or directories). `dist.tar` is read
   with Python's `tarfile` **without extracting**: every member name, type,
   count and size is checked first (exactly the five expected paths, regular
   files, no duplicates, no absolute or `..` paths, a size cap per member and in
   total); only then are those members extracted, one by one, with containment
   checks. A zip layer that collapses duplicate component names is harmless:
   the final tree is validated and every component is bound by hash below.
2. **Content, bound to the plan** (all identity comes from `plan`, nothing is
   trusted because the artifact says so):
   - the moving manifest and its sidecar are byte-identical to the dated copies;
     the sidecars match the manifests;
   - the dated manifest is parsed with `tomllib` under `python3 -I`: its date
     and channel version equal the plan; the set of component files in
     `rustup-components` is **exactly** the basenames of the manifest's URLs,
     the URLs sit under the planned release tag's download prefix, the targets
     are known, and each tarball's sha256 equals the manifest's hash (missing,
     extra or unknown components, or swapped bytes, are rejected). Tarballs are
     never unpacked in a privileged job;
   - the provenance JSON parses with `python3 -I`, and its identity fields
     (release tag, rust version, revision, date, producer host) equal the plan
     values (the exact key set is pinned by the emitter, see the plan);
     `publish` outputs the sha256 of every file **as downloaded**; `push`
     verifies those, then rewrites `tooling_sha` from the `plan` output
     deterministically before staging (the emitter's value is overwritten);
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
`push` verifies the digests `publish` output before it stages anything, so
nothing between the two jobs can swap content.

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
  `trusted/` and `site/` are distinct checkout paths under the workspace;
  `download-artifact` and other actions pinned, with no `run-id`/`github-token`;
  no cache step in `publish`/`push`; the exact `needs` lists above; every
  `needs.*.outputs`/input reference sits in an `env:` block, never inside `run:`.
- **Phase harness**: the phases run in separate temp dirs and processes. All 17
  existing behaviours are carried over (adapted per phase; list in the plan).
- **Hostile-artifact fixtures** for `validate`: path traversal, absolute and
  duplicate normalized paths, symlinks and special files, extra files, wrong
  component names or version, mismatched moving/dated manifests, an existing
  `dist/<date>`, an older-than-newest date, provenance not JSON; valid
  JSON/TOML carrying the **wrong identity** (tag, version, revision, date,
  producer host); missing, extra or unknown-target components; tarball bytes
  not matching the manifest hash; tar members that are duplicated, absolute,
  `..`, links or oversized, fed through the real container checks. Each is
  rejected before any release mutation or push. `push` additionally has: a
  successful fixture with **no** component directory present, and rejection
  fixtures for a missing digest and a mismatched digest from `publish`.
- **Between-phase races**: a same-date or newer-date publication lands after
  `publish` and before `push`; `push` fails closed. A changed script on `main`
  during `push` does not alter the running script.
- **Only a hosted run can prove**: secret exposure per job, environment branch
  gating, artifact transfer by id, `run_attempt` refusal, real Pages trigger.
  The first dispatch needs the user's go.

## Risks

- Unrun end to end, like #12; tested with shims and a local bare remote.
- Org may need to approve the two PATs; the user must create the
  `rustup-publish` environment (deployment branches: `main` only, no tag
  rules), move `RUSTUP_DIST_PUBLISH_TOKEN` into it, and delete the repo-level
  secret of the same name, because a workflow on any branch can read a
  repository secret (the environment secret would take precedence, but the
  repository copy stays exposed). Until that setup is done, a dispatch would
  auto-create `rustup-publish` with no branch rule and `push` would read the
  repository secret; as of 2026-10-17 the environment does not exist, the
  secret is a repository secret and Pages is not enabled (a pre-dispatch
  blocker, see the README checklist).
- Cross-workflow cache: tooling code can poison the `main`-scoped Actions cache
  that `publish-package.yml` restores in the job holding `PACKAGE_PUBLISH_TOKEN`
  (see Contract). Accepted for this change; a follow-up issue removes it.
- First-dispatch checklist: the repository or organization Actions settings may
  cap `GITHUB_TOKEN`, which would make `publish` fail; verify at first dispatch;
  `YURT_RUST_READ_TOKEN` stays readable from any branch (accepted: it is
  read-only on a repo whose tooling the dispatcher already chooses);
  `publish` holds a short-lived write `GITHUB_TOKEN` but runs only trusted code;
  `plan` validates the SHA it resolves as 40-hex.
- `check` runs tooling code with the read-only token: acceptable, it can only
  read the repo the tooling came from.
- Transfer: the ~1 GB components move once, from `emit` to `publish`; storage
  is covered by one-day retention. The README's 1 GB figure is for the
  acceptance workflow, not this artifact.

## Non-goals

Integrity of emitted content beyond the path/shape allowlist; branch protection
on `main`; GitHub App or OIDC; routing the Pages deploy through `workflow_call`;
the in-Yurt rustup port (#68).
