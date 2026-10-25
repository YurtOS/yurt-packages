#!/usr/bin/env python3
"""Index regeneration and Sigstore bundle checks for the package repository.

`release.yml` uses this script to regenerate `index.json`, to list the archives
that still need a signature, and to check every committed bundle before it
pushes. CI runs the same check on every pull request and push.

Clients (`pkg` from YurtOS/yurt-pkg) verify a bundle as a Sigstore bundle
v0.3 (`application/vnd.dev.sigstore.bundle.v0.3+json`) with a message
signature over the raw payload bytes, not a DSSE envelope. The leaf
certificate must carry exactly the subject and issuer below, which images pin
in `/etc/yurt-pkg/trusted-repos.toml`. `check` tests that shape, the payload
digest and the certificate identity. With `--cosign` it also runs a full
`cosign verify-blob` (certificate chain, signature, transparency log) on each
bundle.

What may be signed is anchored in git history (a full clone is required):
every commit on any line of history that holds a validly signed index.json.
The current index must not have a lower index_version than any of them.
Every release (name, version, build) that any of them ever listed was signed;
its record must not change, its url must not move to another release, and its
bundle must stay valid. Only releases no signed index ever listed may be
unsigned ("pending") and get signed. Before any index was signed (the first run),
the legacy list names the 12 archives committed with placeholder bundles,
each pinned to its sha256; once an anchor exists the list must be empty.

Subcommands (all take --legacy FILE and --cosign BIN):

  regenerate-index --now <RFC 3339>   check the tree, verify the current
                                      index.json against its bundle, then
                                      rebuild it from packages/*.json with
                                      index_version above the anchor's,
                                      generated_at now, expires_at + 7 days
  plan                                print the archives that may be signed now
                                      (pending or legacy), one path per line;
                                      fail if the check finds any problem
  check [--strict]                    fail on any problem; --strict also fails
                                      on any unsigned (legacy or pending) path
  prune-legacy                        drop the entries of FILE that now hold a
                                      valid bundle
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SUBJECT = "https://github.com/YurtOS/yurt-packages/.github/workflows/release.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"
MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
PLACEHOLDER = b"local test fixture bundle\n"
# Rekor v1 hashedrekord entries, as cosign v3.0.6 writes them on public-good.
# A change of log format (Rekor v2 writes hashedrekord 0.0.2) must first be
# proven against yurt-pkg's verifier.
TLOG_KIND = ("hashedrekord", "0.0.1")
INDEX = "index.json"
VALID_NAME = re.compile(r"[a-z0-9][a-z0-9._+-]*")
EXPIRY = dt.timedelta(days=7)
# Fulcio certificate extensions: the OIDC issuer (v2 is a DER UTF8String, v1
# the raw string).
OID_ISSUER_V2 = "1.3.6.1.4.1.57264.1.8"
OID_ISSUER_V1 = "1.3.6.1.4.1.57264.1.1"


class SigningError(Exception):
    pass


def format_time(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SigningError(f"invalid RFC 3339 time {value!r}") from error
    if parsed.tzinfo is None:
        raise SigningError(f"time {value!r} has no UTC offset")
    return parsed.astimezone(dt.timezone.utc)


def dump_json(value: object) -> bytes:
    """The bytes serde_json::to_vec_pretty writes, plus a newline."""
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


@dataclass(frozen=True)
class Archive:
    package: str
    version: str
    build: str
    path: str  # relative to the repository root
    sha256: str
    size: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.package, self.version, self.build)


def published_archives(root: Path) -> list[Archive]:
    """Every archive that packages/*.json lists, checked against its file."""
    archives = []
    for package_path in sorted((root / "packages").glob("*.json")):
        package = json.loads(package_path.read_bytes())
        if not VALID_NAME.fullmatch(package_path.stem):
            raise SigningError(f"invalid package name {package_path.stem!r}: expected {VALID_NAME.pattern}")
        if package.get("name") != package_path.stem:
            raise SigningError(f"{package_path.relative_to(root)} declares name {package.get('name')!r}")
        for version in package["versions"]:
            url = version["url"]
            parts = Path(url).parts
            if not url.startswith("artifacts/") or ".." in parts or not url.endswith(".yurtpkg"):
                raise SigningError(f"{package_path.name}: archive url {url!r} is not under artifacts/")
            if version["signing"] != {"subject": SUBJECT, "issuer": ISSUER}:
                raise SigningError(f"{url}: package record names signer {version['signing']}, not {SUBJECT}")
            file = root / url
            if not file.is_file() or file.is_symlink():
                raise SigningError(f"{url}: archive is missing")
            data = file.read_bytes()
            if len(data) != version["size"] or hashlib.sha256(data).hexdigest() != version["sha256"]:
                raise SigningError(f"{url}: archive does not match its package record")
            archives.append(
                Archive(package_path.stem, version["version"], version["build"], url, version["sha256"], version["size"])
            )
    return archives


def regenerate_index(root: Path, now: dt.datetime, legacy: dict[str, str] | None = None, cosign: str | None = None) -> dict:
    """Rebuild index.json; refuses unless the tree checks clean and the
    current index.json is the signed one (or, before any index was signed,
    carries the placeholder bundle)."""
    legacy = legacy or {}
    result = check(root, legacy, cosign)
    if result.problems:
        raise SigningError("refusing to regenerate the index: " + "; ".join(result.problems))
    index_path = root / INDEX
    previous = json.loads(index_path.read_bytes())
    if previous.get("schema") != 1:
        raise SigningError(f"unsupported index schema {previous.get('schema')!r}")
    if result.anchor is None:
        if (root / f"{INDEX}.bundle").read_bytes() != PLACEHOLDER:
            raise SigningError("no signed index in history, and index.json.bundle is not the legacy placeholder")
    elif not result.index_signed:
        raise SigningError("index.json is not the signed index")
    floor = max(previous["index_version"], result.anchor.index_version if result.anchor else 0)
    if now <= parse_time(previous["generated_at"]):
        raise SigningError(f"--now {format_time(now)} is not after generated_at {previous['generated_at']}")
    packages = {}
    for package_path in sorted((root / "packages").glob("*.json")):
        data = package_path.read_bytes()
        packages[package_path.stem] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "url": f"packages/{package_path.stem}.json",
        }
    index = {
        "schema": 1,
        "index_version": floor + 1,
        "generated_at": format_time(now),
        "expires_at": format_time(now + EXPIRY),
        "packages": dict(sorted(packages.items())),
    }
    index_path.write_bytes(dump_json(index))
    return index


def _der_utf8(value: bytes) -> str:
    """Decode a DER UTF8String with a short or long length."""
    if len(value) < 2 or value[0] != 0x0C:
        raise SigningError("issuer extension is not a UTF8String")
    length = value[1]
    offset = 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(value[2 : 2 + count], "big")
        offset = 2 + count
    if offset + length != len(value):
        raise SigningError("issuer extension has a bad length")
    return value[offset:].decode()


def certificate_identity(raw: bytes) -> tuple[list[str], str]:
    from cryptography import x509

    cert = x509.load_der_x509_certificate(raw)
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as error:
        raise SigningError("certificate has no subject alternative name") from error
    subjects = san.get_values_for_type(x509.UniformResourceIdentifier)
    for oid, decode in ((OID_ISSUER_V2, _der_utf8), (OID_ISSUER_V1, lambda value: value.decode())):
        try:
            ext = cert.extensions.get_extension_for_oid(x509.ObjectIdentifier(oid))
        except x509.ExtensionNotFound:
            continue
        return subjects, decode(ext.value.value)
    raise SigningError("certificate has no Fulcio OIDC issuer extension")


def bundle_problem(bundle: bytes, payload: bytes) -> str | None:
    """Why `bundle` is not a release.yml bundle for `payload`, or None."""
    try:
        data = json.loads(bundle)
    except (ValueError, UnicodeDecodeError):
        return "not a JSON Sigstore bundle"
    if not isinstance(data, dict):
        return "not a JSON Sigstore bundle"
    try:
        return _bundle_problem(data, payload)
    except (AttributeError, TypeError, KeyError):
        return "malformed Sigstore bundle"


def _bundle_problem(data: dict, payload: bytes) -> str | None:
    if data.get("mediaType") != MEDIA_TYPE:
        return f"media type is {data.get('mediaType')!r}, expected {MEDIA_TYPE}"
    if "dsseEnvelope" in data or "messageSignature" not in data:
        return "expected a message signature over the raw payload, not a DSSE envelope"
    signature = data["messageSignature"]
    digest = signature.get("messageDigest", {})
    if digest.get("algorithm") != "SHA2_256":
        return f"digest algorithm is {digest.get('algorithm')!r}, expected SHA2_256"
    try:
        signed = base64.b64decode(digest.get("digest", ""), validate=True)
    except ValueError:
        return "message digest is not base64"
    if signed != hashlib.sha256(payload).digest():
        return "message digest does not match the payload"
    if not signature.get("signature"):
        return "message signature is empty"
    material = data.get("verificationMaterial", {})
    entries = material.get("tlogEntries") or []
    if not entries or not all(entry.get("inclusionProof") and entry.get("integratedTime") for entry in entries):
        return "no transparency log entry with an inclusion proof"
    kinds = {(entry.get("kindVersion", {}).get("kind"), entry.get("kindVersion", {}).get("version")) for entry in entries}
    if kinds != {TLOG_KIND}:
        return f"log entry kinds {sorted(kinds)}, expected {TLOG_KIND} (the format yurt-pkg's verifier is proven on)"
    raw = material.get("certificate", {}).get("rawBytes")
    if not raw:
        return "no leaf certificate (keyless signing carries one)"
    try:
        subjects, issuer = certificate_identity(base64.b64decode(raw, validate=True))
    except (SigningError, ValueError) as error:
        return f"unreadable certificate: {error}"
    if subjects != [SUBJECT]:
        return f"certificate subject is {subjects}, expected {SUBJECT}"
    if issuer != ISSUER:
        return f"certificate issuer is {issuer!r}, expected {ISSUER}"
    return None


COSIGN_VERSION = "v3.0.6"
# A real GitHub Actions bundle and its identity (see tests/fixtures/sigstore/README.md).
HEALTH_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sigstore"
HEALTH_SUBJECT = "https://github.com/goreleaser/goreleaser/.github/workflows/release.yml@refs/tags/v2.19.0"


def _run_cosign(cosign: str, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([cosign, *args], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SigningError(f"cosign could not run: {error}") from error


def _verify_blob(cosign: str, bundle: Path, payload: Path, subject: str) -> tuple[int, str]:
    result = _run_cosign(
        cosign,
        ["verify-blob", "--bundle", str(bundle), "--certificate-identity", subject,
         "--certificate-oidc-issuer", ISSUER, str(payload)],
    )
    return result.returncode, (result.stderr or result.stdout).strip()


def cosign_health(cosign: str) -> None:
    """Abort unless cosign is v3.0.6 and verifies the in-repo fixture bundle.

    After this passes, an exit 1 from cosign verify-blob means the bundle is
    invalid (malformed, wrong identity, bad signature or log proof), never
    that cosign itself is broken. Run once per command (find_anchor)."""
    version = _run_cosign(cosign, ["version"])
    versions = [line.split()[-1] for line in version.stdout.splitlines() if line.startswith("GitVersion:")]
    if version.returncode != 0 or versions != [COSIGN_VERSION]:
        raise SigningError(f"cosign {COSIGN_VERSION} is required, got {versions or version.stdout.strip()!r}")
    code, output = _verify_blob(
        cosign,
        HEALTH_FIXTURE / "goreleaser-v2.19.0-checksums.txt.sigstore.json",
        HEALTH_FIXTURE / "goreleaser-v2.19.0-checksums.txt",
        HEALTH_SUBJECT,
    )
    if code != 0:
        raise SigningError(f"cosign health check failed (it does not verify the goreleaser fixture, exit {code}): {output}")


def cosign_problem(cosign: str, bundle: Path, payload: Path) -> str | None:
    """None if it verifies, the failure if cosign rejects it (exit 1).

    Call only after cosign_health. Any other exit, a missing binary or a
    timeout raises SigningError: it says nothing about the bundle."""
    code, output = _verify_blob(cosign, bundle, payload, SUBJECT)
    if code == 0:
        return None
    if code == 1:
        return f"cosign verify-blob failed: {output}"
    raise SigningError(f"cosign could not verify {bundle.name} (exit {code}): {output}")


def cosign_bytes_problem(cosign: str, bundle: bytes, payload: bytes) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        bundle_path = Path(tmp, "payload.bundle")
        payload_path = Path(tmp, "payload")
        bundle_path.write_bytes(bundle)
        payload_path.write_bytes(payload)
        return cosign_problem(cosign, bundle_path, payload_path)


def verify(bundle: bytes, payload: bytes, cosign: str | None) -> str | None:
    problem = bundle_problem(bundle, payload)
    if problem is None and cosign:
        problem = cosign_bytes_problem(cosign, bundle, payload)
    return problem


def git(root: Path, *args: str) -> bytes | None:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    return result.stdout if result.returncode == 0 else None


@dataclass
class Anchor:
    """Everything any signed index in history ever covered.

    A candidate is every commit, on any line of history (--full-history, so a
    merge cannot hide one), that holds an index.json and an index.json.bundle
    that verify (with --cosign, by cosign verify-blob too).
    """

    index_version: int  # the highest signed index_version
    index: bytes  # the signed index.json with that version
    releases: dict[tuple[str, str, str], set[tuple[str, str, int]]]  # key -> signed (url, sha256, size)
    urls: dict[str, tuple[str, str, str]]  # signed archive url -> key


def find_anchor(root: Path, cosign: str | None = None) -> Anchor | None:
    if git(root, "rev-parse", "--is-inside-work-tree") is None:
        raise SigningError("not a git checkout: the signed history is needed")
    if git(root, "rev-parse", "--is-shallow-repository") != b"false\n":
        raise SigningError("shallow clone: the signed history is needed (fetch-depth: 0)")
    if cosign:
        cosign_health(cosign)
    log = git(root, "log", "--full-history", "--format=%H", "HEAD", "--", INDEX, f"{INDEX}.bundle")
    if log is None:
        return None  # no commit yet
    anchor = None
    verified: dict[tuple[str, str], bool] = {}  # (index blob, bundle blob) -> valid
    blob_cache: dict[str, bytes | None] = {}
    def blob(blob_id: str) -> bytes | None:
        if blob_id not in blob_cache:
            blob_cache[blob_id] = git(root, "cat-file", "blob", blob_id)
        return blob_cache[blob_id]

    for commit in log.decode().split():
        blobs = tree_blobs(root, commit)
        if INDEX not in blobs or f"{INDEX}.bundle" not in blobs:
            continue
        pair = (blobs[INDEX], blobs[f"{INDEX}.bundle"])
        index = blob(pair[0])
        if index is None:
            continue
        if pair not in verified:
            bundle = blob(pair[1])
            verified[pair] = bundle is not None and verify(bundle, index, cosign) is None
        if not verified[pair]:
            continue
        try:
            parsed = json.loads(index)
            version = int(parsed["index_version"])
            pinned = parsed["packages"]
        except (ValueError, KeyError, TypeError):
            continue
        if anchor is None:
            anchor = Anchor(version, index, {}, {})
        elif version > anchor.index_version:
            anchor.index_version, anchor.index = version, index
        for name, entry in pinned.items():
            blob_id = blobs.get(f"packages/{name}.json")
            data = blob(blob_id) if blob_id else None
            if data is None or hashlib.sha256(data).hexdigest() != entry.get("sha256") or len(data) != entry.get("size"):
                continue  # this commit re-holds the index beside other metadata; the one that signed it holds it
            for release in json.loads(data)["versions"]:
                key = (name, release["version"], release["build"])
                anchor.releases.setdefault(key, set()).add((release["url"], release["sha256"], release["size"]))
                anchor.urls.setdefault(release["url"], key)
    return anchor


def tree_blobs(root: Path, commit: str, paths: tuple[str, ...] = (INDEX, f"{INDEX}.bundle", "packages/")) -> dict[str, str]:
    """path -> blob id at `commit` for index.json, its bundle and packages/*,
    from one NUL-separated ls-tree (no path quoting)."""
    listing = git(root, "ls-tree", "-z", commit, "--", *paths) or b""
    blobs = {}
    for entry in listing.split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        fields = meta.split()
        if len(fields) == 3 and fields[1] == b"blob":
            blobs[path.decode("utf-8", "surrogateescape")] = fields[2].decode()
    return blobs


def package_blobs(root: Path, commit: str) -> dict[str, str]:
    return tree_blobs(root, commit, ("packages/",))


def committed_bundles(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in (".git", ".worktrees")]
        for name in filenames:
            if name.endswith(".bundle"):
                found.append(Path(dirpath, name).relative_to(root).as_posix())
    return sorted(found)


def read_legacy(path: Path | None) -> dict[str, str]:
    """Legacy entries: bundle path -> sha256 of the archive it may sign."""
    if path is None or not path.exists():
        return {}
    entries = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            raise SigningError(f"legacy entry {line!r}: expected '<bundle path> <archive sha256>'")
        entries[fields[0]] = fields[1]
    return entries


@dataclass
class CheckResult:
    problems: list[str]
    valid: int = 0
    legacy: int = 0  # unsigned archives the legacy list allows (first run only)
    pending: int = 0  # unsigned archives the signed index does not list yet
    to_sign: list[str] = field(default_factory=list)
    anchor: Anchor | None = None
    index_signed: bool = False  # index.json.bundle is valid for index.json


def check(root: Path, legacy: dict[str, str], cosign: str | None = None) -> CheckResult:
    anchor = find_anchor(root, cosign)
    result = CheckResult([], anchor=anchor)
    problems = result.problems
    archives = published_archives(root)
    expected = {f"{INDEX}.bundle"} | {f"{archive.path}.bundle" for archive in archives}
    for bundle_rel in committed_bundles(root):
        if bundle_rel not in expected:
            problems.append(f"{bundle_rel}: no index.json or published archive to sign")
    if anchor is not None and legacy:
        problems.append("legacy list must be empty once a signed index.json exists")
    for entry in sorted(set(legacy) - expected):
        problems.append(f"legacy entry {entry}: not a bundle path this repository expects")

    # index.json
    index_bundle = root / f"{INDEX}.bundle"
    index_bytes = (root / INDEX).read_bytes()
    if index_bundle.is_symlink() or not index_bundle.is_file():
        problems.append(f"{INDEX}.bundle: missing")
    elif anchor is None and index_bundle.read_bytes() == PLACEHOLDER:
        result.legacy += 1  # before the first signed index
    else:
        problem = verify(index_bundle.read_bytes(), index_bytes, cosign)
        if problem:
            problems.append(f"{INDEX}.bundle: {problem}")
        else:
            result.valid += 1
            result.index_signed = True
            version = json.loads(index_bytes)["index_version"]
            # A different signed index at the same version counts as a rollback
            # too: a client that cached the other one would refuse it.
            if anchor is not None and (
                version < anchor.index_version or (version == anchor.index_version and index_bytes != anchor.index)
            ):
                problems.append(
                    f"{INDEX}: index_version {version} rolls back the signed index_version {anchor.index_version}"
                )

    # archives
    signed_urls = anchor.urls if anchor else {}
    for archive in archives:
        bundle_rel = f"{archive.path}.bundle"
        bundle = root / bundle_rel
        previously_signed = anchor is not None and archive.key in anchor.releases
        if previously_signed and (archive.path, archive.sha256, archive.size) not in anchor.releases[archive.key]:
            problems.append(f"{archive.path}: {archive.key} changed after it was signed; publish a new build")
            continue
        if signed_urls.get(archive.path, archive.key) != archive.key:
            problems.append(f"{archive.path}: already signed as {signed_urls[archive.path]}")
            continue
        if bundle.is_symlink():
            problems.append(f"{bundle_rel}: is a symlink")
            continue
        present = bundle.is_file()
        data = bundle.read_bytes() if present else None
        if bundle_rel in legacy and anchor is None and (not present or data == PLACEHOLDER):
            if legacy[bundle_rel] != archive.sha256:
                problems.append(f"{bundle_rel}: the legacy entry pins sha256 {legacy[bundle_rel]}, the archive is {archive.sha256}")
            else:
                result.legacy += 1
                result.to_sign.append(archive.path)
            continue
        if not present:
            if previously_signed:
                problems.append(f"{bundle_rel}: deleted after {archive.key} was signed")
            else:
                result.pending += 1
                result.to_sign.append(archive.path)
            continue
        problem = verify(data, (root / archive.path).read_bytes(), cosign)
        if problem:
            problems.append(f"{bundle_rel}: {problem}")
        else:
            result.valid += 1
    return result


def plan(root: Path, legacy: dict[str, str] | None = None, cosign: str | None = None) -> list[str]:
    """Archives that may be signed now: pending releases, and legacy entries
    before the first signed index. Refuses on any check problem, so a path
    that was signed before is never signed again."""
    result = check(root, legacy or {}, cosign)
    if result.problems:
        raise SigningError("refusing to sign: " + "; ".join(result.problems))
    return result.to_sign


def prune_legacy(root: Path, legacy_path: Path) -> list[str]:
    lines = legacy_path.read_text().splitlines()
    header = [line for line in lines if line.startswith("#")]
    remaining = []
    for line in lines:
        fields = line.split("#", 1)[0].split()
        if not fields:
            continue
        bundle = root / fields[0]
        payload = root / fields[0].removesuffix(".bundle")
        if bundle.is_file() and payload.is_file() and bundle_problem(bundle.read_bytes(), payload.read_bytes()) is None:
            continue
        remaining.append(line)
    legacy_path.write_text("\n".join(header + remaining) + "\n")
    return remaining


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--legacy", type=Path, help="the legacy placeholder list")
    common.add_argument("--cosign", help="cosign binary: also run cosign verify-blob")
    sub = parser.add_subparsers(dest="command", required=True)
    regen = sub.add_parser("regenerate-index", parents=[common])
    regen.add_argument("--now", required=True)
    sub.add_parser("plan", parents=[common])
    chk = sub.add_parser("check", parents=[common])
    chk.add_argument("--strict", action="store_true", help="also fail on legacy or pending unsigned bundles")
    sub.add_parser("prune-legacy", parents=[common])
    args = parser.parse_args(argv)
    root = args.repo_root
    try:
        legacy = read_legacy(args.legacy)
        if args.command == "regenerate-index":
            index = regenerate_index(root, parse_time(args.now), legacy, args.cosign)
            print(f"index_version {index['index_version']}, expires_at {index['expires_at']}")
        elif args.command == "plan":
            for path in plan(root, legacy, args.cosign):
                print(path)
        elif args.command == "check":
            result = check(root, legacy, args.cosign)
            for problem in result.problems:
                print(f"error: {problem}", file=sys.stderr)
            if result.problems:
                return 1
            if args.strict and (result.legacy or result.pending):
                print(f"error: {result.legacy} legacy and {result.pending} pending bundles are unsigned", file=sys.stderr)
                return 1
            if result.valid + result.legacy + result.pending == 0:
                print("error: no bundle checked", file=sys.stderr)
                return 1
            print(f"ok: {result.valid} valid bundles, {result.legacy} legacy placeholders, {result.pending} pending archives")
        elif args.command == "prune-legacy":
            if args.legacy is None:
                raise SigningError("prune-legacy needs --legacy")
            remaining = prune_legacy(root, args.legacy)
            print(f"{len(remaining)} legacy placeholder entries remain")
    except SigningError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
