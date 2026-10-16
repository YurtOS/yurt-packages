#!/usr/bin/env bash
# Publish the rustup dist for one yurt-rust release: component tarballs become assets of the
# yurt-packages release rustup-<X.Y.Z>-<date>, the manifest tree is committed to the Pages branch.
#
# Inputs (environment only):
#   RUST_RELEASE           yurt-rust release tag, rust-vX.Y.Z-A.B.C
#   TOOLING_REF            yurt-rust ref holding the emitter and checker (checked out by the caller)
#   PUBLISH_DATE           YYYY-MM-DD (default: today, UTC)
#   PRODUCER_HOST          host whose archive supplies the guest std
#   YURT_RUST_TOOLING_DIR  checkout of yurt-rust at TOOLING_REF
#   YURT_SITE_DIR          checkout of this repository's Pages branch (origin must be pushable)
#   PAGES_BRANCH           branch served as Pages (default: main)
#   WORK_DIR               scratch directory, must be empty or absent (default: a mktemp -d, removed on exit)
set -euo pipefail

# The token is for gh only; the emitter, checker and python run from a caller-chosen ref and never see it.
GH_AUTH_TOKEN="${GH_TOKEN:-}"
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN GITHUB_ENTERPRISE_TOKEN
gh_cli() {
  if [[ -n "$GH_AUTH_TOKEN" ]]; then GH_TOKEN="$GH_AUTH_TOKEN" gh "$@"; else gh "$@"; fi
}

RUST_REPO="${RUST_REPO:-YurtOS/yurt-rust}"
PACKAGES_REPO="${PACKAGES_REPO:-YurtOS/yurt-packages}"
PAGES_BRANCH="${PAGES_BRANCH:-main}"

die() { echo "publish_rustup_dist: $*" >&2; exit 1; }

# 1. Validate everything before the first network or gh call.
[[ -n "${RUST_RELEASE:-}" ]] || die "RUST_RELEASE is required"
[[ "$RUST_RELEASE" =~ ^rust-v[0-9]+\.[0-9]+\.[0-9]+-[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "RUST_RELEASE is not rust-vX.Y.Z-A.B.C"
DATE="${PUBLISH_DATE:-$(date -u +%F)}"
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "PUBLISH_DATE is not a real YYYY-MM-DD"
python3 -I -c 'import datetime,sys; datetime.date.fromisoformat(sys.argv[1])' "$DATE" 2>/dev/null || die "PUBLISH_DATE is not a real YYYY-MM-DD"
case "${PRODUCER_HOST:-}" in
  aarch64-apple-darwin | x86_64-unknown-linux-gnu | aarch64-unknown-linux-gnu) ;;
  *) die "PRODUCER_HOST must be one of the three supported hosts" ;;
esac
[[ "${TOOLING_REF:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ && "${TOOLING_REF:-}" != *..* ]] || die "TOOLING_REF is not a plain ref name or SHA"
TOOLING_DIR="${YURT_RUST_TOOLING_DIR:?YURT_RUST_TOOLING_DIR is required}"
SITE="${YURT_SITE_DIR:?YURT_SITE_DIR is required}"
if [[ -n "${WORK_DIR:-}" ]]; then
  WORK="$WORK_DIR"
  [[ ! -e "$WORK" || -z "$(ls -A "$WORK" 2> /dev/null)" ]] || die "WORK_DIR must be empty or absent"
  mkdir -p "$WORK"
else
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/rustup-dist.XXXXXX")"
  trap 'rm -rf "$WORK"' EXIT
fi

TOOLING_SHA="$(git -C "$TOOLING_DIR" rev-parse HEAD)"
[[ "$TOOLING_SHA" =~ ^[0-9a-f]{40,64}$ ]] || die "cannot resolve the tooling checkout SHA"

reset_site() {
  git -C "$SITE" fetch --quiet origin "$PAGES_BRANCH"
  git -C "$SITE" checkout --quiet -B "$PAGES_BRANCH" "origin/$PAGES_BRANCH"
  git -C "$SITE" clean --quiet -fdx
}
trap 'reset_site || true; [[ -n "${WORK_DIR:-}" ]] || rm -rf "$WORK"' EXIT

# 2. Fetch the complete release; the version comes from its manifest.
mkdir -p "$WORK/release" "$WORK/components"
gh_cli release download "$RUST_RELEASE" -R "$RUST_REPO" -D "$WORK/release"
{
  read -r VERSION
  read -r REVISION
} < <(python3 -I -c 'import json,sys; r = json.load(open(sys.argv[1]))["release"]; print(r["rust_version"]); print(r["revision"])' "$WORK/release/release-manifest.json")
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "release manifest has no X.Y.Z rust_version"
[[ "$RUST_RELEASE" == "rust-v$VERSION-$REVISION" ]] || die "release manifest version/revision $VERSION-$REVISION does not match $RUST_RELEASE"
TAG="rustup-$VERSION-$DATE"

# 3. Eligibility on fresh state: refresh the Pages checkout, then decide. A manifest published
# for this date is immutable, so its release assets are never deleted or overwritten.
reset_site
if [[ -e "$SITE/dist/$DATE/channel-rust-$VERSION.toml" ]]; then
  die "a manifest for $VERSION on $DATE is already published; rerun with a new date"
fi
if gh_cli release view "$TAG" -R "$PACKAGES_REPO" > /dev/null 2>&1; then
  echo "release $TAG exists without a published manifest (earlier failed run); recreating it"
  gh_cli release delete "$TAG" -R "$PACKAGES_REPO" --yes --cleanup-tag
fi

# 4. Emit, record provenance, upload, verify, and only then publish the manifests.
python3 "$TOOLING_DIR/scripts/emit_rustup_dist.py" --release-dir "$WORK/release" --site "$SITE" \
  --components "$WORK/components" --date "$DATE" --producer-host "$PRODUCER_HOST" \
  --base-url "https://github.com/$PACKAGES_REPO/releases/download/$TAG"
python3 -I -c '
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["tooling_sha"] = sys.argv[2]
open(path, "w").write(json.dumps(data, indent=2, sort_keys=True) + "\n")' "$SITE/dist/$DATE/yurt-release-manifest.json" "$TOOLING_SHA"

gh_cli release create "$TAG" -R "$PACKAGES_REPO" --title "Rustup dist $VERSION ($DATE)" \
  --notes "Component tarballs for rustup channel $VERSION, built from yurt-rust release $RUST_RELEASE. Tooling: $RUST_REPO@$TOOLING_SHA. Producer host: $PRODUCER_HOST."
gh_cli release upload "$TAG" -R "$PACKAGES_REPO" "$WORK/components"/*

python3 "$TOOLING_DIR/scripts/verify_rustup_dist_urls.py" --dist "$SITE/dist" --channel "$VERSION" --date "$DATE"

git -C "$SITE" add dist
git -C "$SITE" -c user.name="github-actions[bot]" -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
  commit --quiet -m "chore: publish rustup dist $VERSION $DATE"
git -C "$SITE" push origin "HEAD:$PAGES_BRANCH"
echo "published $TAG"
