#!/usr/bin/env python3
"""Validate staged rustup dist artifacts as data, for the privileged publish and push jobs.

Everything here is trusted code: identity comes only from the plan, the artifacts are untrusted
output of tooling code and are inspected (never executed, never extracted blindly) before use.
Standard library only, no sibling imports; run with `python3 -I`.

Subcommands (plan values come from PLAN_* in the environment, except for `plan` itself):
  plan --release R --date D --producer-host H --tooling-sha S
      validate the inputs and append release_tag, version, revision, date, producer_host,
      tooling_sha and tag as key=value lines to $OUTPUT_FILE
  validate-dist --dir D --site S
      date rules against S, then D must hold exactly one regular dist.tar with valid content;
      if DIGESTS is set and not empty, the artifact must match it
  validate-components --dist-dir D --dir C
      validate D as above (no date rules), then the component files in C against the manifest;
      append digests=<compact JSON> to $OUTPUT_FILE
  apply --dist-dir D --site S
      requires DIGESTS; date rules, validation, digest check, then write the five files into S
      and print their paths, one per line
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

HOSTS = ("aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
GUEST = "wasm32-yurt-linux-musl"
PACKAGES = ("rustc", "cargo", "rust-std")
RELEASE_URL = "https://github.com/YurtOS/yurt-packages/releases/download"
CANONICAL = "dated manifest is not the canonical rendering of its entries and the plan"
MAX_TAR_BYTES = 8 << 20
MAX_MEMBER_BYTES = 1 << 20
MAX_TOTAL_BYTES = 4 << 20

RELEASE_RE = re.compile(r"rust-v([0-9]+\.[0-9]+\.[0-9]+)-([0-9]+\.[0-9]+\.[0-9]+)")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
BOUND_KEYS = {"release_tag": "release_tag", "rust_version": "version", "revision": "revision", "date": "date", "producer_host": "producer_host"}
PROVENANCE_KEYS = {*BOUND_KEYS, "source_git_revision", "archives"}
OPTIONAL_PROVENANCE_KEYS = {"guest_sdks"}


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


def real_date(value: str) -> bool:
    if not DATE_RE.fullmatch(value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def make_plan(release: str, date: str, producer_host: str, tooling_sha: str, today: str) -> Plan:
    match = RELEASE_RE.fullmatch(release)
    if not match:
        raise StageError(f"release {release!r} is not rust-vX.Y.Z-A.B.C")
    if not real_date(today):
        raise StageError(f"today {today!r} is not a real YYYY-MM-DD date")
    date = date or today
    if not real_date(date):
        raise StageError(f"date {date!r} is not a real YYYY-MM-DD date")
    if date > today:
        raise StageError(f"date {date} is in the future (today is {today})")
    if producer_host not in HOSTS:
        raise StageError(f"producer host {producer_host!r} is not one of the supported hosts {', '.join(HOSTS)}")
    if not SHA_RE.fullmatch(tooling_sha):
        raise StageError(f"tooling SHA {tooling_sha!r} is not a 40-hex commit SHA")
    version, revision = match.groups()
    return Plan(release, version, revision, date, producer_host, tooling_sha, f"rustup-{version}-{date}")


def expected_paths(plan: Plan) -> tuple[str, str, str, str, str]:
    """moving manifest, moving sidecar, dated manifest, dated sidecar, provenance."""
    name = f"channel-rust-{plan.version}.toml"
    return (f"dist/{name}", f"dist/{name}.sha256", f"dist/{plan.date}/{name}", f"dist/{plan.date}/{name}.sha256",
            f"dist/{plan.date}/yurt-release-manifest.json")


def check_date_rules(site: Path, plan: Plan) -> None:
    dist = site / "dist"
    if not os.path.lexists(dist):
        return
    if not stat.S_ISDIR(dist.lstat().st_mode):
        raise StageError(f"{str(dist)!r} exists but dist is not a directory")
    if os.path.lexists(dist / plan.date):
        raise StageError(f"dist/{plan.date} already exists; rerun with a new date")
    # Any entry with a real date name counts, whatever its type: fail closed.
    newest = max((entry.name for entry in dist.iterdir() if real_date(entry.name)), default=None)
    if newest is not None and plan.date <= newest:
        raise StageError(f"date {plan.date} must be later than the newest dated dir {newest}")


def read_dist_dir(directory: Path, plan: Plan) -> dict[str, bytes]:
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise StageError(f"staging directory {str(directory)!r} is not a directory")
    names = sorted(entry.name for entry in directory.iterdir())
    if names != ["dist.tar"]:
        raise StageError(f"staging directory must contain exactly dist.tar, found {names}")
    path = directory / "dist.tar"
    if not stat.S_ISREG(path.lstat().st_mode):
        raise StageError("dist.tar is not a regular file")
    return read_dist_tar(path, plan)


def read_dist_tar(path: Path, plan: Plan) -> dict[str, bytes]:
    expected = expected_paths(plan)
    size = path.lstat().st_size
    if size > MAX_TAR_BYTES:
        raise StageError(f"{path.name} is larger than {MAX_TAR_BYTES} bytes")
    dist: dict[str, bytes] = {}
    try:
        # "r:" is uncompressed only: a compressed stream is refused, never inflated.
        with tarfile.open(path, "r:") as tar:
            members, total = {}, 0
            for member in tar:
                name = member.name
                parts = PurePosixPath(name).parts
                if name.startswith("/") or PurePosixPath(name).is_absolute():
                    raise StageError(f"absolute member path {name!r}")
                if ".." in name.split("/") or ".." in parts:
                    raise StageError(f"member path contains .. : {name!r}")
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or member.sparse is not None:
                    raise StageError(f"member {name!r} is not a regular file")
                if name in members:
                    raise StageError(f"duplicate member {name!r}")
                if name not in expected:
                    raise StageError(f"unexpected member {name!r}")
                if member.size > MAX_MEMBER_BYTES:
                    raise StageError(f"member {name!r} exceeds the per-member cap of {MAX_MEMBER_BYTES} bytes")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise StageError(f"the members' total size exceeds the total cap of {MAX_TOTAL_BYTES} bytes")
                members[name] = member
            missing = [name for name in expected if name not in members]
            if missing:
                raise StageError(f"missing members: {', '.join(missing)}")
            # Only after every header passed: read the five members into memory, nothing touches disk.
            for name in expected:
                handle = tar.extractfile(members[name])
                data = handle.read(MAX_MEMBER_BYTES + 1) if handle else None
                if data is None or len(data) != members[name].size:
                    raise StageError(f"member {name!r} is truncated")
                dist[name] = data
    except tarfile.TarError as error:
        raise StageError(f"{path.name!r} is not an uncompressed tar: {str(error)!r}") from None
    check_content(dist, plan)
    return dist


def check_member_set(dist: dict[str, bytes], plan: Plan) -> None:
    expected = expected_paths(plan)
    missing = [name for name in expected if name not in dist]
    if missing:
        raise StageError(f"missing members: {', '.join(missing)}")
    extra = sorted(set(dist) - set(expected))
    if extra:
        raise StageError(f"unexpected member {extra[0]!r}")


def parse_manifest(dist: dict[str, bytes], plan: Plan) -> dict:
    try:
        return tomllib.loads(dist[expected_paths(plan)[2]].decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        raise StageError(f"dated manifest is not valid TOML: {str(error)!r}") from None


def reject_duplicates(pairs):
    keys = [key for key, _ in pairs]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate keys {duplicates}")
    return dict(pairs)


def parse_provenance(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise StageError(f"provenance is not valid JSON: {str(error)!r}") from None
    if not isinstance(value, dict):
        raise StageError("provenance is not a JSON object")
    return value


def check_content(dist: dict[str, bytes], plan: Plan) -> None:
    check_member_set(dist, plan)
    moving, moving_sidecar, dated, dated_sidecar, provenance_path = expected_paths(plan)
    if dist[moving] != dist[dated]:
        raise StageError("moving manifest differs from the dated copy")
    if dist[moving_sidecar] != dist[dated_sidecar]:
        raise StageError("moving sidecar differs from the dated copy")
    digest = hashlib.sha256(dist[dated]).hexdigest()
    if dist[dated_sidecar] != f"{digest}  channel-rust-{plan.version}.toml\n".encode():
        raise StageError("sidecar does not match the manifest")

    manifest = parse_manifest(dist, plan)
    label = f"{plan.version} (yurt {plan.revision})"
    try:
        if manifest["manifest-version"] != "2":
            raise StageError(f"dated manifest has manifest-version {manifest['manifest-version']!r}, expected '2'")
        if manifest["date"] != plan.date:
            raise StageError(f"manifest date {manifest['date']!r} does not match the plan date {plan.date}")
        for package in ("rust", *PACKAGES):
            if manifest["pkg"][package]["version"] != label:
                raise StageError(f"manifest version {manifest['pkg'][package]['version']!r} of {package} is not {label!r}")
        hosts = list(manifest["pkg"]["rustc"]["target"])
    except (KeyError, TypeError) as error:
        raise StageError(f"dated manifest has an unexpected shape: missing or malformed {error}") from None
    # The manifest carries no producer-host field; the producer must at least be one of its hosts.
    if plan.producer_host not in hosts:
        raise StageError(f"producer host {plan.producer_host} is not a host of the manifest {hosts!r}")
    check_canonical_manifest(dist, plan, manifest)

    provenance = parse_provenance(dist[provenance_path])
    for key in sorted(PROVENANCE_KEYS):
        if key not in provenance:
            raise StageError(f"provenance lacks {key}")
    unknown = sorted(set(provenance) - PROVENANCE_KEYS - OPTIONAL_PROVENANCE_KEYS)
    if unknown:
        raise StageError(f"provenance has unexpected keys {unknown!r}")
    for key, field in BOUND_KEYS.items():
        if provenance[key] != getattr(plan, field):
            raise StageError(f"provenance {key} {provenance[key]!r} does not match the plan value {getattr(plan, field)!r}")


# Vendored copy of write_manifest in YurtOS/yurt-rust scripts/emit_rustup_dist.py. The dated manifest must
# equal this rendering byte for byte, which closes the schema (no xz_url/zst_url, extra keys, changed
# profiles, renames, components or extensions). Template drift: if the emitter's output format changes,
# publish fails loudly with "not the canonical rendering" until this copy is updated to match.
def render_manifest(version: str, revision: str, date: str, entries: dict[str, dict[str, tuple[str, str]]], base_url: str) -> str:
    base = base_url.rstrip("/")
    label = f"{version} (yurt {revision})"
    hosts = list(entries["rustc"])
    guests = [target for target in entries["rust-std"] if target not in hosts]
    lines = ['manifest-version = "2"', f'date = "{date}"', "", "[pkg.rust]", f'version = "{label}"']
    for host in hosts:
        components = ", ".join(f'{{ pkg = "{package}", target = "{host}" }}' for package in PACKAGES)
        extensions = ", ".join(f'{{ pkg = "rust-std", target = "{guest}" }}' for guest in guests)
        lines += ["", f"[pkg.rust.target.{host}]", "available = true", f"components = [{components}]", f"extensions = [{extensions}]"]
    for package in PACKAGES:
        lines += ["", f"[pkg.{package}]", f'version = "{label}"']
        for target, (filename, digest) in entries[package].items():
            lines += ["", f"[pkg.{package}.target.{target}]", "available = true", f'url = "{base}/{filename}"', f'hash = "{digest}"']
    return "\n".join([*lines, "", "[renames]", "", "[profiles]", *(f'{p} = ["rustc", "cargo", "rust-std"]' for p in ("minimal", "default")), ""])


def check_canonical_manifest(dist: dict[str, bytes], plan: Plan, manifest: dict) -> None:
    """Re-render from the parsed (filename, hash) entries and the plan; anything else in the text is refused."""
    entries: dict[str, dict[str, tuple[str, str]]] = {}
    try:
        for package in PACKAGES:
            entries[package] = {}
            for target, entry in manifest["pkg"][package]["target"].items():
                url, digest = entry["url"], entry["hash"]
                if not isinstance(url, str) or not isinstance(digest, str):
                    raise StageError(CANONICAL)
                entries[package][target] = (url.rsplit("/", 1)[-1], digest)
        text = render_manifest(plan.version, plan.revision, plan.date, entries, f"{RELEASE_URL}/{plan.tag}")
    except (KeyError, TypeError, AttributeError):
        raise StageError(CANONICAL) from None
    if text.encode() != dist[expected_paths(plan)[2]]:
        raise StageError(CANONICAL)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_components(directory: Path, dist: dict[str, bytes], plan: Plan) -> dict[str, str]:
    check_member_set(dist, plan)
    manifest = parse_manifest(dist, plan)
    prefix = f"{RELEASE_URL}/{plan.tag}/"
    hashes: dict[str, str] = {}
    try:
        packages = manifest["pkg"]
        unknown = sorted(set(packages) - {"rust", *PACKAGES})
        if unknown:
            raise StageError(f"unknown package {unknown[0]!r} in the manifest")
        for package in PACKAGES:
            for target, entry in packages[package]["target"].items():
                allowed = (*HOSTS, GUEST) if package == "rust-std" else HOSTS
                if target not in allowed:
                    raise StageError(f"unknown target {target!r} for {package}")
                url, digest = entry["url"], entry["hash"]
                name = f"{package}-{plan.version}-{target}.tar.gz"
                if not isinstance(url, str) or not url.startswith(prefix):
                    raise StageError(f"url for {package} {target} {url!r} is not under {prefix}")
                if url != prefix + name:
                    raise StageError(f"url for {package} {target} {url!r} must be {prefix}{name}")
                if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
                    raise StageError(f"hash for {package} {target} {digest!r} is not a sha256")
                hashes[name] = digest
        for target in packages["rust"].get("target", {}):
            if target not in HOSTS:
                raise StageError(f"unknown target {target!r} for rust")
    except (KeyError, TypeError, AttributeError) as error:
        raise StageError(f"dated manifest has an unexpected shape: missing or malformed {error}") from None
    check_canonical_manifest(dist, plan, manifest)

    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise StageError(f"components directory {str(directory)!r} is not a directory")
    found: dict[str, str] = {}
    for entry in sorted(directory.iterdir()):
        if not stat.S_ISREG(entry.lstat().st_mode):
            raise StageError(f"component {entry.name!r} is not a regular file")
        found[entry.name] = ""
    missing = sorted(set(hashes) - set(found))
    if missing:
        raise StageError(f"missing components: {missing!r}")
    extra = sorted(set(found) - set(hashes))
    if extra:
        raise StageError(f"unexpected components: {extra!r}")
    for name in sorted(hashes):
        found[name] = sha256_file(directory / name)
        if found[name] != hashes[name]:
            raise StageError(f"{name!r} does not match the manifest hash")
    return found


def compute_digests(tar: Path, dist: dict[str, bytes], components: dict[str, str] | None) -> dict:
    digests = {
        "dist.tar": sha256_file(tar),
        "members": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(dist.items())},
    }
    if components is not None:
        digests["components"] = dict(sorted(components.items()))
    return digests


def verify_digests(expected: dict, actual: dict) -> None:
    """dist.tar and every member must match exactly; components are publish's concern and ignored."""
    if not isinstance(expected, dict):
        raise StageError("digests are not a JSON object")
    if "dist.tar" not in expected:
        raise StageError("missing digest for dist.tar")
    if expected["dist.tar"] != actual["dist.tar"]:
        raise StageError("digest mismatch for dist.tar")
    members = expected.get("members")
    if not isinstance(members, dict):
        raise StageError("missing digest for members")
    for name, digest in actual["members"].items():
        if name not in members:
            raise StageError(f"missing digest for {name}")
        if members[name] != digest:
            raise StageError(f"digest mismatch for {name}")
    extra = sorted(set(members) - set(actual["members"]))
    if extra:
        raise StageError(f"unexpected digest for {extra[0]}")


