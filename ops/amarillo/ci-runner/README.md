# Klukai CI runners on amarillo and Dominus

Owner decision 2026-10-19: every queued Klukai job runs in its own fresh VM,
deleted after the job. Owner decision 2026-10-22: when Dominus is online, one
CI run is split across **both** hosts; when it is not, everything runs on
amarillo, as before. Other repositories keep the Dominus fleet runners.

## Two pools

- **Pools.** The same controller runs on both hosts.
  - amarillo needs no config: 12 threads, `klukai-amarillo-*` labels, perf alone.
  - Dominus reads `/etc/klukai-ci/host.json` (`ops/dominus-nobara/ci-runner/`):
    - 20 threads and 32 GiB;
    - `klukai-dominus-*` labels;
    - subnet `10.232.0.0/24` with the same fail-closed guard;
    - AMD `k10temp` sensor;
    - the owner's GitHub CLI login, read in memory.
- **Heartbeat.** While Dominus is healthy, not gaming (no new guests while
  `/run/user/1000/dominus-gpu/game-active` exists) and outside 23:00-08:00,
  its controller writes the repository variable `KLUKAI_CI_DOMINUS_HEARTBEAT`
  every minute, and `0` otherwise.
- **Routing.** CI's first job (`tools/ci/pool.py`) reads the heartbeat:
  - **under 150 s old:** `ci-image` → `test`/`real-backends`, the component
    suites and Flutter go to Dominus;
  - **always on amarillo:** quick checks and `image-scan` → `perf`. Perf
    measures on calibrated hardware, and the release artifacts stay local to
    the deployer;
  - **otherwise:** all of it stays on amarillo.

  Jobs that hand files to each other stay in one pool.
- **Fallback.** amarillo's controller takes over any `klukai-dominus-*` job
  still queued after 180 s (Dominus went off, started gaming, or is full),
  registering its runner with that job's labels.
- **Evidence.** Dominus cannot reach amarillo, so amarillo pulls.
  `klukai-ci-pull.timer` (user unit, every minute) copies the run records Dominus
  changed in the last 90 minutes into `/docker/klukai/ci-artifacts`, using
  `ops/amarillo/scripts/klukai-ci-pull.py`. Nothing is deleted or overwritten.
  The deployer reads it all locally and still binds every byte to the run's
  own job log. Dominus prunes its records after two days.
  - The link is relayed, about 2 MiB/s, and a Dominus run writes about 1 GB.
    So each pull copies the frontend job (the PWA the deployer reads, about
    70 MB) first.
  - The rest gets 45 s per pull; interrupted files resume on the next pull.
  - A PWA still arriving makes the deployer retry with backoff, never deploy
    early: the bytes must match the job log.
- **Measured basis.** Full CI took 16 min on amarillo alone. Jobs waited
  2-7 min for CPUs, while booting a VM takes a median of 19 s. The
  `image-scan` → `perf` chain, about 8 min, is the floor.

### Install the Dominus pool (root on Dominus)

```sh
sudo install -d -m 0755 /opt/klukai-ci /etc/klukai-ci /var/lib/libvirt/images/klukai-ci/base
# From amarillo: stream the base image (zstd more than halves 6.2 GiB over the relay),
# then keep it only if its digest matches amarillo's base.qcow2.sha256:
#   B=/var/lib/libvirt/images/klukai-ci/base
#   sudo cat $B/base.qcow2 | zstd -3 -T8 -c | ssh dominus-nobara "zstd -dc | sudo tee $B/base.qcow2.partial >/dev/null"
#   then on Dominus: sha256sum base.qcow2.partial = amarillo's digest, mv to base.qcow2 + base.qcow2.sha256,
#   copy manifest.json and packages.tsv, and match amarillo's modes: dir root:qemu 0750,
#   base.qcow2 qemu:qemu 0440.
sudo install -m 0644 ops/amarillo/ci-runner/{klukai_ci_runner.py,klukai-ci-guard.nft} \
  ops/dominus-nobara/ci-runner/klukai-ci-net.xml /opt/klukai-ci/
sudo install -m 0755 ops/dominus-nobara/ci-runner/network-up.sh /opt/klukai-ci/
sudo install -m 0644 ops/dominus-nobara/ci-runner/host.json /etc/klukai-ci/host.json
sudo install -m 0644 ops/dominus-nobara/ci-runner/systemd/klukai-ci-{network,runner}.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now klukai-ci-network.service klukai-ci-runner.service
```

