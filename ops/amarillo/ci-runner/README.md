# Klukai CI runner on amarillo

Owner decision 2026-10-19: Klukai's CI no longer depends on Dominus. Every
queued Klukai job runs in its own fresh VM on amarillo, which is deleted after
the job. Other repositories keep the Dominus fleet runners.

| Label set (`runs-on`) | VM | Jobs |
|---|---|---|
| `[self-hosted, Linux, X64, klukai-amarillo-heavy]` | 8 vCPU, 8 GiB (+ zram swap) | ci-image, components, test, real-backends, image-scan, perf-gate, frontend, all nightly jobs |
| `[self-hosted, Linux, X64, klukai-amarillo-small]` | 2 vCPU, 3 GiB | source-policy, lint, typecheck, security, all-required |

## How it works

- `klukai_ci_runner.py` (root, `klukai-ci-runner.service`) polls Klukai's
  queued jobs every 20 s. For each job whose labels match a row above exactly,
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
- Roll back to Dominus: revert the `runs-on` labels in `ci.yml`/`nightly.yml`
  (and the two contract tests) to `[self-hosted, Linux, X64, ci-heavy]` /
  `[fleet-ci-v1, ci-small]`, then stop `klukai-ci-runner`.
