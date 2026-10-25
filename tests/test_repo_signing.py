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


def bundle(payload: bytes, **overrides) -> dict:
    cert = overrides.pop("cert", None) or certificate()
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
            "signature": "MEUCIQ==",
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


class RepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_package(self.root, "zsh", [("5.9.0", "yurt_0", b"zsh archive")])
        write_package(self.root, "busybox", [("1.37.0", "yurt_0", b"busybox"), ("1.37.0", "yurt_1", b"busybox 1")])
        (self.root / "index.json").write_bytes(
            rs.dump_json(
                {
                    "schema": 1,
                    "index_version": 17,
                    "generated_at": "2026-09-22T06:12:06Z",
                    "expires_at": "2026-09-29T06:12:06Z",
                    "packages": {},
                }
            )
        )
        # An index that lists both packages as committed, like main after a release run.
        index = json.loads((self.root / "index.json").read_bytes())
        for name in ["busybox", "zsh"]:
            data = (self.root / "packages" / f"{name}.json").read_bytes()
            index["packages"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "url": f"packages/{name}.json"}
        (self.root / "index.json").write_bytes(rs.dump_json(index))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def archives(self) -> list[str]:
        return [
            "artifacts/busybox/1.37.0/busybox-1.37.0-yurt_0.yurtpkg",
            "artifacts/busybox/1.37.0/busybox-1.37.0-yurt_1.yurtpkg",
            "artifacts/zsh/5.9.0/zsh-5.9.0-yurt_0.yurtpkg",
        ]

    def sign(self, rel: str) -> None:
        payload = (self.root / rel).read_bytes()
        (self.root / f"{rel}.bundle").write_bytes(encode(bundle(payload)))

    # regenerate-index

    def test_regenerate_index_bumps_version_and_times(self) -> None:
        now = dt.datetime(2026, 10, 25, 12, 0, 0, tzinfo=dt.timezone.utc)
        rs.regenerate_index(self.root, now)
        raw = (self.root / "index.json").read_bytes()
        index = json.loads(raw)
        self.assertEqual(list(index), ["schema", "index_version", "generated_at", "expires_at", "packages"])
        self.assertEqual(index["index_version"], 18)
        self.assertEqual(index["generated_at"], "2026-10-25T12:00:00Z")
        self.assertEqual(index["expires_at"], "2026-11-01T12:00:00Z")
        self.assertEqual(list(index["packages"]), ["busybox", "zsh"])
        package = (self.root / "packages" / "zsh.json").read_bytes()
        self.assertEqual(
            index["packages"]["zsh"],
            {"sha256": hashlib.sha256(package).hexdigest(), "size": len(package), "url": "packages/zsh.json"},
        )
        self.assertTrue(raw.endswith(b"}\n"))
        rs.regenerate_index(self.root, now + dt.timedelta(seconds=1))
        self.assertEqual(json.loads((self.root / "index.json").read_bytes())["index_version"], 19)

    def test_regenerate_index_refuses_time_that_does_not_advance(self) -> None:
        for now in ["2026-09-22T06:12:06Z", "2026-09-01T00:00:00Z"]:
            with self.subTest(now), self.assertRaises(rs.SigningError):
                rs.regenerate_index(self.root, rs.parse_time(now))

    def test_regenerate_index_refuses_a_record_that_does_not_match_its_archive(self) -> None:
        (self.root / self.archives()[0]).write_bytes(b"replaced")
        with self.assertRaisesRegex(rs.SigningError, "does not match its package record"):
            rs.regenerate_index(self.root, rs.parse_time("2026-10-25T00:00:00Z"))

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

    # plan

    def test_plan_lists_missing_and_placeholder_bundles(self) -> None:
        a, b, c = self.archives()
        (self.root / f"{a}.bundle").write_bytes(rs.PLACEHOLDER)
        self.sign(c)
        self.assertEqual(rs.plan(self.root), [a, b])

    def test_plan_refuses_to_re_sign_an_invalid_real_bundle(self) -> None:
        a = self.archives()[0]
        (self.root / f"{a}.bundle").write_bytes(encode(bundle(b"an older archive")))
        with self.assertRaisesRegex(rs.SigningError, "refusing to re-sign"):
            rs.plan(self.root)

    # check

    def sign_all(self) -> None:
        for rel in self.archives() + ["index.json"]:
            self.sign(rel)

    def test_check_passes_when_everything_is_signed(self) -> None:
        self.sign_all()
        self.assertEqual(rs.check(self.root, set()), rs.CheckResult([], valid=4))

    def test_check_rejects_a_placeholder_unless_it_is_legacy(self) -> None:
        self.sign_all()
        a = self.archives()[0]
        (self.root / f"{a}.bundle").write_bytes(rs.PLACEHOLDER)
        self.assertEqual(rs.check(self.root, set()).problems, [f"{a}.bundle: not a JSON Sigstore bundle"])
        self.assertEqual(rs.check(self.root, {f"{a}.bundle"}), rs.CheckResult([], valid=3, legacy=1))

    def test_legacy_allows_only_the_exact_placeholder_or_a_missing_file(self) -> None:
        self.sign_all()
        a = self.archives()[0]
        (self.root / f"{a}.bundle").write_bytes(b"some other fixture\n")
        self.assertEqual(len(rs.check(self.root, {f"{a}.bundle"}).problems), 1)
        (self.root / f"{a}.bundle").unlink()
        self.assertEqual(rs.check(self.root, {f"{a}.bundle"}), rs.CheckResult([], valid=3, legacy=1))
        self.assertEqual(rs.check(self.root, set()).problems, [f"{a}.bundle: missing"])

    def test_missing_bundle_is_allowed_only_for_a_package_the_index_does_not_list_yet(self) -> None:
        self.sign_all()
        # Publish Package adds busybox 1.37.0-yurt_2 and its metadata, and no bundle.
        write_package(
            self.root,
            "busybox",
            [("1.37.0", "yurt_0", b"busybox"), ("1.37.0", "yurt_1", b"busybox 1"), ("1.37.0", "yurt_2", b"busybox 2")],
        )
        self.assertEqual(rs.check(self.root, set()), rs.CheckResult([], valid=4, pending=1))
        # A placeholder is never allowed, pending or not.
        new = "artifacts/busybox/1.37.0/busybox-1.37.0-yurt_2.yurtpkg"
        (self.root / f"{new}.bundle").write_bytes(rs.PLACEHOLDER)
        self.assertEqual(rs.check(self.root, set()).problems, [f"{new}.bundle: not a JSON Sigstore bundle"])
        (self.root / f"{new}.bundle").unlink()
        # Once the index lists the package (re-signed), the archive needs its bundle.
        rs.regenerate_index(self.root, rs.parse_time("2026-10-25T00:00:00Z"))
        self.sign("index.json")
        self.assertEqual(rs.check(self.root, set()).problems, [f"{new}.bundle: missing"])

    def test_a_symlinked_bundle_is_rejected(self) -> None:
        self.sign_all()
        a = self.archives()[0]
        (self.root / f"{a}.bundle").unlink()
        (self.root / f"{a}.bundle").symlink_to(self.root / "index.json.bundle")
        self.assertEqual(rs.check(self.root, {f"{a}.bundle"}).problems, [f"{a}.bundle: is a symlink"])

    def test_check_rejects_an_unsigned_index(self) -> None:
        self.sign_all()
        (self.root / "index.json.bundle").write_bytes(rs.PLACEHOLDER)
        self.assertEqual(rs.check(self.root, set()).problems, ["index.json.bundle: not a JSON Sigstore bundle"])

    def test_check_rejects_an_index_changed_after_signing(self) -> None:
        self.sign_all()
        rs.regenerate_index(self.root, rs.parse_time("2026-10-25T00:00:00Z"))
        self.assertEqual(rs.check(self.root, set()).problems, ["index.json.bundle: message digest does not match the payload"])

    def test_check_rejects_stray_bundles_and_stale_legacy_entries(self) -> None:
        self.sign_all()
        (self.root / "artifacts" / "orphan.yurtpkg.bundle").write_bytes(rs.PLACEHOLDER)
        self.assertEqual(
            rs.check(self.root, {"artifacts/gone.yurtpkg.bundle"}).problems,
            [
                "artifacts/orphan.yurtpkg.bundle: no index.json or published archive to sign",
                "legacy entry artifacts/gone.yurtpkg.bundle: not a bundle path this repository expects",
            ],
        )

    def fake_cosign(self, exit_code: int) -> str:
        script = self.root.parent / f"fake-cosign-{exit_code}-{os.getpid()}"
        log = self.root.parent / f"fake-cosign-{os.getpid()}.log"
        script.write_text(f"#!/bin/sh\necho \"$@\" >> '{log}'\necho 'bad signature' >&2\nexit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.addCleanup(lambda: script.unlink(missing_ok=True))
        self.addCleanup(lambda: log.unlink(missing_ok=True))
        self.cosign_log = log
        return str(script)

    def test_check_runs_cosign_with_the_pinned_identity(self) -> None:
        self.sign_all()
        self.assertEqual(rs.check(self.root, set(), self.fake_cosign(0)), rs.CheckResult([], valid=4))
        calls = self.cosign_log.read_text().splitlines()
        self.assertEqual(len(calls), 4)
        self.assertIn(
            f"verify-blob --bundle {self.root / 'index.json.bundle'} --certificate-identity {SUBJECT} "
            f"--certificate-oidc-issuer {ISSUER} {self.root / 'index.json'}",
            calls,
        )

    def test_check_fails_when_cosign_rejects(self) -> None:
        self.sign_all()
        result = rs.check(self.root, set(), self.fake_cosign(1))
        self.assertEqual(len(result.problems), 4)
        self.assertEqual(result.valid, 0)
        self.assertIn("cosign verify-blob failed: bad signature", result.problems[0])

    # prune-legacy

    def test_prune_keeps_the_header_and_drops_signed_entries(self) -> None:
        a, b, c = self.archives()
        legacy = self.root / "legacy.txt"
        legacy.write_text(f"# header\n{a}.bundle\n{b}.bundle\nindex.json.bundle\n")
        self.sign(a)
        (self.root / f"{b}.bundle").write_bytes(rs.PLACEHOLDER)
        self.assertEqual(rs.prune_legacy(self.root, legacy), [f"{b}.bundle", "index.json.bundle"])
        self.assertEqual(legacy.read_text(), f"# header\n{b}.bundle\nindex.json.bundle\n")

    # the CLI, end to end as release.yml drives it

    def test_release_sequence(self) -> None:
        cosign = self.root.parent / f"sign-cosign-{os.getpid()}"
        helper = Path(__file__).resolve()
        cosign.write_text(
            "#!/bin/sh\n"
            f"exec '{sys.executable}' '{helper}' --fake-cosign \"$@\"\n"
        )
        cosign.chmod(0o755)
        self.addCleanup(lambda: cosign.unlink(missing_ok=True))
        a, b, c = self.archives()
        (self.root / f"{a}.bundle").write_bytes(rs.PLACEHOLDER)
        self.sign(c)
        legacy = self.root / "legacy.txt"
        legacy.write_text(f"# header\n{a}.bundle\n{b}.bundle\nindex.json.bundle\n")

        def run(*args: str, code: int = 0) -> str:
            result = subprocess.run(
                [sys.executable, str(TOOL), "--repo-root", str(self.root), *args],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, code, result.stderr)
            return result.stdout + result.stderr

        self.assertIn(
            "ok: 1 valid bundles, 3 legacy placeholders, 0 pending archives",
            run("check", "--legacy", str(legacy), "--cosign", str(cosign)),
        )
        self.assertIn("3 legacy and 0 pending bundles are unsigned", run("check", "--strict", "--legacy", str(legacy), code=1))
        self.assertIn("not a JSON Sigstore bundle", run("check", code=1))
        run("regenerate-index", "--now", "2026-10-25T00:00:00Z")
        to_sign = run("plan").split()
        self.assertEqual(to_sign, [a, b])
        for rel in to_sign + ["index.json"]:
            subprocess.run([str(cosign), "sign-blob", "--yes", "--bundle", str(self.root / f"{rel}.bundle"), str(self.root / rel)], check=True)
        self.assertIn("ok: 4 valid bundles, 0 legacy placeholders, 0 pending archives", run("check", "--strict", "--cosign", str(cosign)))
        run("prune-legacy", "--legacy", str(legacy))
        self.assertEqual(legacy.read_text(), "# header\n")
        self.assertEqual(rs.check(self.root, set()), rs.CheckResult([], valid=4))

    def test_check_with_nothing_to_check_fails(self) -> None:
        empty = self.root / "empty"
        (empty / "packages").mkdir(parents=True)
        (empty / "index.json").write_bytes(rs.dump_json({"schema": 1, "packages": {}}))
        result = subprocess.run(
            [sys.executable, str(TOOL), "--repo-root", str(empty), "check"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("index.json.bundle: missing", result.stderr)


class CommittedRepositoryTest(unittest.TestCase):
    """The CI guard on the repository as committed (CI also runs it with cosign)."""

    def test_committed_bundles_pass_the_check(self) -> None:
        result = rs.check(ROOT, rs.read_legacy(LEGACY))
        self.assertEqual(result.problems, [])
        self.assertGreater(result.valid + result.legacy + result.pending, 0)

    def test_legacy_list_only_names_bundles_that_existed_before_release_yml(self) -> None:
        allowed = {
            "index.json.bundle",
            "artifacts/busybox/1.37.0/busybox-1.37.0-yurt_0.yurtpkg.bundle",
            "artifacts/clang/23.0.0/clang-23.0.0-yurt_0.yurtpkg.bundle",
            "artifacts/libcxx/22.1.0/libcxx-22.1.0-1.yurtpkg.bundle",
            "artifacts/ncurses/6.5.0/ncurses-6.5.0-yurt_0.yurtpkg.bundle",
            "artifacts/openssl/4.0.0/openssl-4.0.0-1.yurtpkg.bundle",
            "artifacts/pkg/0.1.0/pkg-0.1.0-yurt_0.yurtpkg.bundle",
            "artifacts/pkg/0.1.1/pkg-0.1.1-1.yurtpkg.bundle",
            "artifacts/pkg/0.1.1/pkg-0.1.1-2.yurtpkg.bundle",
            "artifacts/yurt-greet/0.1.0/yurt-greet-0.1.0-yurt_0.yurtpkg.bundle",
            "artifacts/zlib/1.3.1/zlib-1.3.1-1.yurtpkg.bundle",
            "artifacts/zsh/5.9.0/zsh-5.9.0-yurt_0.yurtpkg.bundle",
            "artifacts/zsh/5.9.0/zsh-5.9.0-yurt_1.yurtpkg.bundle",
        }
        self.assertLessEqual(rs.read_legacy(LEGACY), allowed)


def fake_cosign_main(argv: list[str]) -> int:
    """sign-blob writes a synthetic bundle; verify-blob accepts anything the shape check accepts."""
    command = argv[0]
    if command == "sign-blob":
        bundle_path = Path(argv[argv.index("--bundle") + 1])
        payload = Path(argv[-1]).read_bytes()
        bundle_path.write_bytes(encode(bundle(payload)))
        return 0
    if command == "verify-blob":
        return 0
    return 2


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fake-cosign":
        sys.exit(fake_cosign_main(sys.argv[2:]))
    unittest.main()
