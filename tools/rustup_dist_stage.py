#!/usr/bin/env python3
"""Validate staged rustup dist artifacts as data (skeleton)."""

import sys
from dataclasses import dataclass
from pathlib import Path

HOSTS = ("aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
GUEST = "wasm32-yurt-linux-musl"
PACKAGES = ("rustc", "cargo", "rust-std")
MAX_TAR_BYTES = 8 << 20
MAX_MEMBER_BYTES = 1 << 20
MAX_TOTAL_BYTES = 4 << 20


class StageError(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    release_tag: str
    version: str
    revision: str
    date: str
    producer_host: str
    tooling_sha: str
    tag: str


def make_plan(release: str, date: str, producer_host: str, tooling_sha: str, today: str) -> Plan:
    raise NotImplementedError


def check_date_rules(site: Path, plan: Plan) -> None:
    raise NotImplementedError


def read_dist_dir(directory: Path, plan: Plan) -> dict[str, bytes]:
    raise NotImplementedError


def read_dist_tar(path: Path, plan: Plan) -> dict[str, bytes]:
    raise NotImplementedError


def check_components(directory: Path, dist: dict[str, bytes], plan: Plan) -> dict[str, str]:
    raise NotImplementedError


def compute_digests(tar: Path, dist: dict[str, bytes], components: dict[str, str] | None) -> dict:
    raise NotImplementedError


def verify_digests(expected: dict, actual: dict) -> None:
    raise NotImplementedError


def apply_dist(site: Path, dist: dict[str, bytes], plan: Plan) -> list[str]:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
