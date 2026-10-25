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

Subcommands:

  regenerate-index --now <RFC 3339>   rebuild index.json from packages/*.json,
                                      bump index_version, set generated_at and
                                      expires_at (generated_at + 7 days)
  plan                                print the archives whose bundle is missing
                                      or a placeholder, one path per line
  check [--legacy FILE] [--cosign BIN] [--strict]
                                      fail unless every bundle is a valid
                                      bundle for its payload; paths listed in
                                      FILE may still be placeholders or
                                      missing, and archives of packages the
                                      signed index does not list yet may lack
                                      a bundle (--strict allows neither)
  prune-legacy --legacy FILE          drop the entries of FILE that now hold a
                                      valid bundle
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
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
    path: str  # relative to the repository root
    sha256: str
    size: int


def published_archives(root: Path) -> list[Archive]:
    """Every archive that packages/*.json lists, checked against its file."""
    archives = []
    for package_path in sorted((root / "packages").glob("*.json")):
        package = json.loads(package_path.read_bytes())
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
            archives.append(Archive(package_path.stem, url, version["sha256"], version["size"]))
    return archives


def regenerate_index(root: Path, now: dt.datetime, legacy: dict[str, str] | None = None, cosign: str | None = None) -> dict:
    index_path = root / INDEX
    previous = json.loads(index_path.read_bytes())
    if previous.get("schema") != 1:
        raise SigningError(f"unsupported index schema {previous.get('schema')!r}")
    if now <= parse_time(previous["generated_at"]):
        raise SigningError(f"--now {format_time(now)} is not after generated_at {previous['generated_at']}")
    published_archives(root)  # every record must match its archive before we sign over it
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
        "index_version": previous["index_version"] + 1,
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


def cosign_problem(cosign: str, bundle: Path, payload: Path) -> str | None:
    result = subprocess.run(
        [
            cosign,
            "verify-blob",
            "--bundle",
            str(bundle),
            "--certificate-identity",
            SUBJECT,
            "--certificate-oidc-issuer",
            ISSUER,
            str(payload),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"cosign verify-blob failed: {(result.stderr or result.stdout).strip()}"
    return None


def signing_targets(root: Path) -> list[str]:
    """Paths whose bundle is expected: index.json and every published archive."""
    return [INDEX] + [archive.path for archive in published_archives(root)]


def committed_bundles(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in (".git", ".worktrees")]
        for name in filenames:
            if name.endswith(".bundle"):
                found.append(Path(dirpath, name).relative_to(root).as_posix())
    return sorted(found)


def read_legacy(path: Path | None) -> dict[str, str]:
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


def find_anchor(root: Path, cosign: str | None = None) -> None:
    return None


def plan(root: Path, legacy: dict[str, str] | None = None, cosign: str | None = None) -> list[str]:
    """Archives to sign: those whose bundle is missing or the placeholder.

    Any other invalid bundle is an error, never re-signed: it means the archive
    or its bundle changed after signing.
    """
    targets = []
    for archive in published_archives(root):
        bundle = root / f"{archive.path}.bundle"
        if not bundle.exists() or bundle.read_bytes() == PLACEHOLDER:
            targets.append(archive.path)
            continue
        problem = bundle_problem(bundle.read_bytes(), (root / archive.path).read_bytes())
        if problem:
            raise SigningError(f"{archive.path}.bundle: {problem}; refusing to re-sign")
    return targets


def pending_packages(root: Path) -> set[str]:
    """Packages whose metadata the signed index does not list yet.

    Publish Package commits an archive and its packages/<name>.json; release.yml
    then regenerates the index and signs the archive. Until then the package
    file's hash differs from the index entry, and clients never reach the
    archive through a signed index.
    """
    listed = json.loads((root / INDEX).read_bytes()).get("packages", {})
    pending = set()
    for package_path in (root / "packages").glob("*.json"):
        data = package_path.read_bytes()
        entry = listed.get(package_path.stem)
        if entry != {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "url": f"packages/{package_path.stem}.json",
        }:
            pending.add(package_path.stem)
    return pending


@dataclass
class CheckResult:
    problems: list[str]
    valid: int = 0
    legacy: int = 0  # placeholders or missing bundles the legacy list allows
    pending: int = 0  # missing bundles of archives the signed index does not list yet
    anchor: None = None


def check(root: Path, legacy: dict[str, str], cosign: str | None = None) -> CheckResult:
    legacy = set(legacy)
    result = CheckResult([])
    problems = result.problems
    pending = pending_packages(root)
    pending_archives = {archive.path for archive in published_archives(root) if archive.package in pending}
    targets = signing_targets(root)
    expected = {f"{target}.bundle" for target in targets}
    for bundle_rel in committed_bundles(root):
        if bundle_rel not in expected:
            problems.append(f"{bundle_rel}: no index.json or published archive to sign")
    for target in targets:
        bundle_rel = f"{target}.bundle"
        bundle = root / bundle_rel
        if bundle.is_symlink():
            problems.append(f"{bundle_rel}: is a symlink")
            continue
        if not bundle.is_file():
            if bundle_rel in legacy:
                result.legacy += 1
            elif target in pending_archives:
                result.pending += 1
            else:
                problems.append(f"{bundle_rel}: missing")
            continue
        data = bundle.read_bytes()
        if data == PLACEHOLDER and bundle_rel in legacy:
            result.legacy += 1
            continue
        problem = bundle_problem(data, (root / target).read_bytes())
        if problem is None and cosign:
            problem = cosign_problem(cosign, bundle, root / target)
        if problem:
            problems.append(f"{bundle_rel}: {problem}")
        else:
            result.valid += 1
    for entry in sorted(legacy - expected):
        problems.append(f"legacy entry {entry}: not a bundle path this repository expects")
    return result


def prune_legacy(root: Path, legacy_path: Path) -> list[str]:
    remaining = []
    lines = legacy_path.read_text().splitlines()
    for entry in sorted(line.split()[0] for line in lines if line.strip() and not line.startswith("#")):
        bundle = root / entry
        payload = root / entry.removesuffix(".bundle")
        if bundle.is_file() and payload.is_file() and bundle_problem(bundle.read_bytes(), payload.read_bytes()) is None:
            continue
        remaining.append(entry)
    header = [
        line
        for line in legacy_path.read_text().splitlines()
        if line.startswith("#")
    ]
    legacy_path.write_text("\n".join(header + remaining) + "\n")
    return remaining


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)
    regen = sub.add_parser("regenerate-index")
    regen.add_argument("--now", required=True)
    sub.add_parser("plan")
    chk = sub.add_parser("check")
    chk.add_argument("--legacy", type=Path)
    chk.add_argument("--cosign")
    chk.add_argument("--strict", action="store_true", help="also fail on legacy or pending unsigned bundles")
    prune = sub.add_parser("prune-legacy")
    prune.add_argument("--legacy", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.repo_root
    try:
        if args.command == "regenerate-index":
            index = regenerate_index(root, parse_time(args.now))
            print(f"index_version {index['index_version']}, expires_at {index['expires_at']}")
        elif args.command == "plan":
            for path in plan(root):
                print(path)
        elif args.command == "check":
            result = check(root, read_legacy(args.legacy), args.cosign)
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
            remaining = prune_legacy(root, args.legacy)
            print(f"{len(remaining)} legacy placeholder entries remain")
    except SigningError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