On amarillo, install the puller:

```sh
install -m 0644 ops/amarillo/systemd/klukai-ci-pull.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now klukai-ci-pull.timer
```

To take Dominus out of rotation, stop its controller:

```sh
sudo systemctl stop klukai-ci-runner
```

The heartbeat goes stale within 150 s, new runs stay on amarillo, and
amarillo takes over anything still queued.

## amarillo.

| Label set (`runs-on`) | VM | Jobs |
|---|---|---|
| `[self-hosted, Linux, X64, klukai-amarillo-heavy]` | 8 vCPU, 8 GiB (+ zram swap) | perf-gate (exclusive), existing nightly jobs |
| `[self-hosted, Linux, X64, klukai-amarillo-build]` | 4 vCPU, 4 GiB | ci-image (before serial suites) |
| `[self-hosted, Linux, X64, klukai-amarillo-serial]` | 2 vCPU, 4 GiB | components, test, real-backends |
| `[self-hosted, Linux, X64, klukai-amarillo-medium]` | 4 vCPU, 4 GiB | image-scan |
| `[self-hosted, Linux, X64, klukai-amarillo-frontend]` | 4 vCPU, 8 GiB (+ zram swap) | frontend |
| `[self-hosted, Linux, X64, klukai-amarillo-small]` | 2 vCPU, 3 GiB | source-policy/preflight/selection, lint, typecheck, security, final gates |

## How it works

- `klukai_ci_runner.py` (root, `klukai-ci-runner.service`) polls Klukai's
  queued jobs every 5 s. Small jobs get priority and admissible jobs backfill
  free capacity; a waiting large job does not hide smaller work. For each job whose labels match a row above exactly,
  it creates a copy-on-write disk over the base image and a seed with a
  single-use JIT runner registration (GitHub API, repository-scoped). It then
  boots the VM. The guest runs one job (`klukai-ci-job.service`) and powers off.
  The controller deletes the disk, the seed and any leftover registration.
