# Releasing Klukai core

The core image that runs in production is the exact image CI built and scanned.
Nothing is rebuilt on amarillo. Every step refuses rather than guessing.

## 1. CI builds, scans and retains the artifact

The `image-scan` job (`.github/workflows/ci.yml`) on the heavy runner:

- builds the image from the commit with digest-pinned bases, hash-locked
  dependencies and network-less build stages, then builds it a second time
  with `--no-cache` and records layer-by-layer reproducibility;
- installs Trivy from its release tarball, checked against a pinned SHA-256;
- blocks on HIGH/CRITICAL vulnerabilities and on secrets found in the image;
- writes a CycloneDX SBOM and a licence inventory, and blocks unless the
  inventory passes `tools/ci/license_policy.py` (policy below);
- saves `core-image.tar`, those materials and `manifest.json` (revision, source
  tree, base images, tool versions, layer diff IDs, tar SHA-256, material
  digests) under `/opt/klukai-ci/artifacts/<sha>/<run>-<attempt>/release`.
  Tars are kept 7 days, manifests indefinitely.

## 2. Amarillo pulls, verifies and signs

```sh
GH_TOKEN=... python3 ops/amarillo/scripts/klukai-fetch-ci-artifacts.py image <full sha>
```

The relay requires a successful CI run whose `Required CI gates` job passed for
that exact commit. The manifest must name that run, and its SHA-256 must equal
the `KLUKAI_RELEASE_MANIFEST_SHA256=` line the run's own image-scan job printed
to its GitHub log; bytes another job writes on the shared heavy guest are refused. It reads the files through the Dominus guest agent in 2 MiB
chunks. libvirt caps an agent reply at 10 MiB and virsh's RPC string at 4 MiB,
and exceeding the first disables the agent channel. It then verifies the manifest,
every material digest, the licence verdict, the tar hash, and the loaded image's
layers and revision label, and tags `klukai-core:ci-<sha7>`. Only after all of
that passes does it write `provenance.json` (CI run, manifest digest, layers,
materials, verification result) and sign it with the amarillo release key
(`~/.config/klukai/release-signing/release_ed25519`, never leaves the host)
using `ssh-keygen -Y sign -n klukai-release`. The public key is committed in
`ops/amarillo/release/allowed_signers`, so anyone can check a signature:

```sh
ssh-keygen -Y verify -f ops/amarillo/release/allowed_signers -I klukai-release@amarillo \
  -n klukai-release -s provenance.json.sig < provenance.json
```

The signature attests that amarillo verified this artifact against a green CI
run. It is not an SLSA level claim.

## 3. Deploy with automatic rollback

```sh
cd ~/git/klukai && git pull --ff-only    # main checkout at the released commit
python3 ops/amarillo/scripts/klukai-release.py deploy <full sha>
```

Staged preflight, any failure refuses before production changes:

1. **Artifact:** the signature verifies against the committed signer, the
   provenance binds the manifest by digest, the materials and licence verdict
   check out, and the local tag has exactly the manifest's layers and revision.
2. **Source:** the main checkout is at the release commit and
   `docker-compose.yml` is unmodified.
3. **Rollback safety:** a rollback restores only the image. Core applies
   migrations at startup and will not start if an applied migration file is
   missing, and a rollback recreates with the new compose file. If
   `docker/core/migrations/` or `docker-compose.yml` changed since the running
   image's revision label (or that label is unknown), the deployer refuses
   unless `--accept-manual-rollback` is given after a manual rollback plan.
4. **Backup:** a fresh `pg_dump` goes to `/mnt/nvmeINT/backups/klukai/releases/`.
   It is restored into a throwaway PostgreSQL container of the same image with no
   network, which must have every table and at least the live row counts of
   `users`, `companion_messages`, `companion_episodes`, `companion_memories`,
   `companion_affection` and `memory`. The throwaway container is removed with
   its volume (`docker rm -f -v`). Nothing is ever restored to production.

Rollout: the running image gets an immutable `klukai-core:rollback-<time>-<id>`
tag, the non-secret image pin `~/.local/state/klukai/release/core-image.json`
is switched, and only companion-core is recreated through `klukai_compose.py`
(secrets from the encrypted store, so runtime settings are preserved). The
candidate must run the verified image ID, become healthy and ready within
180 s, then pass every check of a 180 s hold (healthy, ready, no restarts; the
hold may not end on a failed check) and keep its 5xx share
(`status_class="5xx"`) at or below 5%. With no traffic in the window the 5xx
check is recorded as not evaluated. Any failure, and also a compose error,
timeout, Ctrl-C or SIGTERM after the pin switch, makes the deployer pin the
rollback tag, recreate core, wait for health and exit 2 (`rolled_back`) or
loudly report `rollback_failed`. Data and volumes are untouched, so newer
writes survive. After a successful deploy it keeps the newest 10 release dumps
and 5 rollback tags. Each run writes a receipt to
`~/.local/state/klukai/release/receipts/`.

Rollback drill, disposable and safe to repeat:

```sh
python3 ops/amarillo/scripts/klukai-release.py drill --image klukai-core:ci-<sha7>
```

It deploys a good build of a tiny service, then a deliberately broken build,
and passes only if the broken release is rolled back automatically.

After core, deploy the PWA with `./scripts/deploy-web.sh` and run
`scripts/e2e_live.py` as the `claude` test user.

## Licence policy

Owner decision, 2026-10-17: Klukai is private and its image is never
distributed, so GPL/LGPL OS and library components (glibc, psycopg) are
allowed. `tools/ci/license_policy.json` lists the allowed categories, the
GPL/LGPL family, licences reviewed per package (MPL-2.0 for ca-certificates,
certifi, hypothesis, py-vapid and pywebpush) and reviewed unclassified notices.
Any other copyleft or unreviewed licence fails CI until it is reviewed and
listed.
