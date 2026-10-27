#!/usr/bin/env python3
"""Publish Package's version policy: a publish adds a new version, not another copy of one.

Archives are committed to git, so every publish grows the repository for good. Before the
build, `check` compares the port's yurt-pack.toml with the package's packages/<name>.json:

- the same version and build is already published: refused (a release is immutable; Release
  never re-signs one), whatever the new archive's bytes would be;
- the same version under another build: refused unless --allow-new-build is given (the
  workflow's `allow_new_build` input), for a rebuild that has to ship without a version change,
  such as a packaging fix or the Python set's shared `yurt_N` build;
- a new version, or a package not published yet: allowed.

Standard library only: the workflow runs it with the runner's python3.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path


class PolicyError(Exception):
    pass


def check(repo_root: Path, manifest_path: Path, allow_new_build: bool) -> str:
    try:
        manifest = tomllib.loads(manifest_path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PolicyError(f"reading {manifest_path}: {error}") from error
    try:
        name, version, build = (str(manifest[key]) for key in ("name", "version", "build"))
    except KeyError as error:
        raise PolicyError(f"{manifest_path} has no {error.args[0]}") from error
    release = f"{name} {version}-{build}"

    package_path = repo_root / "packages" / f"{name}.json"
    if not package_path.exists():
        return f"{release}: a new package"
    try:
        package = json.loads(package_path.read_text())
        published = [(str(entry["version"]), str(entry["build"])) for entry in package["versions"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PolicyError(f"reading {package_path}: {error}") from error

    if (version, build) in published:
        raise PolicyError(
            f"{release} is already published. A release is never replaced; publish a new version "
            "(or a new build, with allow_new_build)."
        )
    builds = sorted(b for v, b in published if v == version)
    if builds and not allow_new_build:
        raise PolicyError(
            f"{name} {version} is already published (builds: {', '.join(builds)}). Publish when the "
            "version changes; every archive stays in git history. To ship a new build of the same "
            f"version ({build}), dispatch again with allow_new_build."
        )
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
    args = parser.parse_args(argv)
    try:
        message = check(args.repo_root, args.manifest, args.allow_new_build == "true")
    except PolicyError as error:
        print(f"::error::{error}")
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
