# Releasing Klukai core

The core image that runs in production is the exact image CI built and scanned.
Nothing is rebuilt on amarillo. Every step refuses rather than guessing.

Once activated, [deploy-on-merge](deploy-on-merge.md) runs these same steps by
itself for every certified merge to `main`, and adds the atomic web release.
The manual commands below stay valid. They share its deployment lock, so a
manual and an automatic deploy can never interleave.

## 1. CI builds, scans and retains the artifact

The `image-scan` job (`.github/workflows/ci.yml`) on the heavy runner:

- builds the image from the commit with digest-pinned bases, hash-locked
  dependencies and network-less build stages, then builds it a second time
  with `--no-cache` and compares the two: layer digests may differ (they
  include timestamps), but any difference in file content, mode, owner or link
  fails the job, and the comparison is kept as evidence either way;
- installs Trivy from its release tarball, checked against a pinned SHA-256;
- records a complete unsuppressed HIGH/CRITICAL vulnerability scan using a run-private frozen
  Trivy database, then blocks unresolved HIGH/CRITICAL findings. The exact
  reviewed SQLite exceptions use OpenVEX `fixed` only after the policy's
  archive, package, architecture and independently exported library bytes
  match. Unknown changes, missing evidence, scanner failures or expired new
  adjudications fail closed. Raw and filtered reports (including modified
  findings), OpenVEX, policy, scanner/database identities and native proof
  remain together in `image-security.json`; this is an adjudicated scan, not
  an unfiltered zero-finding claim;
- independently blocks on secrets found in the image;
- writes a CycloneDX SBOM and a licence inventory, and blocks unless the
  inventory passes `tools/ci/license_policy.py` (policy below). The SBOM uses
  the same inventory and frozen database as the security evidence;
- saves `core-image.tar`, those materials and `manifest.json` (revision, source
  tree, base images, tool versions, layer diff IDs, tar SHA-256, material
  digests). Schema 4 requires exactly the SBOM, licence inventory, licence
  verdict, reproducibility result and `image-security.json` under
  `/opt/klukai-ci/artifacts/<sha>/<run>-<attempt>/release`.
  Tars are kept 7 days, manifests indefinitely. Scan diagnostics, including
  failed scans, are retained separately in the run's `security-materials/`
  directory; a failed adjudication never produces a release manifest.

## 2. Amarillo pulls, verifies and signs

```sh
GH_TOKEN=... python3 ops/amarillo/scripts/klukai-fetch-ci-artifacts.py image <full sha>
```

