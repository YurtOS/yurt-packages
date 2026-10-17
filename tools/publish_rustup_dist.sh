#!/usr/bin/env bash
# Publish the rustup dist for one yurt-rust release, one phase per job of the five-job workflow:
# plan -> emit -> publish -> check (the tooling's URL checker, not a phase of this script) -> push.
# Component tarballs become assets of the yurt-packages release rustup-<X.Y.Z>-<date>; the five
# manifest files are committed to the Pages branch.
#
#   publish_rustup_dist.sh plan|emit|publish|push
#
# Inputs come from the environment only. Outputs are appended as key=value lines to $OUTPUT_FILE;
# logs go to stderr. PLAN_* means PLAN_RELEASE_TAG PLAN_VERSION PLAN_REVISION PLAN_DATE
# PLAN_PRODUCER_HOST PLAN_TOOLING_SHA PLAN_TAG, the outputs of plan.
#
#   plan     RUST_RELEASE (rust-vX.Y.Z-A.B.C), PUBLISH_DATE (YYYY-MM-DD, not in the future, default
#            today UTC), PRODUCER_HOST, TOOLING_REF, GH_TOKEN (read token, resolves TOOLING_REF).
#            Outputs release_tag version revision date producer_host tooling_sha tag.
#   emit     PLAN_*, GH_TOKEN (read token), YURT_RUST_TOOLING_DIR (yurt-rust at PLAN_TOOLING_SHA),
#            YURT_SITE_DIR (anonymous Pages checkout), OUT_DIR (empty or absent), WORK_DIR (optional
#            scratch, empty or absent; default a mktemp -d, removed on exit). Writes OUT_DIR/dist/dist.tar
#            and OUT_DIR/components/*.
#   publish  PLAN_*, DIST_DIR, COMPONENTS_DIR, YURT_SITE_DIR, GH_TOKEN (the job's GITHUB_TOKEN),
#            GITHUB_RUN_ATTEMPT=1. Validates both artifacts, creates the release, uploads the
#            components. Outputs digests.
#   push     PLAN_*, DIST_DIR, DIGESTS (publish's digests), YURT_SITE_DIR, PUSH_TOKEN,
#            GITHUB_RUN_ATTEMPT=1. Commits the five files onto a fresh main and pushes.
#
# Only emit runs tooling code (the emitter, without -I: it imports its siblings), and never with a
# token in its environment. publish and push run only this script and rustup_dist_stage.py from
# this script's own directory (trusted/), never anything from the site checkout or an artifact.
set -euo pipefail

PHASE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$HERE/rustup_dist_stage.py"
readonly RUST_REPO=YurtOS/yurt-rust PACKAGES_REPO=YurtOS/yurt-packages PAGES_BRANCH=main

# Tokens move into unexported variables: gh gets the phase's token, git push gets PUSH_TOKEN, and
# nothing else (python, the emitter, git fetch) sees either.
GH_AUTH_TOKEN="${GH_TOKEN:-}"
PUSH_AUTH_TOKEN="${PUSH_TOKEN:-}"
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN PUSH_TOKEN
if [[ "$PHASE" == push ]]; then GH_AUTH_TOKEN=""; else PUSH_AUTH_TOKEN=""; fi
gh_cli() {
  if [[ -n "$GH_AUTH_TOKEN" ]]; then GH_TOKEN="$GH_AUTH_TOKEN" gh "$@"; else gh "$@"; fi
}

# Messages name variables and plan values (validated) only, never a string read from an artifact.
die() { echo "publish_rustup_dist: $*" >&2; exit 1; }

require() {
  local var
  for var in "$@"; do [[ -n "${!var:-}" ]] || die "$var is required"; done
}

empty_or_absent() {
  [[ ! -e "$2" && ! -L "$2" ]] || [[ -d "$2" && ! -L "$2" && -z "$(ls -A "$2")" ]] || die "$1 must be an empty directory or absent"
}

require_first_attempt() {
  [[ "${GITHUB_RUN_ATTEMPT:-}" == 1 ]] || die "GITHUB_RUN_ATTEMPT must be 1: rerunning a single job is refused, dispatch a new run"
}

