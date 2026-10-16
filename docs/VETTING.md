# CI and release tool vetting

These records identify selected inputs. They do not certify final CI, a release,
or the absence of every vulnerability in an installed host tool.

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
