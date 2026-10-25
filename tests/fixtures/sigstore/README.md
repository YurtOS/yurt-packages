# Real Sigstore bundle fixture

`goreleaser-v2.19.0-checksums.txt` and
`goreleaser-v2.19.0-checksums.txt.sigstore.json` are the release assets
`checksums.txt` and `checksums.txt.sigstore.json` of
[goreleaser/goreleaser v2.19.0](https://github.com/goreleaser/goreleaser/releases/tag/v2.19.0),
copied unchanged. GitHub Actions signed them keyless with
`cosign sign-blob --bundle` (cosign v3.0.6, the `sigstore/cosign-installer`
v4.1.2 default) on public-good Sigstore, the way `release.yml` signs here.

Certificate identity:
`https://github.com/goreleaser/goreleaser/.github/workflows/release.yml@refs/tags/v2.19.0`,
issuer `https://token.actions.githubusercontent.com`.

The `pkg` verifier (YurtOS/yurt-pkg `yurt-repo-ci verify-blob`) accepts this
bundle for that identity against the trusted root Yurt images ship, and rejects
it for the yurt-packages identity with "identity mismatch".