The relay requires a successful CI run whose `Required CI gates` job passed for
that exact commit. The manifest must name that run, and its SHA-256 must equal
the `KLUKAI_RELEASE_MANIFEST_SHA256=` line the run's own image-scan job printed
to its GitHub log; bytes another job writes on the shared heavy guest are refused. Since 2026-10-19 Klukai CI runs on amarillo's own VMs and the relay
reads `/docker/klukai/ci-artifacts` directly (`--source amarillo`, what
deploy-on-merge always uses). The legacy Dominus path reads the files through the guest agent in 2 MiB
chunks. libvirt caps an agent reply at 10 MiB and virsh's RPC string at 4 MiB,
and exceeding the first disables the agent channel. It then verifies the manifest,
every material digest, the licence verdict, the tar hash, and the loaded image's
layers, revision label and runtime configuration digest, independently checks
the actual loaded native bytes by immutable image ID, and tags
`klukai-core:ci-<sha7>`. Only after all of that passes does it write
`provenance.json` (CI run, manifest digest, layers, configuration digest,
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
   check out (including content-identical second build and recomputed security
   adjudication under the installed reviewed policy), and the image the
   local tag resolves to has exactly the manifest's layers, revision and
   runtime configuration digest. The configuration digest covers entrypoint,
   command, environment, user, working directory, ports, volumes, labels, stop
   signal, health check and platform, with unset keys dropped so the CI guest's
   containerd store and amarillo's classic store agree. The deployer resolves
   the tag once and from then on uses only that image ID, pinned as the
   immutable tag `klukai-core:verified-<sha7>-<id12>`. Before tagging, deployment
   independently exports and checks the loaded artifact's native bytes again.
   A signed `passed` flag cannot substitute for either boundary's checks.
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
is switched to the verified tag (inside the rollback-protected region), and only companion-core is recreated through `klukai_compose.py`
(secrets from the encrypted store, so runtime settings are preserved). The
candidate must run the verified image ID, become healthy and ready within
180 s, then pass every check of a 180 s hold (healthy, ready, no restarts; the
hold may not end on a failed check) and keep its 5xx share
(`status_class="5xx"`) at or below 5%. The 5xx check must be evaluated:
Prometheus must answer and be scraping core (`up{job="klukai-core"}` is 1),
otherwise the candidate is rejected after three attempts. Only a confirmed
absence of traffic in the window passes, recorded as not evaluated. Any
failure, and also a compose error, timeout, Ctrl-C or SIGTERM from the pin
switch on, makes the deployer pin the
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

After core, deploy the PWA and run `scripts/e2e_live.py` as the `claude` test
user. Before the atomic web root is activated, deploy the PWA with
`./scripts/deploy-web.sh`. After activation, relay the CI-built PWA and switch
it atomically:

```sh
GH_TOKEN=... python3 ops/amarillo/scripts/klukai-fetch-ci-artifacts.py --source amarillo web <full sha>
python3 ops/amarillo/scripts/klukai_web_release.py deploy <full sha>
```

The second command checks every entry file through core and switches back
automatically on failure.

A release that changes `docker/core/migrations/` or `docker-compose.yml` needs a
reviewed recovery plan in `docs/releases/recovery/` (see its README) before
deploy-on-merge will ship it.

## Licence policy

Owner decision, 2026-10-17: Klukai is private and its image is never
distributed, so GPL/LGPL OS and library components (glibc, psycopg) are
allowed. `tools/ci/license_policy.json` lists the allowed categories, the
GPL/LGPL family, licences reviewed per package (MPL-2.0 for ca-certificates,
certifi, hypothesis, py-vapid and pywebpush) and reviewed unclassified notices.
Any other copyleft or unreviewed licence fails CI until it is reviewed and
listed.

## Updating the executing verifier for schema 4

The pinned schema 3 deployer refuses schema 4 artifacts until its own executing
copy is deliberately updated. This is separate operator maintenance, before
application rollout. Review the complete archived-code diff from the installed
pin to the final main revision, including the verifier and exact local policy.
Keep protected-path, PR approval and migration recovery requirements unchanged;
standing delivery authorization does not fabricate an approval event.

Pause automation **before the maintenance change reaches main**, and verify no
transaction is inflight. Retain the previous executing pin and running immutable
image/web/checkout/recovery identities. After independent review and full CI for
the exact final main SHA, use the existing pinned install command:

```sh
D=~/.local/share/klukai/deployer/current/ops/amarillo/scripts
python3 "$D/klukai-deploy-on-merge.py" pause
python3 "$D/klukai-deploy-on-merge.py" status  # require no inflight transaction
python3 "$D/klukai-deploy-on-merge.py" install --revision <reviewed-final-main-sha>
```

The normal installer preserves main ancestry checks and switches `current`
atomically. Do not copy files into its pinned tree, import from the candidate
checkout or monkeypatch a running process. In a fresh process, verify the new
`current` target, archived files against the reviewed git objects, actual import
paths, installed policy digest, valid schema 4 acceptance and invalid/tampered
evidence rejection using disposable fixtures. Installation alone does not roll
out the app. Then execute the separately reviewed manual rollout/recovery
procedure with signature, certification, backup, migration, readiness, hold and
5xx gates intact. Resume automation only after live verification and a recorded
outcome. If maintenance fails before rollout, production stays on its healthy
revision; restore the old executing pin through the supported installer while
no transaction is inflight.

Production relay and deployment accept only schema 4. Schema 3 is available
only through explicit `--allow-legacy` for historical artifact validation and
perf baselines; the perf candidate remains strict. Automatic rollback restores
the retained immutable old image and pin directly, so crossing back to a
schema 3-era image never requires weakening the new-candidate verifier. Policy
expiry blocks new adjudications after expiry; previously issued evidence is
checked against its issuance interval and the exact installed policy, keeping
historical verification possible. A policy change requires reviewed executing
verifier maintenance, never trusting an envelope's copied policy implicitly.
