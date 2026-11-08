# Native image security update — 2026-11-07

**Deployment remains blocked.** The verified package update removes the two
zlib findings and installs fixed SQLite bytes, but the unchanged Trivy 0.71.0
CI gate still exits 1 with two HIGH SQLite findings. This document records a
reviewable remediation, not a successful release or permission to bypass CI.

## Materials and provenance

The production Dockerfile retains its existing pinned CPython 3.14.8 and
Debian 13 distroless bases. Current official image candidates also contained
the vulnerable packages, so refreshing them would not resolve the findings.
The update installs exactly two existing dependencies, with no apt resolution,
new packages, libc upgrade or compiler. Installation runs without networking;
`ADD --checksum` pins each official archive. The build explicitly requires
amd64 rather than selecting unreviewed bytes for another architecture.

| Package | Version | Archive SHA256 |
| --- | --- | --- |
| libsqlite3-0 | 3.54.1-2, Debian sid | `7789bf0b2338b781de388d83d7c473f9386c9ef638af110b5c1b3d1debb36f00` |
| zlib1g | 1:1.3.dfsg+really1.3.1-1+deb13u1, trixie-security | `8c27c342230503b73dfde629642c3b352e87b554e06d60cf58772f236a305ceb` |

Official package identities and hashes:
[SQLite](https://packages.debian.org/sid/amd64/libsqlite3-0/download),
[zlib](https://packages.debian.org/trixie/amd64/zlib1g/download).
The exact archive URLs, runtime library paths and library SHA256s are retained
in `docker/core/native-security-packages.json` and copied into the image at
`/usr/share/klukai-runtime/native-security-packages.json`.

Both hashes were also authenticated through signed archive indexes, using the
public Debian archive keyring from the existing pinned Python image:

| Signed index | InRelease SHA256 | Valid Debian 13 signing subkey |
| --- | --- | --- |
| [sid](https://deb.debian.org/debian/dists/sid/InRelease) | `7ea1bbff2245657eb1de74da54e1653b38c760c748fa9b33360c7c42fada47ef` | `B8E5F13176D2A7A75220028078DBA3BC47EF2265` |
| [trixie-security](https://security.debian.org/debian-security/dists/trixie-security/InRelease) | `fa9db169adac7380297ee3cf1ba5f7741ffe6e7a2505f33bfb2b84b7003e8d6b` | `89C87ACEA5DD6B8E6A7068808E9F831205B4BA95` |

`gpgv` validated InRelease signatures, then SHA256/size checks authenticated each
`main/binary-amd64/Packages.xz`; its package stanza matched the downloaded
archive. Independent authenticated apt downloads reproduced the same hashes.
An exact-version simulated install reported **2 upgraded, 0 newly installed,
0 removed**. Signed releases, indexes and verification output are retained
locally under `/tmp/klukai-debian-signed-20261107/`.

SQLite's declared dependency and highest ELF symbol requirement are glibc
2.38; the pinned images provide 2.41. Its ELF dependencies are libc and libm.
The fixed library loads through the existing native packaging closure. zlib
is already in distroless, so the update also replaces its final runtime
payload, dpkg status and md5sums. Package metadata retains the true versions
and original Debian 13 OS identity. CycloneDX confirms both updated package
versions; the existing license gate reported 126 findings and zero violations.

## Reproduction and observed results

Local builds used source parent `8e0c81f14e3abaadc55cf91d956bc9eed1f76b57`
plus this uncommitted image patch. They used the investigative label
`VCS_REF=8e0c81f`; these are not exact committed-source release artifacts.

```sh
docker build --build-arg VCS_REF=8e0c81f -t klukai-core:security-20261107 \
  -f docker/core/Dockerfile docker/core
docker build --no-cache --build-arg VCS_REF=8e0c81f \
  -t klukai-core:security-20261107-rebuild -f docker/core/Dockerfile docker/core
python3 -m tools.ci.release_artifact compare-builds \
  --first klukai-core:security-20261107 \
  --second klukai-core:security-20261107-rebuild \
  --output /tmp/klukai-security-reproducibility-20261107.json
KLUKAI_TEST_IMAGE=klukai-core:security-20261107 python3 -m pytest \
  tests/release/test_image_runtime.py tests/release/test_release_artifact.py \
  -q --override-ini addopts=
```

Both builds succeeded. Image IDs were
`sha256:b940052f6b02ae82bc00f4b2349aa24f38eb0345a7fb27d17fd5b96f38a44163`
and `sha256:32c02967548c640b9eea40a5f28b7cd5b3745d8ea6c647637e28d275279d5054`.
The existing reproducibility gate passed: **6,758 files, zero differing paths,
content_identical=true**. Five layer digests differed due to timestamps, as
allowed by that gate's existing content comparison.

Runtime/release tests: **17 passed**. The new security regression first failed
on the prior image's SQLite 3.46.1. On the candidate, `/proc/self/maps` and file
hashes prove Python loads SQLite **3.54.1**, library SHA256
`225ceb839db2c1dd484cfcaa5ef9fbdd429eb1d5f1e0c39fbd50150bca5fc7bc`, and the
patched zlib library SHA256
`6c13931760210e8b391b63a337dfe4071346c4ad6e3827694edb7b6989bb16dd`.
The probe also checks installed dpkg versions and embedded material records.
Existing runtime-packaging/executor tests: **60 passed**; touched-test Ruff
format/check and `git diff --check` passed.

Existing `test_api_spend_cap.py` and `test_creative_budget.py` ran with the
verified SQLite library selected through process-local `LD_LIBRARY_PATH`:
**16 passed**, including concurrent reservations and persistent caps. All nine
resulting synthetic ledgers passed `PRAGMA integrity_check`. A disconnected
container roundtrip exercised the real budget modules with old SQLite 3.46.1,
then candidate 3.54.1, then old 3.46.1 again: spend totals **0.20 → 0.35 → 1.00**,
creative attempts **1 → 2 → 3**. The old image reopened and wrote both files,
enforced the final caps, and integrity stayed `ok` at every stage. No production
ledgers or provider calls were used. No migration, compose or release recovery
guard change is needed for this scoped binary update.

## Remaining scan failure

The actual CI Trivy 0.71.0 executable was verified against pinned archive
SHA256 `30a3d22b23f88c233f1658f562fb477cae3b3e8b4761109d515b7698daf85814`;
the vulnerability DB was refreshed on 2026-11-07. The unchanged gate command:

```sh
trivy image --severity HIGH,CRITICAL --exit-code 1 --no-progress \
  klukai-core:security-20261107
```

Result: **exit 1, HIGH: 2, CRITICAL: 0**. Findings are
[CVE-2026-119374](https://security-tracker.debian.org/tracker/CVE-2026-119374)
and [CVE-2026-119379](https://security-tracker.debian.org/tracker/CVE-2026-119379),
with truthful installed version `3.54.1-2`, status `affected` and no fixed
version for Debian 13. Debian records sid fixed from `3.54.1-1` while trixie
remains unfixed. The official
[Trivy Debian detector](https://github.com/aquasecurity/trivy/blob/v0.71.0/pkg/detector/ospkg/debian/debian.go)
selects advisories by OS release and unconditionally reports entries with an
empty fixed version, without comparing installed versions. The zlib findings
[CVE-2026-76844](https://security-tracker.debian.org/tracker/CVE-2026-76844) and
[CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091)
are resolved by the official stable-security package.

Full local evidence is indexed in
`/tmp/klukai-search-image-security-20261107.md`, including image probes, signed
indexes, SBOM/license reports, failed scan JSON, tests and ledger roundtrip.
No ignore entry, VEX statement, OS relabeling, metadata hiding or scan-policy
change is part of this patch. Full CI and deployment remain blocked pending a
separately reviewed resolution of the remaining scanner findings.