- **Limits (owner):**
  - at most 8 CPU threads in total across VMs;
  - a VM starts only if amarillo keeps 4 GiB free beyond it and the CPU package
    is below 62 C;
  - running VMs pause at 68 C and resume below 60 C (amarillo's limit is 70 C);
  - a job VM is destroyed after 4 hours.
- **Isolation (production runs on this host):**
  - The guests sit on the `klukai-ci` libvirt network (`kci-br0`, NAT,
    10.231.0.0/24, no DHCP/DNS from the host). The guard `klukai-ci-guard.nft`
    blocks amarillo itself, the LAN, Tailscale, ZeroTier, Docker networks,
    reserved ranges and IPv6, so the public internet is all they reach. Ports
    are isolated, so concurrent guests cannot reach each other.
  - The only host path a guest sees is the CI evidence tree,
    `/docker/klukai/ci-artifacts`. It is mounted at `/opt/klukai-ci/artifacts`
    through virtiofs, and on the host it is bind-mounted `nosuid,nodev,noexec`.
    While no guest runs, the controller hands guest-written files to the tree's
    owner, which also clears setuid bits.
  - Inside a guest, the runner (uid 1000) may run only `apparmor_parser --replace`
    as root. A job-started hook refuses anything but Klukai's
    push/workflow_dispatch/schedule events.
- **Cross-job hand-off:** the `ci-image` job saves its executor image to
  `<sha>/<run>/ci-tool-image/image.tar`, and `test` and `real-backends` load it
  in their own VMs. The postgres/valkey/qdrant images and the build bases are
  baked into the base image.
- **Evidence:** `klukai-fetch-ci-artifacts.py` reads the same tree locally:
  - `image <sha>` picks it automatically;
  - `nightly` and `archive` take `--source amarillo`;
  - the hourly archive keeps the newest three revisions and moves older ones
    into `/docker/klukai/ci-records/` (90 days).

## Install (root)

```bash
sudo install -d -m 0755 /opt/klukai-ci
sudo install -m 0644 klukai-ci-net.xml klukai-ci-guard.nft klukai_ci_runner.py /opt/klukai-ci/
sudo install -m 0755 network-up.sh /opt/klukai-ci/
sudo install -m 0644 systemd/klukai-ci-{network,runner}.service /etc/systemd/system/
# GitHub token able to manage the repository's runners, encrypted to this host:
sudo install -d -m 0700 /etc/klukai-ci
printf '%s' "$TOKEN" | sudo systemd-creds encrypt --name=github-token --with-key=host+tpm2 - /etc/klukai-ci/github-token.cred
sudo systemctl daemon-reload && sudo systemctl enable --now klukai-ci-network.service
sudo python3 build-base.py --toolchain-dir <dir with the two toolchain snapshots>
sudo systemctl enable --now klukai-ci-runner.service
```

The toolchain snapshots are the Dominus heavy guest's verified
`toolchain-provenance-b838d34bba223857/snapshots/*.tar.gz` (digests are in
`build-base.py`). Don't rebuild them from amarillo's own Flutter/Chrome: Chrome
auto-updates.

## Operate

- Status: `systemctl status klukai-ci-runner`, `journalctl -u klukai-ci-runner`,
  `sudo virsh list` (guests are `klukai-ci-<12 hex>`).
- Rebuild the base image: `sudo systemctl stop klukai-ci-runner`, then wait for
  running guests to finish, then rerun `build-base.py`, then start the service.
  The manifest is in `/var/lib/libvirt/images/klukai-ci/base/manifest.json`.
  Then give Dominus the same bytes: stop its controller, copy `base.qcow2`,
  `base.qcow2.sha256`, `manifest.json` and `packages.tsv` as in the install steps, check the
  digest, then start it. Until then, stop Dominus's controller so both pools
  run on one base image.
- Roll back to Dominus: revert the `runs-on` labels in `ci.yml`/`nightly.yml`
  (and the two contract tests) to `[self-hosted, Linux, X64, ci-heavy]` /
  `[fleet-ci-v1, ci-small]`, then stop `klukai-ci-runner`.

## Push acceleration and release certification

Source admission still runs before checkout. The same first VM checks Ruff
formatting using only its existing hash-locked dependency, then selects the
complete push diff. Known component edits run affected suites; shared, unknown,
workflow and dependency edits run everything. An unavailable/empty diff fails
open to more tests. Retired Telegram files have no active test closure.

`Affected CI gates` validates the exact selected results, including expected
skips. `Required CI gates` certifies only full runs; routine pushes show this
job skipped in GitHub (GitHub may render a skipped check as successful). The
release relay independently requires every full named job to succeed. A full
`workflow_dispatch` with the exact `expected_revision` is required to certify
a routine-only commit for release. Dispatch always runs all gates, including
the independent no-cache image rebuild and unchanged paired performance test.
Superseded CI runs on the same ref are cancelled; deployment workflows retain
their existing concurrency policy.

Only reusable data survives job VMs: pip hash-verified downloads, npm integrity
cache, Trivy data, SHA256-pinned fonts and Docker/BuildKit transport materials.
Current source evidence and environments are rebuilt and revalidated per run.
BuildKit exports use unique run directories and atomic shared pointers; all
cached blobs are checked before import. Corrupt/missing caches become cold
builds. The performance job verifies and loads the current scanned candidate,
then uses a verified exact-revision baseline artifact or builds it using only
reusable dependency layers. Performance still runs alone on its eight threads; other jobs share a 12-thread budget. Its host harness installs the
complete eleven-package perf closure copied from the existing CI hash lock;
measured services still use the exact baseline and candidate images, with the
same six paired rounds, samples and password hashing.

Caches under the shared artifact tree contain no accounts or credentials.
They can be removed while CI is idle to force cold setup; do not prune active
run directories. While no guests run, the controller retains the two newest BuildKit exports
per role, any currently published cache pointer, and a one-hour grace period.
Only disposable cache payloads are pruned; terminal evidence and scanned image
tars keep their existing archival policy.
