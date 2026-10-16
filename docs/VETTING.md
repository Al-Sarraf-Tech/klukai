# CI and release tool vetting

These records identify selected inputs. They do not certify final CI, a release,
or the absence of every vulnerability in an installed host tool.

## pytest-asyncio 1.4.0 — 2026-10-15

The first full run in the isolated CPython 3.14 CI image failed before test
collection: 1.3.0 imports the deprecated `asyncio.AbstractEventLoopPolicy` at
runtime, and `PYTHONWARNINGS=error` rejects it. The
[upstream 1.4.0 release notes](https://github.com/pytest-dev/pytest-asyncio/releases)
identify the fix: that import now occurs only during type checking.

- The non-yanked wheel and source hashes were verified against the
  [versioned PyPI metadata](https://pypi.org/pypi/pytest-asyncio/1.4.0/json).
  Wheel SHA256: `933ca923a23075a87fb7070c0ec272a6848489824d887c85c812670932835aa1`.
- The wheel's Apache-2.0 license and relevant source were inspected before
  installation. The dated package advisory check found no known vulnerabilities.
- The CI lock was regenerated with existing pip-tools 7.5.3 and pip 26.0.1 on
  Python 3.14. Only pytest-asyncio changes version; the other 130 pins remain.
- Core tests now explicitly select function-scoped fixture loops. The existing
  session-scoped integration user fixture explicitly retains its session loop;
  an isolated regression exercises that actual fixture without live backends.

Raw metadata, wheel, hashes, advisory results and startup RED/GREEN logs are in
`~/.local/state/klukai/s-uplift-20261015/pytest-asyncio-1.4.0-vetting/`.
The old CI image remains failed evidence. A new image and full run are required.

## Cosign 3.1.3 — 2026-10-15

The existing Cosign 3.0.6 has a legacy-bundle verification bypass described in
[GHSA-fx35-mq7g-6g98](https://github.com/sigstore/cosign/security/advisories/GHSA-fx35-mq7g-6g98).
Select the patched 3.1.3 binary for release verification. The earlier distroless
OCI image verification is outside this advisory's affected commands.

- Source: `sigstore/cosign`, commit
  `11926fa5bbbbde47e88fc006b625a17769b743b2`; Apache-2.0 LICENSE checked at that commit.
- [Versioned release](https://github.com/sigstore/cosign/releases/tag/v3.1.3):
  `cosign-linux-amd64`, 141178250 bytes, SHA256
  `4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71`.
- The published checksum and binary signatures both verified with the exact
  documented identity `keyless@projectsigstore.iam.gserviceaccount.com` and
  issuer `https://accounts.google.com`. Both bundles use the modern
  `application/vnd.dev.sigstore.bundle.v0.3+json` format. Bootstrap verification
  explicitly selected that format, which the advisory says is unaffected.
- The verified binary reports version 3.1.3, the commit above and a clean tree.
  It lives in private task capacity; the system binary was not replaced.

[Official verification instructions](https://docs.sigstore.dev/cosign/system_config/installation/)
define the expected signer. Raw release metadata, signatures, checksums,
verification logs, license and hash-bound receipt are retained under
`~/.local/state/klukai/s-uplift-20261015/tool-vetting/`.
Workflow provisioning and consumption of this verified input remain required.

## Existing Trivy input — 2026-10-15

Amarillo supplies Fedora's signed `trivy-0.69.3-1.fc44.x86_64` RPM, installed
2026-04-29. Its executable SHA256 is
`7454c06bb5393a11f7e883adf690e59625ec499a691fc1ce89ac66c592ec2053`.
The [upstream incident advisory](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23)
identifies 0.69.3 as unaffected by that particular compromise. This is not a
blanket vulnerability clearance. Final scanner provenance, current database
identity and the actual artifact scan must accompany the release manifest.
Do not execute mutable installers or select an unchecked replacement release.

## Syft 1.54.1 — 2026-10-15

The installed unmanaged Syft 1.42.1 is affected by the upstream
[temporary-file cleanup advisory](https://github.com/anchore/syft/security/advisories/GHSA-rjcw-vg7j-m9rc)
and [binary-parser allocation advisory](https://github.com/anchore/syft/security/advisories/GHSA-mw2c-m758-9v5q).
Their patched versions are 1.42.3 and 1.52.0, respectively. Select the pinned
[1.54.1 release](https://github.com/anchore/syft/releases/tag/v1.54.1)
for private version and format-conversion acceptance; the system binary stays
unchanged. The candidate is **not cleared for release scans**: its published
dependency inventory produced additional HIGH candidates described below.

- Source tag resolves to commit `b254e6d92f28c3868a755f62fb3ca8f26e9fee76`.
  Its Apache-2.0 LICENSE exactly matches the authenticated archive member.
- Linux amd64 archive: 30968256 bytes, SHA256
  `c069905b391cc4c20a5ba65ad5c10be2a7ba074f8ea6ad203e24d14e303dad47`.
  The selected regular executable is 91779234 bytes, SHA256
  `dbf75864e7a7ff9e1fbf00552c31483f693188632a9f68f343cd7653dac513d6`.
- The checksum manifest's modern Sigstore v0.3 bundle verified with the
  previously vetted Cosign 3.1.3, the exact certificate identity
  `https://github.com/anchore/syft/.github/workflows/release.yaml@refs/heads/main`
  and issuer `https://token.actions.githubusercontent.com`. The selected
  archive and published SBOM both match that authenticated manifest.
  This authenticates release assets; it is not a source-to-binary attestation.
- The published SBOM is Syft JSON schema 16.1.10. Existing Trivy 0.69.3 rejects
  that format as unsupported; its failed scan is retained and does not mean
  zero vulnerabilities. The authenticated executable reports version 1.54.1,
  commit above and Go 1.26.8. Its private CycloneDX 1.6 conversion preserves
  all 297 original package identities and adds one executable file component.
- Trivy recognizes all 297 Go package rows in that converted SBOM. It reports
  HIGH candidates CVE-2026-78667, CVE-2026-78669 and CVE-2026-97031 in Go 1.26.8,
  plus CVE-2026-78669 in golang.org/x/net v0.59.0. Declared fixed versions are
  Go 1.26.9/1.27.2 and x/net v0.60.0. Lower and unknown severity findings are
  retained too. Reachability and binary incorporation are not adjudicated;
  package-version findings do not establish that the converter is exploitable.
  Resolve these findings or select a passing replacement before release use.

[Anchore's verification instructions](https://oss.anchore.com/docs/installation/verification/)
define the repository/workflow identity policy. Raw signed inputs, verification
commands/output, immutable source references and the failed format check remain
under `~/.local/state/klukai/s-uplift-20261015/tool-vetting/syft-1.54.1/`.
A separate attempt to retain that documentation page returned HTTP 403; the
page was read through the web tool, without a paid fallback.
