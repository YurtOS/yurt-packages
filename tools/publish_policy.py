#!/usr/bin/env python3
"""Publish Package's version policy: a publish adds a new version, not another copy of one.

Archives are committed to git, so every publish grows the repository for good. Before the
build, `check` compares the port's yurt-pack.toml with the package's packages/<name>.json:

- the same version and build is already published: refused (a release is immutable; Release
  never re-signs one), whatever the new archive's bytes would be;
- the same version under another build: refused unless --allow-new-build is given (the
  workflow's `allow_new_build` input), for a rebuild that has to ship without a version change,
  such as a packaging fix or the Python set's shared `yurt_N` build;
- a release pkg would not rank above the newest published one: refused unless
  --allow-downgrade is given (the workflow's `allow_downgrade` input). pkg's resolver orders
  releases by semver version, then by the N of a `yurt_N` build (any other build counts as 0),
  and skips yanked releases; release_key() is that order;
- a new version, or a package not published yet: allowed.

Standard library only: the workflow runs it with the runner's python3.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

# semver.org's regular expression, as the semver crate pkg uses accepts it.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
YURT_BUILD = re.compile(r"^yurt_(\d+)$")


class PolicyError(Exception):
    pass


def semver_key(version: str) -> tuple | None:
    """A sort key in semver precedence, or None when `version` is not semver.

    A release ranks above its prereleases. Prerelease identifiers compare left to right:
    numeric ones as numbers and below alphanumeric ones, which compare as ASCII; a shorter list
    ranks lower when it is a prefix. Build metadata (`+...`) does not count.
    """
    match = SEMVER.match(version)
    if not match:
        return None
    core = tuple(int(part) for part in match.group(1, 2, 3))
    if match.group(4) is None:
        return (core, (1,))
    identifiers = tuple(
        (0, int(ident), "") if ident.isdigit() else (1, 0, ident) for ident in match.group(4).split(".")
    )
    return (core, (0, identifiers))


def build_rank(build: str) -> int:
    """pkg's yurt_build_number(build).unwrap_or(0)."""
    match = YURT_BUILD.match(build)
    return int(match.group(1)) if match else 0


def release_key(version: str, build: str) -> tuple:
    """The order pkg's resolver picks releases in; an unparsable version ranks as 0.0.0."""
    return (semver_key(version) or semver_key("0.0.0"), build_rank(build))


def check(repo_root: Path, manifest_path: Path, allow_new_build: bool, allow_downgrade: bool = False) -> str:
    try:
        manifest = tomllib.loads(manifest_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PolicyError(f"reading {manifest_path}: {error}") from error
    try:
        name, version, build = (str(manifest[key]) for key in ("name", "version", "build"))
    except KeyError as error:
        raise PolicyError(f"{manifest_path} has no {error.args[0]}") from error
    release = f"{name} {version}-{build}"
    if semver_key(version) is None:
        raise PolicyError(f"{release}: {version} is not a semver version")

    package_path = repo_root / "packages" / f"{name}.json"
    if not package_path.exists():
        return f"{release}: a new package"
    try:
        package = json.loads(package_path.read_text())
        entries = [
            (str(entry["version"]), str(entry["build"]), entry.get("yanked") is True) for entry in package["versions"]
        ]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PolicyError(f"reading {package_path}: {error}") from error

    published = [(v, b) for v, b, _ in entries]
    if (version, build) in published:
        raise PolicyError(
            f"{release} is already published. A release is never replaced; publish a new version "
            "(or a new build, with allow_new_build)."
        )
    current = [(v, b) for v, b, yanked in entries if not yanked]
    downgrade = ""
    if current:
        newest = max(current, key=lambda release: release_key(*release))
        if release_key(version, build) <= release_key(*newest):
            relation = "older than" if release_key(version, build) < release_key(*newest) else "not newer than"
            downgrade = f"{release} is {relation} the newest published {name} {newest[0]}-{newest[1]}"
            if not allow_downgrade:
                raise PolicyError(
                    f"{downgrade} (pkg orders by semver version, then by the N of a yurt_N build, any "
                    "other build counting as 0), so pkg would not pick it. Bump the port's version or "
                    "yurt_N build; to publish it anyway, dispatch again with allow_downgrade."
                )
    builds = sorted(b for v, b in published if v == version)
    if builds and not allow_new_build:
        raise PolicyError(
            f"{name} {version} is already published (builds: {', '.join(builds)}). Publish when the "
            "version changes; every archive stays in git history. To ship a new build of the same "
            f"version ({build}), dispatch again with allow_new_build."
        )
    if downgrade:
        return f"{downgrade} (allow_downgrade)"
    if builds:
        return f"{release}: a new build of {name} {version} (allow_new_build; published builds: {', '.join(builds)})"
    return f"{release}: a new version"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check", help="refuse a publish the version policy does not allow")
    check_parser.add_argument("--repo-root", type=Path, required=True)
    check_parser.add_argument("--manifest", type=Path, required=True, help="the port's yurt-pack.toml")
    check_parser.add_argument(
        "--allow-new-build",
        choices=["true", "false"],
        default="false",
        help="allow another build of a published version (the workflow input, as text)",
    )
    check_parser.add_argument(
        "--allow-downgrade",
        choices=["true", "false"],
        default="false",
        help="allow a release pkg would rank at or below the newest published one (the workflow input, as text)",
    )
    args = parser.parse_args(argv)
    try:
        message = check(args.repo_root, args.manifest, args.allow_new_build == "true", args.allow_downgrade == "true")
    except PolicyError as error:
        print(f"::error::{error}")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