def write_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o644)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
    finally:
        os.close(fd)


def apply_dist(site: Path, dist: dict[str, bytes], plan: Plan) -> list[str]:
    check_member_set(dist, plan)
    check_date_rules(site, plan)
    paths = expected_paths(plan)
    provenance = parse_provenance(dist[paths[4]])
    provenance["tooling_sha"] = plan.tooling_sha
    contents = {**dist, paths[4]: (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()}
    dist_dir, dated = site / "dist", site / "dist" / plan.date
    # Check every target before writing anything: nothing is followed, nothing but these five is touched.
    for name in paths[:2]:
        target = site / name
        if os.path.lexists(target) and not stat.S_ISREG(target.lstat().st_mode):
            raise StageError(f"{name} exists and is not a regular file")
    dist_dir.mkdir(exist_ok=True)
    if not stat.S_ISDIR(dist_dir.lstat().st_mode):
        raise StageError(f"{str(dist_dir)!r} exists but dist is not a directory")
    dated.mkdir()
    os.chmod(dated, 0o755)
    for name in (*paths[2:], *paths[:2]):
        write_file(site / name, contents[name])
    return list(paths)


def plan_from_env() -> Plan:
    env = {key: os.environ.get(f"PLAN_{key.upper()}", "") for key in Plan.__dataclass_fields__}
    if not env["date"]:
        raise StageError("PLAN_DATE is empty; the plan must fix the date")
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    plan = make_plan(env["release_tag"], env["date"], env["producer_host"], env["tooling_sha"], today)
    for key, value in env.items():
        if getattr(plan, key) != value:
            raise StageError(f"PLAN_{key.upper()} {value!r} does not match the plan derived from the inputs ({getattr(plan, key)!r})")
    return plan


def write_outputs(lines: dict[str, str]) -> None:
    output = os.environ.get("OUTPUT_FILE")
    if not output:
        raise StageError("OUTPUT_FILE is required")
    for key, value in lines.items():
        if "\n" in value or "\r" in value:
            raise StageError(f"output {key} contains a newline")
    with open(output, "a", encoding="utf-8") as handle:
        handle.write("".join(f"{key}={value}\n" for key, value in lines.items()))


def expected_digests() -> dict:
    raw = os.environ.get("DIGESTS", "")
    if not raw:
        raise StageError("DIGESTS is required: the digest output of publish")
    try:
        return json.loads(raw)
    except ValueError:
        raise StageError("DIGESTS is not valid digest JSON") from None


def run(args: argparse.Namespace) -> None:
    if args.command == "plan":
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        plan = make_plan(args.release, args.date, args.producer_host, args.tooling_sha, today)
        write_outputs({key: getattr(plan, key) for key in Plan.__dataclass_fields__})
        return
    plan = plan_from_env()
    if args.command == "validate-dist":
        check_date_rules(Path(args.site), plan)
        dist = read_dist_dir(Path(args.dir), plan)
        if os.environ.get("DIGESTS"):
            verify_digests(expected_digests(), compute_digests(Path(args.dir) / "dist.tar", dist, None))
    elif args.command == "validate-components":
        dist = read_dist_dir(Path(args.dist_dir), plan)
        components = check_components(Path(args.dir), dist, plan)
        digests = compute_digests(Path(args.dist_dir) / "dist.tar", dist, components)
        write_outputs({"digests": json.dumps(digests, separators=(",", ":"), sort_keys=True)})
    elif args.command == "apply":
        expected = expected_digests()
        site = Path(args.site)
        check_date_rules(site, plan)
        dist = read_dist_dir(Path(args.dist_dir), plan)
        verify_digests(expected, compute_digests(Path(args.dist_dir) / "dist.tar", dist, None))
        for path in apply_dist(site, dist, plan):
            print(path)


def one_line(text: str) -> str:
    """Escape anything non-printable, so an error can never start a new log line (workflow commands)."""
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rustup_dist_stage.py", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--release", required=True)
    plan.add_argument("--date", required=True)
    plan.add_argument("--producer-host", required=True)
    plan.add_argument("--tooling-sha", required=True)
    validate = commands.add_parser("validate-dist")
    validate.add_argument("--dir", required=True)
    validate.add_argument("--site", required=True)
    components = commands.add_parser("validate-components")
    components.add_argument("--dist-dir", required=True)
    components.add_argument("--dir", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--dist-dir", required=True)
    apply.add_argument("--site", required=True)
    args = parser.parse_args(argv)
    try:
        run(args)
    except (StageError, OSError) as error:
        print(f"rustup_dist_stage: {one_line(str(error))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