# The PLAN_* values must be exactly what plan derives from them (same rules, same today).
check_plan() {
  require PLAN_RELEASE_TAG PLAN_VERSION PLAN_REVISION PLAN_DATE PLAN_PRODUCER_HOST PLAN_TOOLING_SHA PLAN_TAG
  local derived expected
  derived="$(mktemp "${TMPDIR:-/tmp}/rustup-plan.XXXXXX")"
  expected="$(mktemp "${TMPDIR:-/tmp}/rustup-plan.XXXXXX")"
  printf '%s\n' "release_tag=$PLAN_RELEASE_TAG" "version=$PLAN_VERSION" "revision=$PLAN_REVISION" "date=$PLAN_DATE" \
    "producer_host=$PLAN_PRODUCER_HOST" "tooling_sha=$PLAN_TOOLING_SHA" "tag=$PLAN_TAG" > "$expected"
  if OUTPUT_FILE="$derived" python3 -I "$STAGE" plan --release "$PLAN_RELEASE_TAG" --date "$PLAN_DATE" \
    --producer-host "$PLAN_PRODUCER_HOST" --tooling-sha "$PLAN_TOOLING_SHA" && cmp -s "$derived" "$expected"; then
    rm -f "$derived" "$expected"
  else
    rm -f "$derived" "$expected"
    die "the PLAN_* values are not the plan derived from PLAN_RELEASE_TAG, PLAN_DATE, PLAN_PRODUCER_HOST and PLAN_TOOLING_SHA"
  fi
  FIVE=("dist/channel-rust-$PLAN_VERSION.toml" "dist/channel-rust-$PLAN_VERSION.toml.sha256"
    "dist/$PLAN_DATE/channel-rust-$PLAN_VERSION.toml" "dist/$PLAN_DATE/channel-rust-$PLAN_VERSION.toml.sha256"
    "dist/$PLAN_DATE/yurt-release-manifest.json")
}

# SITE gets reset and cleaned: it must be a checkout of its own, never a directory inside another repo.
require_site_checkout() {
  [[ -d "$SITE" && "$(git -C "$SITE" rev-parse --show-toplevel 2> /dev/null)" == "$(cd "$SITE" && pwd -P)" ]] ||
    die "YURT_SITE_DIR is not the top directory of a git checkout"
}

reset_site() {
  git -C "$SITE" fetch --quiet origin "$PAGES_BRANCH"
  git -C "$SITE" checkout --quiet -B "$PAGES_BRANCH" "origin/$PAGES_BRANCH"
  git -C "$SITE" reset --quiet --hard "origin/$PAGES_BRANCH"
  git -C "$SITE" clean --quiet -fdx
}

