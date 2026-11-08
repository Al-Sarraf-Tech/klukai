# Exact SQLite security adjudication for the search release

The fixed official Debian SQLite 3.54.1-2 library remains reported vulnerable
by Trivy 0.71.0's Debian 13 data because both advisories have no fixed-version
entry for that distribution. The original vulnerable library must not return.
The verified archive and runtime hashes, vendor fixes, ABI checks and ledger
compatibility evidence are recorded in
`docs/research/2026-11-07-native-security.md`.

The release may use OpenVEX `fixed` for CVE-2026-119373,
CVE-2026-119374 and CVE-2026-119379 only when the exact reviewed package, architecture, archive
identity, exported library bytes, loaded library path, truthful package
metadata and immutable image identity all verify. Never rely on Trivy's PURL
matcher to enforce hashes, or on the candidate's self-reported hashes alone.
No wildcard package, version, CVE or architecture is permitted. The policy
has an explicit review expiry. Any mismatch, unknown suppression, unresolved
HIGH/CRITICAL finding, scanner failure or missing evidence refuses release.

The first of these was previously hidden by an owner-approved, expiring
`.trivyignore` entry. An isolated scan must expose it. Debian also identifies
that issue as fixed in the same package, so it receives the same exact-byte
proof instead of inheriting the old reachability waiver. The other existing
ignore, CVE-2026-119586, is not asserted fixed or silently accepted here.
Keep ambient ignore/config files out of this release scan.

Retain the complete raw vulnerability scan, exact OpenVEX statement, filtered
scan including modified findings, scanner/database identities, image identity
and independently checked native proof in one strict `image-security.json`
release material. Recompute the verdict from those records; a `passed` flag
is not evidence. Bind that material through schema 4 of the existing signed
release manifest. Recheck the loaded artifact's actual bytes at the relay and
deployment boundary. Preserve all secret, licence, reproducibility, source,
signature, backup, migration-recovery, health and rollback gates.

The pinned deployer currently understands schema 3 only. Update its executing
copy deliberately with its existing `install --revision` command after the
exact change is independently reviewed, passes full CI and reaches main,
while deployment automation is paused. Do not monkeypatch its imports or
alter branch protection, automated PR approval or migration approval policy.
Schema 3 may be explicitly supported for historical validation, never as a
silent downgrade for a new release. Preserve recovery of the running baseline.

Implementation is necessary release maintenance under the owner's standing
delivery authorization and no-permission-prompts direction. It does not
constitute an automated protected-path approval or authorize unresolved
vulnerabilities. The separate executing-copy update and revised manual rollout
wrapper require independent review before execution. Production stays on its
healthy current revision until all of those checks pass.
