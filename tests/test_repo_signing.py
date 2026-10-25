"""Tests of tools/repo_signing.py: index regeneration, the signing plan and the bundle check.

The bundles here are synthetic: a self-signed certificate that carries the Fulcio identity
extensions, in the Sigstore bundle v0.3 layout cosign v3 writes. They exercise the shape,
digest and identity checks. The cryptographic checks (certificate chain, signature,
transparency log) belong to `cosign verify-blob`, which `check --cosign` runs; here a stub
stands in for it.

Needs `cryptography` (CI installs cryptography==46.0.3).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "repo_signing.py"
LEGACY = ROOT / "tools" / "legacy_placeholder_bundles.txt"

spec = importlib.util.spec_from_file_location("repo_signing", TOOL)
rs = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["repo_signing"] = rs  # dataclasses look the module up while it loads
spec.loader.exec_module(rs)

SUBJECT = rs.SUBJECT
ISSUER = rs.ISSUER


def der_utf8(text: str) -> bytes:
    raw = text.encode()
    assert len(raw) < 128
    return bytes([0x0C, len(raw)]) + raw


def certificate(subject: str = SUBJECT, issuer: str | None = ISSUER, issuer_v1: str | None = None) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "sigstore.dev")])
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(minutes=10))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(subject)]), critical=True
        )
    )
    if issuer is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(rs.OID_ISSUER_V2), der_utf8(issuer)),
            critical=False,
        )
    if issuer_v1 is not None:
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(rs.OID_ISSUER_V1), issuer_v1.encode()),
            critical=False,
        )
    return builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def fake_signature(payload: bytes, subject: str = SUBJECT, issuer: str = ISSUER) -> str:
    """What the fake cosign below signs and verifies: bound to the payload and the identity."""
    return base64.b64encode(hashlib.sha256(f"fake-cosign|{subject}|{issuer}|".encode() + payload).digest()).decode()


def bundle(payload: bytes, **overrides) -> dict:
    cert = overrides.pop("cert", None) or certificate()
    signer = overrides.pop("signer", (SUBJECT, ISSUER))
    data = {
        "mediaType": rs.MEDIA_TYPE,
        "verificationMaterial": {
            "certificate": {"rawBytes": base64.b64encode(cert).decode()},
            "tlogEntries": [
                {
                    "logIndex": "1",
                    "logId": {"keyId": "wNI9atQGlz+VWfO6LRygH4QUfY/8W4RFwiT5i5WRgB0="},
                    "kindVersion": {"kind": "hashedrekord", "version": "0.0.1"},
                    "integratedTime": "1792703036",
                    "inclusionPromise": {"signedEntryTimestamp": "AAAA"},
                    "inclusionProof": {"logIndex": "1", "rootHash": "AAAA", "treeSize": "2", "hashes": [], "checkpoint": {"envelope": "x"}},
                    "canonicalizedBody": "AAAA",
                }
            ],
        },
        "messageSignature": {
            "messageDigest": {
                "algorithm": "SHA2_256",
                "digest": base64.b64encode(hashlib.sha256(payload).digest()).decode(),
            },
            "signature": fake_signature(payload, *signer),
        },
    }
    data.update(overrides)
    return data


def encode(value: dict) -> bytes:
    return json.dumps(value).encode()


class BundleProblemTest(unittest.TestCase):
    payload = b'{"schema": 1}\n'

    def test_valid_bundle_passes(self) -> None:
        self.assertIsNone(rs.bundle_problem(encode(bundle(self.payload)), self.payload))

    def test_fulcio_v1_issuer_extension_is_read(self) -> None:
        cert = certificate(issuer=None, issuer_v1=ISSUER)
        self.assertIsNone(rs.bundle_problem(encode(bundle(self.payload, cert=cert)), self.payload))

    def test_rejections(self) -> None:
        payload = self.payload
        good = bundle(payload)
        cases = {
            "placeholder": rs.PLACEHOLDER,
            "empty": b"",
            "not an object": b"[]",
            "v0.2 media type": encode({**good, "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.2"}),
            "dsse": encode({**{k: v for k, v in good.items() if k != "messageSignature"}, "dsseEnvelope": {}}),
            "dsse and message": encode({**good, "dsseEnvelope": {}}),
            "sha512": encode({**good, "messageSignature": {**good["messageSignature"], "messageDigest": {"algorithm": "SHA2_512", "digest": good["messageSignature"]["messageDigest"]["digest"]}}}),
            "other payload": encode(bundle(b"other")),
            "no signature": encode({**good, "messageSignature": {**good["messageSignature"], "signature": ""}}),
            "no tlog": encode({**good, "verificationMaterial": {**good["verificationMaterial"], "tlogEntries": []}}),
            "no proof": encode({**good, "verificationMaterial": {**good["verificationMaterial"], "tlogEntries": [{**good["verificationMaterial"]["tlogEntries"][0], "inclusionProof": None}]}}),
            "rekor v2 entry": encode({**good, "verificationMaterial": {**good["verificationMaterial"], "tlogEntries": [{**good["verificationMaterial"]["tlogEntries"][0], "kindVersion": {"kind": "hashedrekord", "version": "0.0.2"}}]}}),
            "public key, no certificate": encode({**good, "verificationMaterial": {**good["verificationMaterial"], "certificate": None, "publicKey": {"hint": "x"}}}),
            "wrong subject": encode(bundle(payload, cert=certificate(subject=SUBJECT.replace("refs/heads/main", "refs/heads/dev")))),
            "publish-package subject": encode(bundle(payload, cert=certificate(subject=SUBJECT.replace("release.yml", "publish-package.yml")))),
            "wrong issuer": encode(bundle(payload, cert=certificate(issuer="https://accounts.google.com"))),
            "no issuer": encode(bundle(payload, cert=certificate(issuer=None))),
            "garbage certificate": encode({**good, "verificationMaterial": {**good["verificationMaterial"], "certificate": {"rawBytes": "AAAA"}}}),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(rs.bundle_problem(data, payload))

    def test_tampered_payload_is_rejected(self) -> None:
        data = encode(bundle(self.payload))
        for payload in [self.payload + b"\n", self.payload[:-1], b""]:
            self.assertEqual(rs.bundle_problem(data, payload), "message digest does not match the payload")


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sigstore"
GORELEASER_SUBJECT = "https://github.com/goreleaser/goreleaser/.github/workflows/release.yml@refs/tags/v2.19.0"


class RealBundleTest(unittest.TestCase):
    """A real GitHub Actions keyless bundle from cosign v3.0.6 (see fixtures/sigstore/README.md)."""

    payload = (FIXTURES / "goreleaser-v2.19.0-checksums.txt").read_bytes()
    bundle = (FIXTURES / "goreleaser-v2.19.0-checksums.txt.sigstore.json").read_bytes()

    def test_accepted_for_its_own_identity(self) -> None:
        with mock.patch.object(rs, "SUBJECT", GORELEASER_SUBJECT):
            self.assertIsNone(rs.bundle_problem(self.bundle, self.payload))

    def test_rejected_for_the_yurt_packages_identity(self) -> None:
        self.assertEqual(
            rs.bundle_problem(self.bundle, self.payload),
            f"certificate subject is ['{GORELEASER_SUBJECT}'], expected {SUBJECT}",
        )

    def test_rejected_for_another_payload(self) -> None:
        with mock.patch.object(rs, "SUBJECT", GORELEASER_SUBJECT):
            self.assertEqual(
                rs.bundle_problem(self.bundle, self.payload + b"\n"),
                "message digest does not match the payload",
            )


def write_package(root: Path, name: str, versions: list[tuple[str, str, bytes]]) -> None:
    records = []
    for version, build, data in versions:
        rel = f"artifacts/{name}/{version}/{name}-{version}-{build}.yurtpkg"
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
        records.append(
            {
                "version": version,
                "build": build,
                "url": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "signing": {"subject": SUBJECT, "issuer": ISSUER},
                "depends": [],
                "yanked": False,
            }
        )
    (root / "packages").mkdir(exist_ok=True)
    (root / "packages" / f"{name}.json").write_bytes(rs.dump_json({"name": name, "versions": records}))


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


FAKE_COSIGN = None  # set in RepoTest.setUpClass


class RepoTest(unittest.TestCase):
    """A git repository shaped like yurt-packages main before the first Release run."""

    A = "artifacts/busybox/1.37.0/busybox-1.37.0-yurt_0.yurtpkg"
    B = "artifacts/busybox/1.37.0/busybox-1.37.0-yurt_1.yurtpkg"
    C = "artifacts/zsh/5.9.0/zsh-5.9.0-yurt_0.yurtpkg"

    @classmethod
    def setUpClass(cls) -> None:
        cls.bin = tempfile.TemporaryDirectory()
        script = Path(cls.bin.name, "cosign")
        script.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{Path(__file__).resolve()}' --fake-cosign \"$@\"\n")
        script.chmod(0o755)
        cls.cosign = str(script)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.bin.cleanup()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        write_package(self.root, "zsh", [("5.9.0", "yurt_0", b"zsh archive")])
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"busybox"), ("1.37.0", "yurt_1", b"busybox 1")])
        index = {
            "schema": 1,
            "index_version": 17,
            "generated_at": "2026-09-22T06:12:06Z",
            "expires_at": "2026-09-29T06:12:06Z",
            "packages": {},
        }
        for name in ["busybox", "zsh"]:
            data = (self.root / "packages" / f"{name}.json").read_bytes()
            index["packages"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "url": f"packages/{name}.json"}
        (self.root / "index.json").write_bytes(rs.dump_json(index))
        (self.root / "index.json.bundle").write_bytes(rs.PLACEHOLDER)
        for rel in [self.A, self.B]:  # C has no bundle, like pkg-0.1.1-2 on main
            (self.root / f"{rel}.bundle").write_bytes(rs.PLACEHOLDER)
        self.legacy_path = self.root / "legacy.txt"
        self.legacy_path.write_text(
            "# header\n" + "".join(f"{rel}.bundle {self.sha(rel)}\n" for rel in [self.A, self.B, self.C])
        )
        self.commit("seed")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def sha(self, rel: str) -> str:
        return hashlib.sha256((self.root / rel).read_bytes()).hexdigest()

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)

    def legacy(self) -> dict[str, str]:
        return rs.read_legacy(self.legacy_path)

    def sign(self, rel: str, signer: tuple[str, str] = (SUBJECT, ISSUER)) -> None:
        payload = (self.root / rel).read_bytes()
        (self.root / f"{rel}.bundle").write_bytes(encode(bundle(payload, signer=signer)))

    def release(self, now: str = "2026-10-26T00:00:00Z") -> None:
        """What release.yml does, with the fake cosign."""
        to_sign = rs.plan(self.root, self.legacy(), self.cosign)
        rs.regenerate_index(self.root, rs.parse_time(now), self.legacy(), self.cosign)
        for rel in to_sign:
            self.sign(rel)
        self.sign("index.json")
        result = rs.check(self.root, {}, self.cosign)
        self.assertEqual(result.problems, [])
        self.assertEqual((result.legacy, result.pending), (0, 0))
        rs.prune_legacy(self.root, self.legacy_path)
        self.commit("release")

    def publish(self, build: str, data: bytes) -> str:
        """What Publish Package commits: the archive and its metadata, no bundle, no index."""
        package = json.loads((self.root / "packages" / "busybox.json").read_bytes())
        versions = [(v["version"], v["build"], (self.root / v["url"]).read_bytes()) for v in package["versions"]]
        write_package(self.root, "busybox", versions + [("1.37.0", build, data)])
        self.commit(f"publish busybox {build}")
        return f"artifacts/busybox/1.37.0/busybox-1.37.0-{build}.yurtpkg"

    def problems(self, legacy: dict[str, str] | None = None) -> list[str]:
        return rs.check(self.root, self.legacy() if legacy is None else legacy, self.cosign).problems

    # before the first signed index

    def test_first_run_state_checks_clean(self) -> None:
        result = rs.check(self.root, self.legacy(), self.cosign)
        self.assertEqual(result.problems, [])
        self.assertIsNone(result.anchor)
        self.assertEqual((result.valid, result.legacy, result.pending), (0, 4, 0))
        self.assertEqual(rs.plan(self.root, self.legacy(), self.cosign), [self.A, self.B, self.C])

    def test_legacy_entry_is_pinned_to_its_archive_bytes(self) -> None:
        (self.root / self.A).write_bytes(b"swapped")
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"swapped"), ("1.37.0", "yurt_1", b"busybox 1")])
        self.commit("swap a legacy archive")
        self.assertEqual(len(self.problems()), 1)
        self.assertIn("the legacy entry pins sha256", self.problems()[0])
        with self.assertRaisesRegex(rs.SigningError, "refusing to sign"):
            rs.plan(self.root, self.legacy(), self.cosign)

    def test_placeholder_needs_a_legacy_entry(self) -> None:
        self.assertEqual(
            self.problems({}),
            [f"{self.A}.bundle: not a JSON Sigstore bundle", f"{self.B}.bundle: not a JSON Sigstore bundle"],
        )

    # the first release

    def test_first_release_signs_everything_and_empties_the_legacy_list(self) -> None:
        self.release()
        self.assertEqual(self.legacy_path.read_text(), "# header\n")
        result = rs.check(self.root, {}, self.cosign)
        self.assertEqual(result.problems, [])
        self.assertIsNotNone(result.anchor)
        self.assertEqual(result.anchor.index_version, 18)
        self.assertEqual((result.valid, result.legacy, result.pending), (4, 0, 0))
        self.assertEqual(rs.plan(self.root, {}, self.cosign), [])

    # F1: no re-sign of a release the signed index lists

    def test_swapped_archive_with_updated_record_and_deleted_bundle_is_refused(self) -> None:
        self.release()
        (self.root / f"{self.A}.bundle").unlink()
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"evil"), ("1.37.0", "yurt_1", b"busybox 1")])
        self.commit("swap busybox yurt_0")
        problems = self.problems({})
        self.assertEqual(len(problems), 1)
        self.assertIn("changed after it was signed", problems[0])
        with self.assertRaisesRegex(rs.SigningError, "refusing to sign"):
            rs.plan(self.root, {}, self.cosign)
        with self.assertRaisesRegex(rs.SigningError, "refusing to regenerate"):
            rs.regenerate_index(self.root, rs.parse_time("2026-10-27T00:00:00Z"), {}, self.cosign)

    def test_deleting_a_signed_bundle_is_refused(self) -> None:
        self.release()
        (self.root / f"{self.A}.bundle").unlink()
        self.commit("delete a bundle")
        self.assertEqual(self.problems({}), [f"{self.A}.bundle: deleted after ('busybox', '1.37.0', 'yurt_0') was signed"])
        with self.assertRaises(rs.SigningError):
            rs.plan(self.root, {}, self.cosign)

    def test_moving_a_signed_archive_under_a_new_release_name_is_refused(self) -> None:
        self.release()
        package = json.loads((self.root / "packages" / "busybox.json").read_bytes())
        package["versions"][0]["build"] = "yurt_9"  # same url, new key
        (self.root / "packages" / "busybox.json").write_bytes(rs.dump_json(package))
        (self.root / f"{self.A}.bundle").unlink()
        self.commit("rename a release")
        self.assertEqual(len(self.problems({})), 1)
        self.assertIn("already signed as", self.problems({})[0])

    def test_a_new_release_is_pending_and_gets_signed(self) -> None:
        self.release()
        new = self.publish("yurt_2", b"busybox 2")
        result = rs.check(self.root, {}, self.cosign)
        self.assertEqual(result.problems, [])
        self.assertEqual((result.valid, result.pending), (4, 1))
        self.assertEqual(rs.plan(self.root, {}, self.cosign), [new])
        self.release("2026-10-27T00:00:00Z")
        self.assertEqual(rs.check(self.root, {}, self.cosign).anchor.index_version, 19)
        # Signed now, so a deleted bundle is refused.
        (self.root / f"{new}.bundle").unlink()
        self.assertIn("deleted after", self.problems({})[0])

    def test_a_pending_placeholder_is_refused(self) -> None:
        self.release()
        new = self.publish("yurt_2", b"busybox 2")
        (self.root / f"{new}.bundle").write_bytes(rs.PLACEHOLDER)
        self.assertEqual(self.problems({}), [f"{new}.bundle: not a JSON Sigstore bundle"])

    # N1: every release any signed index ever listed stays protected

    def test_dropped_then_re_added_release_is_refused(self) -> None:
        self.release()  # v18 lists busybox yurt_1 (B)
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"busybox")])
        (self.root / self.B).unlink()
        (self.root / f"{self.B}.bundle").unlink()
        self.commit("drop busybox yurt_1")
        self.release("2026-10-27T00:00:00Z")  # v19 does not list it
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"busybox"), ("1.37.0", "yurt_1", b"other bytes")])
        self.commit("re-add busybox yurt_1 with new bytes")
        problems = self.problems({})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("changed after it was signed", problems[0])
        with self.assertRaisesRegex(rs.SigningError, "refusing to sign"):
            rs.plan(self.root, {}, self.cosign)

    def test_a_dropped_release_re_added_unchanged_needs_its_old_bundle(self) -> None:
        self.release()
        old_bundle = (self.root / f"{self.B}.bundle").read_bytes()
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"busybox")])
        (self.root / self.B).unlink()
        (self.root / f"{self.B}.bundle").unlink()
        self.commit("drop busybox yurt_1")
        self.release("2026-10-27T00:00:00Z")
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"busybox"), ("1.37.0", "yurt_1", b"busybox 1")])
        self.commit("re-add busybox yurt_1 without its bundle")
        self.assertIn("deleted after", self.problems({})[0])
        with self.assertRaises(rs.SigningError):
            rs.plan(self.root, {}, self.cosign)
        (self.root / f"{self.B}.bundle").write_bytes(old_bundle)
        self.commit("restore its bundle")
        self.assertEqual(self.problems({}), [])

    # N2: a merge that hides the newer signed line is still a rollback

    def test_ours_merge_hiding_newer_signed_indexes_is_a_rollback(self) -> None:
        self.release()  # v18
        git(self.root, "branch", "old")
        self.release("2026-10-27T00:00:00Z")  # v19
        self.release("2026-10-28T00:00:00Z")  # v20
        git(self.root, "checkout", "-q", "old")
        git(self.root, "merge", "-q", "-s", "ours", "-m", "merge main, keep v18", "main")
        git(self.root, "checkout", "-q", "-B", "main")
        self.assertEqual(json.loads((self.root / "index.json").read_bytes())["index_version"], 18)
        self.assertEqual(self.problems({}), ["index.json: index_version 18 rolls back the signed index_version 20"])
        with self.assertRaises(rs.SigningError):
            rs.regenerate_index(self.root, rs.parse_time("2026-10-29T00:00:00Z"), {}, self.cosign)

    # N3: a commit re-holding a signed index beside other package files never wedges

    def test_restored_index_bundle_beside_changed_metadata_does_not_wedge(self) -> None:
        self.release()
        good_bundle = (self.root / "index.json.bundle").read_bytes()
        good_package = (self.root / "packages" / "busybox.json").read_bytes()
        (self.root / "index.json.bundle").unlink()
        self.commit("delete the index bundle")
        (self.root / "index.json.bundle").write_bytes(good_bundle)
        package = json.loads(good_package)
        package["versions"][0]["yanked"] = True
        (self.root / "packages" / "busybox.json").write_bytes(rs.dump_json(package))
        self.commit("restore the bundle, with changed metadata")
        (self.root / "packages" / "busybox.json").write_bytes(good_package)
        self.commit("restore the metadata")
        self.assertEqual(self.problems({}), [])
        self.assertEqual(rs.plan(self.root, {}, self.cosign), [])
        self.release("2026-10-27T00:00:00Z")

    def test_restored_index_bundle_beside_an_added_release_does_not_wedge(self) -> None:
        self.release()
        good_bundle = (self.root / "index.json.bundle").read_bytes()
        (self.root / "index.json.bundle").unlink()
        self.commit("delete the index bundle")
        (self.root / "index.json.bundle").write_bytes(good_bundle)
        new = self.publish("yurt_2", b"busybox 2")  # commits the restored bundle beside a new release
        result = rs.check(self.root, {}, self.cosign)
        self.assertEqual(result.problems, [])
        self.assertEqual(rs.plan(self.root, {}, self.cosign), [new])
        self.release("2026-10-27T00:00:00Z")

    # R3-1: a cosign tool error aborts; only a verification failure drops a candidate

    def test_cosign_tool_error_on_a_historical_index_aborts(self) -> None:
        self.release()  # v18
        v18 = hashlib.sha256((self.root / "index.json").read_bytes()).hexdigest()
        self.release("2026-10-27T00:00:00Z")  # v19
        os.environ["FAKE_COSIGN_TOOL_ERROR_FOR"] = v18
        self.addCleanup(os.environ.pop, "FAKE_COSIGN_TOOL_ERROR_FOR", None)
        for name, call in {
            "check": lambda: rs.check(self.root, {}, self.cosign),
            "plan": lambda: rs.plan(self.root, {}, self.cosign),
            "regenerate": lambda: rs.regenerate_index(self.root, rs.parse_time("2026-10-28T00:00:00Z"), {}, self.cosign),
        }.items():
            with self.subTest(name), self.assertRaisesRegex(rs.SigningError, "cosign could not verify .*TUF remote mirror"):
                call()
        result = subprocess.run(
            [sys.executable, str(TOOL), "--repo-root", str(self.root), "check", "--cosign", self.cosign],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("cosign could not verify", result.stderr)

    def test_cosign_tool_error_on_a_current_bundle_aborts(self) -> None:
        self.release()
        os.environ["FAKE_COSIGN_TOOL_ERROR_FOR"] = self.sha(self.A)
        self.addCleanup(os.environ.pop, "FAKE_COSIGN_TOOL_ERROR_FOR", None)
        with self.assertRaisesRegex(rs.SigningError, "cosign could not verify"):
            rs.check(self.root, {}, self.cosign)

    def test_missing_cosign_aborts(self) -> None:
        self.release()
        with self.assertRaisesRegex(rs.SigningError, "cosign could not verify"):
            rs.check(self.root, {}, str(self.root / "no-such-cosign"))

    # P1: a malformed bundle in history is invalid, not a cosign tool error

    def test_malformed_bundle_in_history_is_skipped_not_a_wedge(self) -> None:
        self.release()  # v18
        good_index = (self.root / "index.json").read_bytes()
        good_bundle = (self.root / "index.json.bundle").read_bytes()
        index = json.loads(good_index)
        index["index_version"] = 50
        (self.root / "index.json").write_bytes(rs.dump_json(index))
        malformed = bundle((self.root / "index.json").read_bytes())
        malformed["extra"] = 1  # passes the shape check, cosign rejects it
        (self.root / "index.json.bundle").write_bytes(encode(malformed))
        self.commit("a malformed index bundle")
        self.assertIsNone(rs.bundle_problem(encode(malformed), (self.root / "index.json").read_bytes()))
        problems = self.problems({})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("index.json.bundle: cosign verify-blob failed: Error: bundle does not contain cert", problems[0])
        (self.root / "index.json").write_bytes(good_index)
        (self.root / "index.json.bundle").write_bytes(good_bundle)
        self.commit("restore the signed index")
        self.assertEqual(self.problems({}), [])
        self.assertEqual(rs.find_anchor(self.root, self.cosign).index_version, 18)
        self.release("2026-10-27T00:00:00Z")

    def test_cosign_that_fails_its_health_check_aborts(self) -> None:
        self.release()
        os.environ["FAKE_COSIGN_BROKEN"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_COSIGN_BROKEN", None)
        for name, call in {
            "check": lambda: rs.check(self.root, {}, self.cosign),
            "plan": lambda: rs.plan(self.root, {}, self.cosign),
            "regenerate": lambda: rs.regenerate_index(self.root, rs.parse_time("2026-10-28T00:00:00Z"), {}, self.cosign),
        }.items():
            with self.subTest(name), self.assertRaisesRegex(rs.SigningError, "cosign health check failed"):
                call()

    def test_cosign_of_another_version_aborts(self) -> None:
        self.release()
        os.environ["FAKE_COSIGN_VERSION"] = "v3.1.4"
        self.addCleanup(os.environ.pop, "FAKE_COSIGN_VERSION", None)
        with self.assertRaisesRegex(rs.SigningError, "cosign v3.0.6 is required"):
            rs.check(self.root, {}, self.cosign)

    # P2: package names

    def test_package_blobs_reads_non_ascii_paths(self) -> None:
        (self.root / "packages" / "p\u00e4ck.json").write_bytes(b"{}\n")
        self.commit("a non-ASCII file name")
        head = git(self.root, "rev-parse", "HEAD").strip()
        self.assertIn("packages/p\u00e4ck.json", rs.package_blobs(self.root, head))

    def test_non_ascii_package_name_is_refused(self) -> None:
        for name in ["p\u00e4ck", "Pkg", "-pkg", "pkg name"]:
            with self.subTest(name):
                write_package(self.root, name, [("1.0.0", "1", b"x")])
                with self.assertRaisesRegex(rs.SigningError, "invalid package name"):
                    rs.check(self.root, self.legacy(), self.cosign)
                (self.root / "packages" / f"{name}.json").unlink()

    # F2: the legacy list is dead once an index is signed

    def test_legacy_entry_re_added_after_the_first_release_is_refused(self) -> None:
        self.release()
        (self.root / f"{self.A}.bundle").write_bytes(rs.PLACEHOLDER)
        self.legacy_path.write_text(f"# header\n{self.A}.bundle {self.sha(self.A)}\n")
        self.commit("re-add a legacy entry")
        problems = self.problems()
        self.assertIn("legacy list must be empty once a signed index.json exists", problems)
        with self.assertRaises(rs.SigningError):
            rs.plan(self.root, self.legacy(), self.cosign)

    def test_deleting_the_index_bundle_does_not_reopen_the_first_run(self) -> None:
        self.release()
        (self.root / "index.json.bundle").write_bytes(rs.PLACEHOLDER)
        self.commit("placeholder index bundle")
        self.assertEqual(self.problems({}), ["index.json.bundle: not a JSON Sigstore bundle"])
        with self.assertRaisesRegex(rs.SigningError, "refusing to regenerate"):
            rs.regenerate_index(self.root, rs.parse_time("2026-10-27T00:00:00Z"), {}, self.cosign)

    # F3: regenerate-index starts from the signed index only

    def test_regenerate_refuses_an_index_edited_after_signing(self) -> None:
        self.release()
        index = json.loads((self.root / "index.json").read_bytes())
        index["index_version"] = 3
        (self.root / "index.json").write_bytes(rs.dump_json(index))
        self.commit("lower the index version")
        with self.assertRaisesRegex(rs.SigningError, "message digest does not match"):
            rs.regenerate_index(self.root, rs.parse_time("2026-10-27T00:00:00Z"), {}, self.cosign)

    def test_restoring_an_older_signed_index_is_a_rollback(self) -> None:
        self.release()
        old_index = (self.root / "index.json").read_bytes()
        old_bundle = (self.root / "index.json.bundle").read_bytes()
        self.release("2026-10-27T00:00:00Z")
        (self.root / "index.json").write_bytes(old_index)
        (self.root / "index.json.bundle").write_bytes(old_bundle)
        self.commit("restore index_version 18")
        self.assertEqual(self.problems({}), ["index.json: index_version 18 rolls back the signed index_version 19"])
        with self.assertRaises(rs.SigningError):
            rs.regenerate_index(self.root, rs.parse_time("2026-10-28T00:00:00Z"), {}, self.cosign)

    def test_regenerate_bumps_above_the_signed_version(self) -> None:
        self.release()
        index = rs.regenerate_index(self.root, rs.parse_time("2026-10-27T12:00:00Z"), {}, self.cosign)
        self.assertEqual(index["index_version"], 19)
        self.assertEqual(index["generated_at"], "2026-10-27T12:00:00Z")
        self.assertEqual(index["expires_at"], "2026-11-03T12:00:00Z")
        self.assertEqual(list(index), ["schema", "index_version", "generated_at", "expires_at", "packages"])
        package = (self.root / "packages" / "zsh.json").read_bytes()
        self.assertEqual(
            index["packages"]["zsh"],
            {"sha256": hashlib.sha256(package).hexdigest(), "size": len(package), "url": "packages/zsh.json"},
        )

    def test_regenerate_refuses_time_that_does_not_advance(self) -> None:
        for now in ["2026-09-22T06:12:06Z", "2026-09-01T00:00:00Z"]:
            with self.subTest(now), self.assertRaises(rs.SigningError):
                rs.regenerate_index(self.root, rs.parse_time(now), self.legacy(), self.cosign)

    def test_regenerate_refuses_a_record_that_does_not_match_its_archive(self) -> None:
        (self.root / self.B).write_bytes(b"replaced")
        with self.assertRaisesRegex(rs.SigningError, "does not match its package record"):
            rs.regenerate_index(self.root, rs.parse_time("2026-10-25T00:00:00Z"), self.legacy(), self.cosign)

    # cosign: identity bound, and a forged anchor does not count

    def test_cosign_rejects_a_bundle_from_another_identity(self) -> None:
        self.release()
        other = (SUBJECT.replace("refs/heads/main", "refs/heads/dev"), ISSUER)
        self.sign(self.A, signer=other)  # right certificate shape, wrong signer
        self.assertIsNone(rs.bundle_problem((self.root / f"{self.A}.bundle").read_bytes(), (self.root / self.A).read_bytes()))
        problems = self.problems({})
        self.assertEqual(len(problems), 1)
        self.assertIn("cosign verify-blob failed: Error: failed to verify signature: identity mismatch", problems[0])

    def test_a_forged_index_bundle_in_history_is_not_an_anchor(self) -> None:
        index = json.loads((self.root / "index.json").read_bytes())
        index["index_version"] = 1000
        (self.root / "index.json").write_bytes(rs.dump_json(index))
        self.sign("index.json", signer=("https://example.com/forger", ISSUER))
        self.commit("forged index")
        self.assertIsNotNone(rs.find_anchor(self.root))  # the shape alone accepts it
        self.assertIsNone(rs.find_anchor(self.root, self.cosign))

    def test_shallow_clone_is_refused(self) -> None:
        self.release()
        clone = self.root.parent / f"{self.root.name}-shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.root}", str(clone)], check=True)
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(clone)]))
        with self.assertRaisesRegex(rs.SigningError, "shallow clone"):
            rs.check(clone, {}, self.cosign)

    # other shape rules

    def test_stray_bundles_and_unknown_legacy_entries(self) -> None:
        (self.root / "artifacts" / "orphan.yurtpkg.bundle").write_bytes(rs.PLACEHOLDER)
        legacy = {**self.legacy(), "artifacts/gone.yurtpkg.bundle": "0" * 64}
        self.assertEqual(
            self.problems(legacy),
            [
                "artifacts/orphan.yurtpkg.bundle: no index.json or published archive to sign",
                "legacy entry artifacts/gone.yurtpkg.bundle: not a bundle path this repository expects",
            ],
        )

    def test_a_symlinked_bundle_is_rejected(self) -> None:
        self.release()
        (self.root / f"{self.A}.bundle").unlink()
        (self.root / f"{self.A}.bundle").symlink_to(self.root / "index.json.bundle")
        self.assertEqual(self.problems({}), [f"{self.A}.bundle: is a symlink"])

    def test_published_archives_refuses_bad_records(self) -> None:
        package = self.root / "packages" / "zsh.json"
        record = json.loads(package.read_bytes())
        for name, change in {
            "escape": {"url": "artifacts/../index.json"},
            "outside artifacts": {"url": "index.json"},
            "other signer": {"signing": {"subject": SUBJECT.replace("release", "publish-package"), "issuer": ISSUER}},
            "missing archive": {"url": "artifacts/zsh/5.9.0/zsh-5.9.0-yurt_9.yurtpkg"},
        }.items():
            with self.subTest(name):
                bad = {**record, "versions": [{**record["versions"][0], **change}]}
                package.write_bytes(rs.dump_json(bad))
                with self.assertRaises(rs.SigningError):
                    rs.published_archives(self.root)

    def test_legacy_file_format(self) -> None:
        self.legacy_path.write_text(f"{self.A}.bundle\n")
        with self.assertRaisesRegex(rs.SigningError, "expected '<bundle path> <archive sha256>'"):
            self.legacy()

    def test_prune_keeps_the_header_and_drops_signed_entries(self) -> None:
        self.sign(self.A)
        self.assertEqual(
            rs.prune_legacy(self.root, self.legacy_path),
            [f"{self.B}.bundle {self.sha(self.B)}", f"{self.C}.bundle {self.sha(self.C)}"],
        )
        self.assertEqual(
            self.legacy_path.read_text(),
            f"# header\n{self.B}.bundle {self.sha(self.B)}\n{self.C}.bundle {self.sha(self.C)}\n",
        )

    # the CLI, end to end as release.yml drives it, with the identity-checking fake cosign

    def test_release_sequence_through_the_cli(self) -> None:
        legacy = str(self.legacy_path)

        def run(*args: str, code: int = 0) -> str:
            result = subprocess.run(
                [sys.executable, str(TOOL), "--repo-root", str(self.root), *args],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
            return result.stdout + result.stderr

        self.assertIn("ok: 0 valid bundles, 4 legacy placeholders, 0 pending archives", run("check", "--legacy", legacy, "--cosign", self.cosign))
        self.assertIn("4 legacy and 0 pending bundles are unsigned", run("check", "--strict", "--legacy", legacy, code=1))
        to_sign = run("plan", "--legacy", legacy, "--cosign", self.cosign).split()
        self.assertEqual(to_sign, [self.A, self.B, self.C])
        run("regenerate-index", "--legacy", legacy, "--cosign", self.cosign, "--now", "2026-10-25T00:00:00Z")
        for rel in to_sign + ["index.json"]:
            subprocess.run([self.cosign, "sign-blob", "--yes", "--bundle", str(self.root / f"{rel}.bundle"), str(self.root / rel)], check=True)
        self.assertIn("ok: 4 valid bundles, 0 legacy placeholders, 0 pending archives", run("check", "--strict", "--cosign", self.cosign))
        run("prune-legacy", "--legacy", legacy)
        self.assertEqual(self.legacy_path.read_text(), "# header\n")
        self.commit("release")
        self.assertIn("ok: 4 valid bundles", run("check", "--legacy", legacy, "--cosign", self.cosign))
        # A bundle signed by another identity passes the shape check but not cosign.
        subprocess.run(
            [self.cosign, "sign-blob", "--yes", "--bundle", str(self.root / f"{self.A}.bundle"), str(self.root / self.A)],
            check=True,
            env={**os.environ, "FAKE_COSIGN_SUBJECT": "https://example.com/other"},
        )
        self.assertIn("ok: 4 valid bundles", run("check"))
        self.assertIn("identity mismatch", run("check", "--cosign", self.cosign, code=1))


class CommittedRepositoryTest(unittest.TestCase):
    """The CI guard on the repository as committed (CI also runs it with cosign)."""

    def test_committed_bundles_pass_the_check(self) -> None:
        result = rs.check(ROOT, rs.read_legacy(LEGACY))
        self.assertEqual(result.problems, [])
        self.assertGreater(result.valid + result.legacy + result.pending, 0)

    def test_legacy_list_only_pins_archives_that_existed_before_release_yml(self) -> None:
        allowed = {
            "artifacts/busybox/1.37.0/busybox-1.37.0-yurt_0.yurtpkg.bundle": "41cfd7e93bdcfc3b05f8af4d319f2eac2c253d3b7d49765163dd71945674b2dd",
            "artifacts/clang/23.0.0/clang-23.0.0-yurt_0.yurtpkg.bundle": "0b41781e6bd9ed229872cfee2270c8c599603a86eeec466fefc381446825dc4f",
            "artifacts/libcxx/22.1.0/libcxx-22.1.0-1.yurtpkg.bundle": "b42d1d58e20c3f425e88699e8d1e549f1afe74477b72873c053789180b167df9",
            "artifacts/ncurses/6.5.0/ncurses-6.5.0-yurt_0.yurtpkg.bundle": "533a73fc3cbadde2107d0d7f62d984e14885c33ac232867e9b956ea3aeda1026",
            "artifacts/openssl/4.0.0/openssl-4.0.0-1.yurtpkg.bundle": "0a36496180d78739bff7dedf4efce526063a620fbdaed06ab4a2ef5f3b1c9894",
            "artifacts/pkg/0.1.0/pkg-0.1.0-yurt_0.yurtpkg.bundle": "5b02418555dc098fd44ecc7af56a8f97d637636ec23590ce5129f1c26fccff7b",
            "artifacts/pkg/0.1.1/pkg-0.1.1-1.yurtpkg.bundle": "b9138e31040c635d09fc81d0c6a54d92a26fdb88d94669c8289d1068f514bf1d",
            "artifacts/pkg/0.1.1/pkg-0.1.1-2.yurtpkg.bundle": "625b0e0c07ea5139def5da239d3d8219485f7f8db34031ca839b66f596bfcc6f",
            "artifacts/yurt-greet/0.1.0/yurt-greet-0.1.0-yurt_0.yurtpkg.bundle": "c8845ff2fceef34bde0d261653719995c700477cbf602f903d80e1ed879dd7a2",
            "artifacts/zlib/1.3.1/zlib-1.3.1-1.yurtpkg.bundle": "a26bb9aa98fc6e09b4dc280a2f32882b0d8aa4e695b823c60addf01028cbdf40",
            "artifacts/zsh/5.9.0/zsh-5.9.0-yurt_0.yurtpkg.bundle": "6923f054c8fe35a0bbcbe15f26fa8179b10d83e5d1a985180804c01b2c67560d",
            "artifacts/zsh/5.9.0/zsh-5.9.0-yurt_1.yurtpkg.bundle": "61674b3f4a9a933d20030396b428fe1201ff3236e4de933e524d725acce2d578",
        }
        self.assertLessEqual(rs.read_legacy(LEGACY).items(), allowed.items())


def fake_cosign_main(argv: list[str]) -> int:
    """sign-blob signs as FAKE_COSIGN_SUBJECT (default: the pinned subject); verify-blob
    accepts only a bundle whose signature binds the payload to the identity it is asked for."""
    command = argv[0]
    if command == "version":
        print(f"GitVersion:    {os.environ.get('FAKE_COSIGN_VERSION', 'v3.0.6')}")
        return 0
    bundle_path = Path(argv[argv.index("--bundle") + 1])
    payload = Path(argv[-1]).read_bytes()
    if command == "sign-blob":
        signer = (os.environ.get("FAKE_COSIGN_SUBJECT", SUBJECT), ISSUER)
        bundle_path.write_bytes(encode(bundle(payload, signer=signer)))
        return 0
    if command == "verify-blob":
        if hashlib.sha256(payload).hexdigest() in os.environ.get("FAKE_COSIGN_TOOL_ERROR_FOR", "").split():
            print("Error: updating local metadata and targets: error updating to TUF remote mirror: timeout", file=sys.stderr)
            return 2
        subject = argv[argv.index("--certificate-identity") + 1]
        issuer = argv[argv.index("--certificate-oidc-issuer") + 1]
        if os.environ.get("FAKE_COSIGN_BROKEN"):  # e.g. a stale trusted root: everything fails
            print("Error: failed to verify certificate: unable to verify certificate chain", file=sys.stderr)
            return 1
        if payload == RealBundleTest.payload and bundle_path.read_bytes() == RealBundleTest.bundle:
            return 0 if (subject, issuer) == (GORELEASER_SUBJECT, ISSUER) else 1
        try:
            data = json.loads(bundle_path.read_bytes())
        except ValueError:
            data = None
        if isinstance(data, dict) and set(data) - {"mediaType", "verificationMaterial", "messageSignature"}:
            # What cosign v3.0.6 prints for a bundle with an unknown top-level field.
            print("Error: bundle does not contain cert for verification, please provide public key", file=sys.stderr)
            return 1
        try:
            signature = json.loads(bundle_path.read_bytes())["messageSignature"]["signature"]
        except (ValueError, KeyError, TypeError):
            print("Error: bundle does not parse", file=sys.stderr)
            return 1
        if signature != fake_signature(payload, subject, issuer):
            # cosign v3.0.6 reports every verification failure as "Error: failed to verify ..."
            print("Error: failed to verify signature: identity mismatch or bad signature", file=sys.stderr)
            return 1
        return 0
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-cosign":
        sys.exit(fake_cosign_main(sys.argv[2:]))
    unittest.main()