phase_plan() {
  require OUTPUT_FILE
  # Validate everything before the first gh call.
  [[ -n "${RUST_RELEASE:-}" ]] || die "RUST_RELEASE is required"
  [[ "$RUST_RELEASE" =~ ^rust-v[0-9]+\.[0-9]+\.[0-9]+-[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "RUST_RELEASE is not rust-vX.Y.Z-A.B.C"
  local publish_date sha
  publish_date="${PUBLISH_DATE:-$(date -u +%F)}"
  [[ "$publish_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "PUBLISH_DATE is not a real YYYY-MM-DD"
  python3 -I -c 'import datetime,sys; datetime.date.fromisoformat(sys.argv[1])' "$publish_date" 2> /dev/null || die "PUBLISH_DATE is not a real YYYY-MM-DD"
  [[ ! "$publish_date" > "$(date -u +%F)" ]] || die "PUBLISH_DATE must not be in the future"
  case "${PRODUCER_HOST:-}" in
    aarch64-apple-darwin | x86_64-unknown-linux-gnu | aarch64-unknown-linux-gnu) ;;
    *) die "PRODUCER_HOST must be one of the three supported hosts" ;;
  esac
  [[ "${TOOLING_REF:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "${TOOLING_REF:-}" != *..* ]] || die "TOOLING_REF is not a plain ref name or SHA"

  sha="$(gh_cli api "repos/$RUST_REPO/commits/$TOOLING_REF" --jq .sha)" || die "cannot resolve TOOLING_REF in $RUST_REPO"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "TOOLING_REF did not resolve to a 40-hex commit SHA"
  python3 -I "$STAGE" plan --release "$RUST_RELEASE" --date "$publish_date" --producer-host "$PRODUCER_HOST" --tooling-sha "$sha"
}

emit_cleanup() {
  # The emitter wrote into the anonymous checkout; leave it as it was found.
  git -C "$SITE" reset --quiet --hard 2> /dev/null || true
  git -C "$SITE" clean --quiet -fdx 2> /dev/null || true
  [[ -n "${WORK_DIR:-}" ]] || rm -rf "$WORK"
}

phase_emit() {
  check_plan
  require YURT_RUST_TOOLING_DIR YURT_SITE_DIR OUT_DIR
  local tooling="$YURT_RUST_TOOLING_DIR" out="$OUT_DIR" head
  SITE="$YURT_SITE_DIR"
  require_site_checkout
  head="$(git -C "$tooling" rev-parse HEAD)" || die "cannot read the commit of YURT_RUST_TOOLING_DIR"
  [[ "$head" == "$PLAN_TOOLING_SHA" ]] || die "YURT_RUST_TOOLING_DIR is not checked out at PLAN_TOOLING_SHA"
  empty_or_absent OUT_DIR "$out"
  if [[ -n "${WORK_DIR:-}" ]]; then
    WORK="$WORK_DIR"
    empty_or_absent WORK_DIR "$WORK"
    mkdir -p "$WORK"
  else
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/rustup-dist.XXXXXX")"
  fi
  trap emit_cleanup EXIT
  mkdir -p "$WORK/release" "$WORK/dist" "$out/dist"

  # The release's own manifest must name exactly the planned release.
  gh_cli release download "$PLAN_RELEASE_TAG" -R "$RUST_REPO" -D "$WORK/release"
  python3 -I -c '
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        release = json.load(handle)["release"]
    same = [release["tag"], release["rust_version"], release["revision"]] == sys.argv[2:5]
except (OSError, ValueError, KeyError, TypeError):
    same = False
sys.exit(0 if same else 1)' "$WORK/release/release-manifest.json" "$PLAN_RELEASE_TAG" "$PLAN_VERSION" "$PLAN_REVISION" ||
    die "the release manifest's tag, rust_version or revision does not match the plan for $PLAN_RELEASE_TAG"

  python3 "$tooling/scripts/emit_rustup_dist.py" --release-dir "$WORK/release" --site "$SITE" \
    --components "$out/components" --date "$PLAN_DATE" --producer-host "$PLAN_PRODUCER_HOST" \
    --base-url "https://github.com/$PACKAGES_REPO/releases/download/$PLAN_TAG"

  # Pack exactly the five files: explicit names, regular files only, no directory entries.
  python3 -I -c '
import os, stat, sys, tarfile
site, target, names = sys.argv[1], sys.argv[2], sys.argv[3:]
with tarfile.open(target, "x", format=tarfile.PAX_FORMAT) as tar:
    for name in names:
        try:
            fd = os.open(os.path.join(site, name), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            sys.exit(f"publish_rustup_dist: the emitter did not write {name} as a regular file")
        with os.fdopen(fd, "rb") as handle:
            status = os.fstat(fd)
            if not stat.S_ISREG(status.st_mode):
                sys.exit(f"publish_rustup_dist: the emitter did not write {name} as a regular file")
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = status.st_size, 0o644, 0
            tar.addfile(info, handle)' "$SITE" "$WORK/dist/dist.tar" "${FIVE[@]}"
  # Self-check with the validator publish uses (date rules are publish's: the site now holds the output).
  python3 -I "$STAGE" validate-dist --dir "$WORK/dist" --site "$WORK/no-site"
  mv "$WORK/dist/dist.tar" "$out/dist/dist.tar"
  echo "emitted $PLAN_TAG" >&2
}

phase_publish() {
  require_first_attempt
  require OUTPUT_FILE
  check_plan
  require DIST_DIR COMPONENTS_DIR YURT_SITE_DIR
  SITE="$YURT_SITE_DIR"
  require_site_checkout
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/rustup-dist.XXXXXX")"
  trap 'rm -rf "$WORK"' EXIT

  # Hash the components first (the slow part), so that the fresh fetch, the date rules and the
  # release mutation follow each other closely.
  OUTPUT_FILE="$WORK/outputs" python3 -I "$STAGE" validate-components --dist-dir "$DIST_DIR" --dir "$COMPONENTS_DIR"
  # Eligibility on fresh state: a manifest published for this date (or a later one) refuses, so the
  # assets of a published manifest are never deleted or overwritten.
  reset_site
  python3 -I "$STAGE" validate-dist --dir "$DIST_DIR" --site "$SITE"
  # The delete below is safe only because the workflow-level publish-package concurrency group
  # serializes whole runs: no other run can publish a manifest for this tag between the date-rule
  # check above and the delete (tests/test_publish_workflow.py asserts the group).
  if gh_cli release view "$PLAN_TAG" -R "$PACKAGES_REPO" > /dev/null 2>&1; then
    echo "release $PLAN_TAG exists without a published manifest (earlier failed run); recreating it" >&2
    gh_cli release delete "$PLAN_TAG" -R "$PACKAGES_REPO" --yes --cleanup-tag
  fi
  gh_cli release create "$PLAN_TAG" -R "$PACKAGES_REPO" --latest=false --title "Rustup dist $PLAN_VERSION ($PLAN_DATE)" \
    --notes "Component tarballs for rustup channel $PLAN_VERSION, built from yurt-rust release $PLAN_RELEASE_TAG. Tooling: $RUST_REPO@$PLAN_TOOLING_SHA. Producer host: $PLAN_PRODUCER_HOST."
  gh_cli release upload "$PLAN_TAG" -R "$PACKAGES_REPO" "$COMPONENTS_DIR"/*
  grep '^digests=' "$WORK/outputs" >> "$OUTPUT_FILE"
  echo "created release $PLAN_TAG" >&2
}

site_git() {
  git -C "$SITE" -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" "$@"
}

# The checkout stores no credentials; the token reaches git only through the push's environment
# (not argv, not .git/config). Fetches are anonymous: the Pages branch lives in a public repo.
push_site() {
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="http.https://github.com/.extraheader" \
    GIT_CONFIG_VALUE_0="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$PUSH_AUTH_TOKEN" | base64 | tr -d '\n')" \
    git -C "$SITE" push --quiet origin "HEAD:$PAGES_BRANCH"
}

stage_five() {
  local expected staged modes
  git -C "$SITE" add -A
  expected="$(printf '%s\n' "${FIVE[@]}" | LC_ALL=C sort)"
  staged="$(git -C "$SITE" diff --cached --name-only --no-renames | LC_ALL=C sort)"
  [[ "$staged" == "$expected" ]] || die "the staged change is not exactly the five dist paths"
  modes="$(git -C "$SITE" diff --cached --raw --no-renames | awk '{print $2}' | sort -u)"
  [[ "$modes" == 100644 ]] || die "the five dist paths are not all staged as mode 100644"
}

phase_push() {
  require_first_attempt
  check_plan
  require DIST_DIR YURT_SITE_DIR
  [[ -n "${DIGESTS:-}" ]] || die "DIGESTS is required: the digests output of publish"
  [[ -n "$PUSH_AUTH_TOKEN" ]] || die "PUSH_TOKEN is required"
  SITE="$YURT_SITE_DIR"
  require_site_checkout
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/rustup-dist.XXXXXX")"
  trap 'reset_site || true; rm -rf "$WORK"' EXIT

  # The artifact must be the one publish validated, before anything is staged.
  python3 -I "$STAGE" validate-dist --dir "$DIST_DIR" --site "$WORK/no-site"
  # Never rebase onto a newer main: every attempt starts from a fresh main and rechecks the date
  # rules there (apply), so a publication that landed meanwhile fails closed.
  local attempt
  for attempt in 1 2 3; do
    reset_site
    python3 -I "$STAGE" apply --dist-dir "$DIST_DIR" --site "$SITE" >&2
    stage_five
    site_git commit --quiet -m "chore: publish rustup dist $PLAN_VERSION $PLAN_DATE"
    if push_site; then
      echo "published $PLAN_TAG" >&2
      return 0
    fi
    [[ "$attempt" -lt 3 ]] || die "could not push to $PAGES_BRANCH after $attempt attempts"
    echo "push to $PAGES_BRANCH rejected; resetting to origin/$PAGES_BRANCH and checking again" >&2
  done
}

case "$PHASE" in
  plan) phase_plan ;;
  emit) phase_emit ;;
  publish) phase_publish ;;
  push) phase_push ;;
  *)
    echo "usage: publish_rustup_dist.sh plan|emit|publish|push (inputs in the environment)" >&2
    exit 2
    ;;
esac
