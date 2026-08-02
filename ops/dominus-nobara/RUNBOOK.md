# Dominus Nobara rebuild and cutover

This runbook rebuilds the lost Windows/WSL2 AI services on `dominus-nobara`
without installing application Python or CUDA packages into the Nobara system.
The host owns only the RAID mounts, NVIDIA driver/container runtime, Docker
Compose, Tailscale, and systemd. The preserved native-vLLM exception is a
self-contained RAID virtual environment at `/mnt/nvmer0/ai/vllm`; it installs
nothing into the OS Python. Model data and mutable service data live on
`/mnt/nvmer0`.

The migration is deliberately release-based and reversible. Never use
`docker compose down -v`, `docker system prune`, or delete an existing model
cache during this procedure.

## Non-negotiable invariants

- `/mnt/nvmer0` must be a real mounted filesystem before any Compose command.
  A missing mount must never turn into empty directories on the root disk.
- Every published port binds to the target Tailscale address
  `100.107.121.5`; nothing binds to `0.0.0.0` on the host.
- Every inter-host SSH, rsync, API, and browser connection uses Tailscale
  (`100.107.121.5` or its MagicDNS name). LAN addresses are never a fallback.
- The llama.cpp router admits at most one locked preset. b10200 checks its
  sleeper on an interval of up to one second, so the runtime uses a fixed
  `--sleep-idle-seconds 898` safety deadline. There is no environment override,
  its cache is an empty tmpfs, and `--offline` plus no additive model directory
  prevents discovery outside the lock. The gateway strips every client `ttl`.
  Effective idle residency therefore cannot exceed 900 seconds.
- Native vLLM is fully stopped, not put to sleep, at a fixed 895-second
  deadline. The proxy records monotonic state on the RAID only when bytes move
  or a connection opens/closes; an idle keep-alive cannot pin the model. A
  250 ms fail-closed watchdog stops vLLM on an expired, missing, malformed, or
  boot-mismatched record.
- ComfyUI and companion voice share a restart-safe, fixed 600-second GPU lease.
  The only workload names are `comfyui` and `companion-voice`; the only marker
  states are `active`, `cleaning`, and `cleanup_failed`. Acquisition drains
  current inference, unloads llama.cpp, waits for native vLLM quiescence, and
  blocks conflicting model loads until cleanup and release. Release or expiry
  cleans residue from both leased workload classes, regardless of which one
  held the lease. Expired, malformed, `cleaning`, and `cleanup_failed` markers
  stay fail-closed until positive cleanup succeeds; no reader silently deletes
  them. ComfyUI has no raw host port that can bypass this boundary.
- GameMode owns the GPU while a game is active. The start hook synchronously
  stops the entire canonical Compose user unit after creating the marker, then
  stops on-demand containers and verifies NVIDIA processes. The independent
  native vLLM proxy stays up only to return HTTP 503; `vllm-server.service`
  cannot start. Game end restores the stack through the guarded systemd unit,
  never raw Compose, and never reloads a model.
- The immutable model release is mounted read-only. Speaches and
  TranscriptionSuite receive separate, writable Hugging Face cache views made
  with verified XFS reflinks. No service may silently download a substitute.
- Pinned Speaches 0.8.3 cannot prove an explicit cleanup path for every model
  type, so the cutover service is CPU-only and receives no NVIDIA device. Its
  Whisper, Piper, and Kokoro managers all receive the same locked 600-second
  idle TTL. The wrapper rejects values above a server-owned 895-second cutoff:
  upstream uses a `threading.Timer`, so the five-second margin keeps scheduling
  jitter below the public 900-second residency policy. Unmanaged VAD, realtime,
  voice-chat, and diarization routes are removed, and STT forcibly disables
  its optional VAD filter. GPU Speaches remains disabled until a complete
  crash-safe cleanup contract exists.
- TranscriptionSuite and its bootstrap are defined for recovery evidence only
  and are hard-disabled. They have no host port or NVIDIA device. Do not start
  either profile until the exact gated model bytes, an exclusive GPU launcher/
  interlock, and tested inbound authentication all exist as one reviewed gate.
- The RAID arrays are RAID 0, not backups. Keep unique data and custom images
  on Amarillo and on another independent device. `/mnt/satar0` is also RAID 0
  and is only a second local copy, not sufficient protection by itself.
- Do not recover credentials from historical logs. Rotate the LM gateway,
  Speaches, voice, Hugging Face/CivitAI, and TranscriptionSuite credentials.
- Docker's data root, containerd content store, application state, logs,
  models, caches, and outputs all live on `/mnt/nvmer0`. Mount-gated root and
  user units prevent a missing RAID from creating root-filesystem fallbacks.
- The old Windows/WSL2 `dominus` installation is gone. It is historical
  evidence only—not a source, dependency, or rollback target. Rollback means a
  previously verified Nobara release/config/image set.
- Amarillo's dedicated staging directory remains in place until the remote
  SHA-256 pass, application acceptance, and explicit owner acceptance. Delete
  only that marked staging directory afterward; never delete surviving model
  caches.

## Service map

| Service | Host endpoint | Startup policy | Model behavior |
| --- | --- | --- | --- |
| LM Studio compatibility gateway | `100.107.121.5:1234` | base user unit | CPU only; stopped with the canonical unit during games |
| llama.cpp b10200 router | internal `:8080` | base user unit | lazy, one model, hard 900-second idle unload |
| Preserved native vLLM | loopback `127.0.0.1:8000` | lazy proxy + watchdog | locked AWQ coder; hard process stop by 900 seconds |
| Companion voice | `100.107.121.5:8301` | base lazy shell (`voice`) | lazy XTTS; explicit unload and 600-second TTS TTL |
| ComfyUI 0.29.2 | internal `:8188` through `:1234/api/v1/comfy` | base empty shell (`image`) | authenticated bounded lease; loads checkpoints only for jobs |
| Speaches 0.8.3 CPU | `100.107.121.5:8390` | base lazy shell (`speech`) | no NVIDIA device; lazy models at 600 seconds, hard runtime cutoff 895 seconds |
| TranscriptionSuite 1.3.7 | reserved internal `:9786`; no host endpoint | production-disabled | recovery definition only; bootstrap/API entrypoints reject starts and receive no GPU |

The pinned external linux/amd64 manifests are:

- llama.cpp: `sha256:657694ff6b0ceba64cbaed4502b2c3e5c52812c9911bc813cd1f65b3499b2e72`
- Speaches: `sha256:f48b50035b173ec4f78af1371a4f8a3f0e24c3c63aeaba6ef99a55d0eee7c1ff`
- TranscriptionSuite: `sha256:9b9587d6db3dbc6e06ab5df3498798a9f194bf853156fc2fe62b1b1771dba1b0`
- Python 3.12.11 slim (preflight, ComfyUI, materializer base):
  `sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49`

ComfyUI is built from release `v0.29.2`, commit
`322122449c9d2ba8b8df1bb517364527dd0615f1`, with a checked archive SHA-256
and a fully hashed Python lock. The compatibility gateway also builds from a
pinned base and dependency lock in this source tree. Companion voice has a
pinned direct Python requirement set, but its transitive Python and apt inputs
are not fully hash-hermetic; promote the accepted exported image by its
recorded image ID/digest instead of claiming that a fresh voice build is the
byte-exact artifact.

## 1. Record the starting state

Run these read-only checks on `dominus-nobara` and save the output with the
cutover notes. Stop if the expected mountpoint or Tailscale IP is absent.

```bash
mountpoint -q /mnt/nvmer0
findmnt /mnt/nvmer0
sudo mdadm --detail /dev/md1
df -hT /mnt/nvmer0 /mnt/satar0 /
tailscale ip -4
if [[ -n "${SSH_CONNECTION:-}" ]]; then
  read -r _ _ ssh_server_ip _ <<<"$SSH_CONNECTION"
  [[ "$ssh_server_ip" == 100.107.121.5 ]]
fi
nvidia-smi
docker version
docker compose version
docker info --format '{{json .Runtimes}}'
docker info --format 'DockerRootDir={{.DockerRootDir}} Driver={{.Driver}}'
sudo du -sh /var/lib/docker /var/lib/containerd
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
ss -lntp | grep -E ':(1234|8301|8390|9786|8000|8001|8188|8388)\b' || true
systemctl --user status vllm-proxy.service vllm-server.service vllm-idle-watcher.timer --no-pager
```

Preserve the existing Caddy failover and `brave-mcp` containers. This project
uses the Compose project name `dominus-ai-stack` and must not manipulate other
projects.

The surviving native-vLLM assets are target-only and must not be staged,
re-downloaded, moved, or deleted. Verify all 31 locked files (36,865,817,004
bytes) in place from the RAID model root:

```bash
cd /mnt/nvmer0/models
sha256sum --check --strict \
  /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara/preserved-target-models.sha256
test "$(sha256sum \
  /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara/preserved-target-models.sha256 \
  | awk '{print $1}')" = "$(jq -r '.ledger.sha256' \
  /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara/preserved-target-models.lock.json)"
```

This separately locks the AWQ primary at revision
`9971cd6828ce3eefdcd9e9ca72dc4586ede07379`, its 0.6B draft at
`c1899de289a04d12100db370d81485cdf75e47ca`, and the preserved Q4_K_M GGUF. A failed byte check
blocks native vLLM cutover; it is not repaired from the dead Windows host.

Fingerprint the surviving RAID vLLM environment independently:

```bash
accepted_packages=/mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara/config/vllm/requirements.accepted.txt
live_packages=/tmp/dominus-vllm-packages.live.txt
uv pip freeze --python /mnt/nvmer0/ai/vllm/bin/python | LC_ALL=C sort > "$live_packages"
test "$(wc -l < "$live_packages")" = 195
test "$(sha256sum "$live_packages" | awk '{print $1}')" = \
  b9d1d52a8813033c408b0670eef9f9698a0051cbb63844dd6dff61c5e9b05080
diff -u <(grep -vE '^(#|$)' "$accepted_packages") "$live_packages"
/mnt/nvmer0/ai/vllm/bin/python --version | grep -Fx 'Python 3.12.13'
```

This is an accepted fingerprint of the surviving environment—not a
hash-locked rebuild requirements file. Do not feed it to `pip install`; preserve
and back up `/mnt/nvmer0/ai/vllm` as one unit with its target-only models.

If this shell was reached over SSH, the assertion above proves its server end
is the locked Tailscale address. Disconnect immediately if it fails. The model
transfer script additionally verifies both ends of `SSH_CONNECTION` and that
the SSH alias resolves to `100.107.121.5` before either rsync or remote writes.

If NVIDIA Container Toolkit is absent, install it from NVIDIA's official RPM
repository, then run `sudo nvidia-ctk runtime configure --runtime=docker` and
restart Docker during an agreed interruption window. Re-run `docker info`
afterward. A metadata-only check is not enough; before cutover, validate the
runtime with the pinned CUDA 12.6.3 linux/amd64 manifest:

```bash
docker run --rm --gpus all \
  nvidia/cuda@sha256:2c8193530ecc423e0f123d0c85b68a15d1395adcddabfc943e2523dbfde172e1 \
  nvidia-smi
```

Do not restart Docker while a game, render, transcription, or existing vLLM
request is active.

## 2. Install the source and persistent directories

The systemd unit expects the Klukai source at this exact location:

```text
/mnt/nvmer0/services/ai-stack/source/klukai
```

This cutover is an **uncommitted recovery snapshot** based on a Git `HEAD`; the
base commit alone does not identify the files being deployed. After all static
checks and immediately before transfer, freeze source edits and capture every
Git-allowlisted tracked or untracked, non-ignored file. The capture records each
path's type, mode, size, and SHA-256 in canonical JSONL and hashes that complete
manifest into one aggregate source-tree digest. Secrets and model bytes remain
outside this source allowlist and have their own ledgers.

On Amarillo:

```bash
source_root=/home/jalsarraf/git/klukai
snapshot_parent=/home/jalsarraf/.local/state/dominus-nobara-recovery
snapshot_dir="$snapshot_parent/source-2026-08-01-v1"
install -d -m 0700 "$snapshot_parent"

python3 "$source_root/ops/dominus-nobara/scripts/capture-source-tree.py" \
  --source-root "$source_root" \
  --output-dir "$snapshot_dir"
git -C "$source_root" rev-parse HEAD >"$snapshot_dir/base-head.txt"
git -C "$source_root" status --porcelain=v1 -uall >"$snapshot_dir/base-status.txt"
install -m 0644 "$source_root/ops/dominus-nobara/models.lock.json" \
  "$snapshot_dir/models.lock.json"
(
  cd "$snapshot_dir"
  sha256sum models.lock.json >models.lock.sha256
  sha256sum --check --strict source-tree.sha256
  sha256sum --check --strict models.lock.sha256
)
```

`source-files.nul` is the complete deployment allowlist. Review it once, then
transfer exactly that set over the locked Tailscale SSH alias into a fresh
same-filesystem staging directory. Never rsync into an existing canonical tree:
that can retain stale files even when every allowlisted file verifies. Also copy
the immutable capture metadata outside the source tree:

```bash
ssh_target=dominus-nobara
target_source_parent=/mnt/nvmer0/services/ai-stack/source
target_source=/mnt/nvmer0/services/ai-stack/source/klukai
target_releases=/mnt/nvmer0/services/ai-stack/source/releases
target_snapshot=/mnt/nvmer0/services/ai-stack/backups/source-2026-08-01-v1
# Prove the transport immediately before the first remote write.  SSH_CONNECTION
# is `client-ip client-port server-ip server-port`; both ends must be Tailscale.
tailscale ip -4 | grep -Fxq 100.111.198.19
ssh "$ssh_target" 'set -eu
  set -- $SSH_CONNECTION
  test "$#" -eq 4
  test "$1" = 100.111.198.19
  test "$3" = 100.107.121.5
  test "$4" = 1227'
ssh "$ssh_target" \
  "test ! -e '$target_snapshot'; \
   install -d -m 0755 '$target_source_parent' '$target_releases'; \
   install -d -m 0700 '$target_snapshot'"
target_next=$(ssh "$ssh_target" "mktemp -d '$target_releases/.klukai-next.XXXXXX'")
case "$target_next" in
  "$target_releases"/.klukai-next.*) ;;
  *) echo "unsafe remote staging path" >&2; exit 1 ;;
esac
rsync -a --from0 --files-from="$snapshot_dir/source-files.nul" --relative \
  "$source_root/" "$ssh_target:$target_next/"
rsync -a "$snapshot_dir/" "$ssh_target:$target_snapshot/"
ssh "$ssh_target" \
  "python3 '$target_next/ops/dominus-nobara/scripts/capture-source-tree.py' \
    --source-root '$target_next' \
    --allowlist '$target_snapshot/source-files.nul' \
    --verify-manifest '$target_snapshot/source-files.jsonl' \
    --require-exact-set"

# Promotion uses coreutils 9.10's renameat2 exchange while every stack/model
# unit is stopped.  The canonical path therefore never disappears, even if
# the host loses power between promotion and archival.  A pre-existing partial
# canonical tree is retained inside this snapshot.
ssh "$ssh_target" sh -s -- "$target_source" "$target_next" "$target_snapshot" <<'REMOTE'
set -eu
canonical=$1
next=$2
snapshot=$3
archive=$snapshot/canonical-before-promotion
for unit in dominus-ai-stack.service vllm-server.service vllm-proxy.service \
  vllm-idle-watchdog.service vllm-idle-watcher.timer; do
  state=$(systemctl --user is-active "$unit" 2>/dev/null || true)
  case "$state" in
    inactive|failed) ;;
    *) echo "$unit must be inactive before source promotion" >&2; exit 1 ;;
  esac
done
running_project_containers=$(docker ps --quiet \
  --filter label=com.docker.compose.project=dominus-ai-stack)
test -z "$running_project_containers" || {
  echo "dominus-ai-stack containers must be stopped before source promotion" >&2
  exit 1
}
test -d "$next"
test ! -L "$next"
test ! -e "$archive"
chmod 0755 "$next"
if [ -e "$canonical" ] || [ -L "$canonical" ]; then
  mv -T --exchange --no-copy -- "$next" "$canonical"
  if ! mv -T --no-copy -- "$next" "$archive"; then
    # `next` still contains the old tree, so exchange restores the exact
    # pre-promotion state without leaving the canonical path absent.
    mv -T --exchange --no-copy -- "$next" "$canonical"
    exit 1
  fi
else
  mv -T --no-copy -- "$next" "$canonical"
fi
REMOTE
```

Any source edit after capture invalidates the snapshot: discard its metadata,
freeze again, and recapture instead of amending the manifest by hand. A failed
promotion restores the archived canonical tree; a successful one leaves it as
recoverable evidence below the snapshot. On the target, create only the
explicit service directories:

```bash
sudo install -d -o jalsarraf -g jalsarraf -m 0700 \
  /mnt/nvmer0/services/ai-stack/config

sudo install -d -o jalsarraf -g jalsarraf -m 0755 \
  /mnt/nvmer0/services/ai-stack/source \
  /mnt/nvmer0/services/ai-stack/config/tls \
  /mnt/nvmer0/services/ai-stack/config/transcriptionsuite \
  /mnt/nvmer0/services/ai-stack/models/releases \
  /mnt/nvmer0/services/ai-stack/cache/speech/releases \
  /mnt/nvmer0/services/ai-stack/backups

sudo install -d -o 10001 -g 10001 -m 0770 \
  /mnt/nvmer0/services/ai-stack/cache/comfyui \
  /mnt/nvmer0/services/ai-stack/cache/companion-voice \
  /mnt/nvmer0/services/ai-stack/data/comfyui/input \
  /mnt/nvmer0/services/ai-stack/data/comfyui/output \
  /mnt/nvmer0/services/ai-stack/data/comfyui/user

sudo install -d -o 10000 -g 10000 -m 0770 \
  /mnt/nvmer0/services/ai-stack/data/transcriptionsuite \
  /mnt/nvmer0/services/ai-stack/runtime/transcriptionsuite \
  /mnt/nvmer0/services/ai-stack/config/transcriptionsuite \
  /mnt/nvmer0/services/ai-stack/state/transcriptionsuite

sudo install -d -o 1000 -g 1001 -m 0770 \
  /mnt/nvmer0/services/ai-stack/cache/vllm \
  /mnt/nvmer0/services/ai-stack/cache/vllm/huggingface/hub \
  /mnt/nvmer0/services/ai-stack/cache/vllm/torch \
  /mnt/nvmer0/services/ai-stack/cache/vllm/triton \
  /mnt/nvmer0/services/ai-stack/cache/vllm/vllm \
  /mnt/nvmer0/services/ai-stack/state/vllm \
  /mnt/nvmer0/services/ai-stack/logs/vllm
```

Install the environment template as a private file and edit it locally on the
target. Never paste token values into a terminal command, chat, or journal.

```bash
install -m 0600 \
  /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara/config/stack.env.example \
  /mnt/nvmer0/services/ai-stack/config/stack.env
```

Provision fresh values for `LM_STUDIO_TOKEN`, `SPEACHES_API_KEY`, and
`VOICE_API_TOKEN`. The file must be a regular non-symlink owned by UID 1000
with mode exactly `0600`; the pre-render/start validator rejects any drift.
`HUGGINGFACE_TOKEN` may be empty for public models, but PyAnnote remains blocked
until its gated terms are accepted and the lock contains exact hashes.

Compose uses required-value interpolation plus a network-isolated one-shot for
the three public service tokens; an empty, whitespace-only, or shorter-than-32
character value aborts startup instead of silently disabling authentication.
Verify the deployed file without printing its values:

```bash
python3 - <<'PY'
from pathlib import Path

values = {}
for line in Path('/mnt/nvmer0/services/ai-stack/config/stack.env').read_text().splitlines():
    if line and not line.lstrip().startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        values[key] = value.strip()
for key in ('LM_STUDIO_TOKEN', 'SPEACHES_API_KEY', 'VOICE_API_TOKEN'):
    if len(values.get(key, '')) < 32:
        raise SystemExit(f'{key} must contain a new secret of at least 32 characters')
PY
```

For future TranscriptionSuite authentication work, obtain a Tailscale
certificate for the
machine's exact MagicDNS FQDN `dominus-nobara.tail9bdca.ts.net` and place the
certificate/key at the paths in `stack.env`. Keep the private key mode `0600`.
The currently provisioned certificate expires on 2026-10-31; expiration is a
renewal deadline, not a reason to disable TLS or fall back to an IP/LAN URL.

```bash
sudo tailscale cert \
  --cert-file /mnt/nvmer0/services/ai-stack/config/tls/dominus-nobara.crt \
  --key-file /mnt/nvmer0/services/ai-stack/config/tls/dominus-nobara.key \
  dominus-nobara.tail9bdca.ts.net
sudo chmod 0644 /mnt/nvmer0/services/ai-stack/config/tls/dominus-nobara.crt
sudo chmod 0600 /mnt/nvmer0/services/ai-stack/config/tls/dominus-nobara.key
```

Before any future enablement review, and at least 14 days before each recorded
expiry, verify the exact hostname, remaining lifetime, and permissions without
printing key material:

```bash
tls_dir=/mnt/nvmer0/services/ai-stack/config/tls
fqdn=dominus-nobara.tail9bdca.ts.net
openssl x509 -in "$tls_dir/dominus-nobara.crt" -noout -checkhost "$fqdn"
openssl x509 -in "$tls_dir/dominus-nobara.crt" -noout -checkend 1209600
test "$(stat -c '%a' "$tls_dir/dominus-nobara.crt")" = 644
test "$(stat -c '%a' "$tls_dir/dominus-nobara.key")" = 600
```

The current definition is disabled, so there is no safe in-place renewal/start
procedure to run. Future enablement must first add one exclusive start lock
shared by the launcher and renewal path, change Compose to bind a versioned
TLS release through one atomically replaced `current` pointer, and test it.
Under that lock, renewal must unconditionally stop the profile and positively
prove that no TranscriptionSuite container is running. It must stage both files
on the same filesystem, validate hostname/lifetime/modes, and compare the
public key derived from the staged private key with the certificate public key
before one atomic release-pointer promotion. Only after revalidation may a
future authenticated launcher proceed. Record the new `notAfter` date in the
encrypted operations ledger. Until that design lands, a failed `-checkend`
keeps both disabled entrypoints blocked; never replace the pair separately.

## 3. Move Docker and containerd storage onto the RAID

Docker 29 on this host uses the external containerd image store. Changing only
Docker's `data-root` leaves the much larger `/var/lib/containerd` on root, so
both stores must move in the same maintenance window. The existing root stores
remain untouched as the rollback copy until acceptance.

Use the checked, idempotent migration command from a Tailscale SSH session. The
explicit sudo environment preservation is required because sudo otherwise
drops `SSH_CONNECTION`; the script validates both Tailscale endpoints, the
mount/XFS features, inactive GameMode, accepted configs, and its exclusive
lock before stopping anything:

```bash
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
sudo --preserve-env=SSH_CONNECTION \
  ./scripts/migrate-container-storage.sh --preflight
sudo --preserve-env=SSH_CONNECTION ./scripts/migrate-container-storage.sh
sudo sed -n '1,160p' \
  /mnt/nvmer0/services/ai-stack/state/container-storage-v1/COMPLETE
```

The first command is read-only apart from its ephemeral `/run/lock` lock file:
it does not stop a service or create a migration ledger. The live run keeps
sorted, full container-ID and image-ID sets plus descriptive container, image,
volume, and network inventories and exact config/drop-in backups below. Docker
regenerates the IDs of its built-in `bridge`, `host`, and `none` networks on a
data-root restart, so those three are compared by name/driver/scope; every
custom network keeps an exact ID comparison
`/mnt/nvmer0/services/ai-stack/backups/container-storage-v1`. On startup or
verification failure it restores the old configs and runtime automatically,
leaves both RAID target stores for inspection, and marks an attempted start so
a later run cannot merge divergent metadata blindly. A completed rerun is
read-only verification. The identity snapshots omit status and timestamps, and
sorting prevents harmless daemon enumeration order from causing a false diff.
Never run the expanded commands below after the script; they document its
audited operations for manual recovery only.

First prove the XFS target features, save the live inventory, and install
mount-gated root-unit drop-ins from the checked source:

```bash
mountpoint -q /mnt/nvmer0
sudo xfs_info /mnt/nvmer0 | grep -q 'ftype=1'
sudo xfs_info /mnt/nvmer0 | grep -q 'reflink=1'
command -v rsync

sudo install -d -o jalsarraf -g jalsarraf -m 0700 \
  /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01
docker ps -a --no-trunc --format '{{.ID}}\t{{.Image}}\t{{.Names}}' | sort \
  > /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/containers.before.txt
docker image ls --all --no-trunc --format '{{.ID}}\t{{.Repository}}:{{.Tag}}' | sort \
  > /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/images.before.txt
sudo cp -a /etc/docker/daemon.json \
  /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/daemon.json.before
sudo cp -a /etc/containerd/config.toml \
  /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/containerd-config.toml.before

sudo install -d /etc/systemd/system/docker.service.d \
  /etc/systemd/system/containerd.service.d
sudo install -m 0644 systemd/docker.service.d/10-dominus-nvme-mount.conf \
  /etc/systemd/system/docker.service.d/10-dominus-nvme-mount.conf
sudo install -m 0644 systemd/containerd.service.d/10-dominus-nvme-mount.conf \
  /etc/systemd/system/containerd.service.d/10-dominus-nvme-mount.conf
```

Schedule an interruption for Caddy, `brave-mcp`, and any other current
container, then stop both engines before the stable copies. Do not use `mv`,
`rm`, `--delete`, or a trailing glob:

```bash
sudo systemctl stop docker.service docker.socket containerd.service
systemctl is-active --quiet docker.service && exit 1 || true
systemctl is-active --quiet containerd.service && exit 1 || true

sudo install -d -o root -g root -m 0711 \
  /mnt/nvmer0/docker-data /mnt/nvmer0/containerd
sudo rsync -aHAX --numeric-ids --info=progress2 \
  /var/lib/docker/ /mnt/nvmer0/docker-data/
sudo rsync -aHAX --numeric-ids --info=progress2 \
  /var/lib/containerd/ /mnt/nvmer0/containerd/
```

Merge Docker's data root without replacing the existing NVIDIA runtime:

```bash
sudo python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path('/etc/docker/daemon.json')
document = json.loads(path.read_text(encoding='utf-8'))
if 'nvidia' not in document.get('runtimes', {}):
    raise SystemExit('refusing to lose the existing NVIDIA runtime')
old = document.get('data-root')
if old not in (None, '/var/lib/docker', '/mnt/nvmer0/docker-data'):
    raise SystemExit(f'refusing to replace unexpected data-root {old!r}')
document['data-root'] = '/mnt/nvmer0/docker-data'
temporary = path.with_name('.daemon.json.dominus.partial')
temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY
sudo restorecon -v /etc/docker/daemon.json
sudo dockerd --validate --config-file /etc/docker/daemon.json
```

Preserve `disabled_plugins = ["cri"]` and change only containerd's root. The
branch below intentionally aborts on an unfamiliar configuration:

```bash
grep -Fxq 'disabled_plugins = ["cri"]' /etc/containerd/config.toml
if grep -Fxq 'root = "/mnt/nvmer0/containerd"' /etc/containerd/config.toml; then
  true
elif grep -Fxq '#root = "/var/lib/containerd"' /etc/containerd/config.toml; then
  sudo sed -i \
    's|^#root = "/var/lib/containerd"$|root = "/mnt/nvmer0/containerd"|' \
    /etc/containerd/config.toml
else
  echo 'unexpected containerd root configuration; restore backups and stop' >&2
  exit 1
fi
sudo restorecon -v /etc/containerd/config.toml
```

Reload and start in dependency order, then prove both roots and the NVIDIA
runtime. Compare the before/after ledgers before continuing:

```bash
sudo systemctl daemon-reload
sudo systemd-analyze verify docker.service containerd.service
sudo systemctl start containerd.service docker.socket docker.service
sudo systemctl is-active --quiet containerd.service docker.service

docker info --format '{{.DockerRootDir}}' | grep -Fx /mnt/nvmer0/docker-data
test "$(sudo containerd config dump \
  | awk -F"'" '$1 == "root = " {print $2; exit}')" = /mnt/nvmer0/containerd
docker info --format '{{json .Runtimes}}' | grep -q 'nvidia'
findmnt --target /mnt/nvmer0/docker-data
findmnt --target /mnt/nvmer0/containerd
systemctl cat docker.service containerd.service | grep -F 'RequiresMountsFor=/mnt/nvmer0'
test "$(systemctl cat docker.service containerd.service \
  | grep -Fc 'ExecStartPre=/usr/bin/mountpoint --quiet /mnt/nvmer0')" = 2

docker ps -a --no-trunc --format '{{.ID}}\t{{.Image}}\t{{.Names}}' | sort \
  > /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/containers.after.txt
docker image ls --all --no-trunc --format '{{.ID}}\t{{.Repository}}:{{.Tag}}' | sort \
  > /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/images.after.txt
diff -u \
  /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/containers.before.txt \
  /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/containers.after.txt
diff -u \
  /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/images.before.txt \
  /mnt/nvmer0/services/ai-stack/backups/docker-migration-2026-08-01/images.after.txt
```

Image order may differ, but no pre-existing container or image ID may be
missing. Start and health-check the preserved Caddy failover and `brave-mcp`
containers through their existing procedures. Do not remove either old root
directory; the rollback section restores their saved configurations.

## 4. Stage, transfer, and verify the immutable model release

The lock file is `models.lock.json`. It records exact sources, revisions,
sizes, hashes, confidence, and known reconstruction limits. The scripts refuse
broad paths, limit parallelism to 20, quarantine bad partials, never use
`--delete`, and never remove staging automatically.

On Amarillo:

```bash
cd /home/jalsarraf/git/klukai/ops/dominus-nobara
STAGING_ROOT=/home/jalsarraf/dominus-model-staging-20260801-v1 \
DOWNLOAD_JOBS=20 \
  ./scripts/stage-models.sh

STAGING_ROOT=/home/jalsarraf/dominus-model-staging-20260801-v1 \
VERIFY_JOBS=20 \
  ./scripts/verify-models.sh

STAGING_ROOT=/home/jalsarraf/dominus-model-staging-20260801-v1 \
  ./scripts/transfer-models.sh
```

If a gated repository is enabled later, load its token into the process
environment without printing it, re-lock every file SHA-256, then rerun the
same idempotent commands. Do not enable a snapshot with a null or guessed
hash.

On `dominus-nobara`, independently verify the release marker and all files:

```bash
cd /mnt/nvmer0/services/ai-stack/models/releases/dominus-wsl2-rebuild-2026-08-01-v1
sha256sum --check --strict .manifest/SHA256SUMS
sha256sum .manifest/models.lock.json
cat .manifest/models.lock.sha256
```

The last two hashes must match. Do not flip `current` if any file fails.

Record the old targets, then atomically select the verified release:

```bash
readlink -e /mnt/nvmer0/services/ai-stack/models/current || true
readlink -e /mnt/nvmer0/services/ai-stack/cache/speech/current || true

cd /mnt/nvmer0/services/ai-stack/models
ln -sfn releases/dominus-wsl2-rebuild-2026-08-01-v1 current.next
mv -Tf current.next current

sudo install -d -o 1000 -g 1001 -m 0770 \
  /mnt/nvmer0/services/ai-stack/cache/speech/releases/dominus-wsl2-rebuild-2026-08-01-v1/speaches-hf \
  /mnt/nvmer0/services/ai-stack/cache/speech/releases/dominus-wsl2-rebuild-2026-08-01-v1/transcriptionsuite-hf
cd /mnt/nvmer0/services/ai-stack/cache/speech
ln -sfn releases/dominus-wsl2-rebuild-2026-08-01-v1 current.next
mv -Tf current.next current
```

Existing containers retain the old bind mount after a symlink flip. Recreate,
not merely restart, containers when changing releases.

## 5. Build the writable offline HF cache views

Speaches 0.8.3 discovers only standard Hugging Face cache trees and needs
model-card metadata. TranscriptionSuite 1.3.7 also requires a writable
`/models`; its entrypoint recursively chowns that path. Therefore neither
service writes into the immutable release.

Build the materializer image, then run its network-isolated runtime without
starting dependencies or pulling a replacement image:

```bash
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile maintenance build hf-cache-materialize
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile maintenance run --rm --no-deps --pull never hf-cache-materialize
```

The command must end with JSON containing `"status": "ok"` and
`"network_used": false`. It validates source and destination hashes, tries
`cp --reflink=always`, and falls back to `cp --reflink=auto` only if reflinks
are unsupported. It is idempotent.

After materialization, give the TranscriptionSuite cache to its locked app UID;
its upstream entrypoint also enforces this ownership. Before a future
re-materialization, stop TranscriptionSuite and temporarily return only that
cache tree to UID 1000, run the materializer, then repeat this handoff:

```bash
transcription_cache_release=$(readlink -e \
  /mnt/nvmer0/services/ai-stack/cache/speech/current/transcriptionsuite-hf)
[[ "$transcription_cache_release" == \
  /mnt/nvmer0/services/ai-stack/cache/speech/releases/*/transcriptionsuite-hf ]]
sudo chown -R 10000:10000 "$transcription_cache_release"
sudo chmod -R u+rwX,go-rwx "$transcription_cache_release"
```

Verify both cache views and ensure they remain on the NVMe XFS mount:

```bash
findmnt --target /mnt/nvmer0/services/ai-stack/cache/speech/current
find /mnt/nvmer0/services/ai-stack/cache/speech/current \
  \( -path '*/refs/main' -o -path '*/snapshots/*/README.md' \) -print
du -sh /mnt/nvmer0/services/ai-stack/cache/speech/current/*
```

Expected Speaches cache repositories include
`Systran/faster-whisper-large-v3` and
`speaches-ai/Kokoro-82M-v1.0-ONNX`. Expected TranscriptionSuite cache
repositories include `nvidia/parakeet-tdt-0.6b-v3` and the shared Whisper
snapshot. The exact PyAnnote Community-1 snapshot is a hard blocker for
diarization until its gated files are enabled and hashed in the lock.

## 6. Pull and build container images

Make the entire Compose graph machine checked. Only the three authenticated
host ports are literal Tailscale binds—there is no `TAILSCALE_IPV4` override
that can turn them into `0.0.0.0`. TranscriptionSuite has only a reserved
internal `expose` and is hard-disabled. Never redirect a fully interpolated
render to disk: it contains every service secret. Persist only a mode-`0600`
`--no-interpolate` contract. The Python validator performs its real render in
memory and emits status only. The b10200 deadline is 898 seconds because its
one-second polling interval must still fit under the 900-second maximum:

```bash
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
umask 077
install -d -m 0755 /run/user/1000/dominus-gpu
test ! -L /run/user/1000/dominus-gpu
test "$(stat -c '%u:%a' /run/user/1000/dominus-gpu)" = "1000:755"
snapshot_dir=/mnt/nvmer0/services/ai-stack/backups/source-2026-08-01-v1
compose_contract=$(mktemp "$snapshot_dir/.compose.no-interpolate.XXXXXX.json")
trap 'rm -f -- "$compose_contract"' EXIT
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile '*' config --no-interpolate --format json >"$compose_contract"
chmod 0600 "$compose_contract"
python3 scripts/verify-compose-contract.py \
  --compose-file compose.yaml \
  --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --source-root /mnt/nvmer0/services/ai-stack/source/klukai \
  --raid-root /mnt/nvmer0
jq -e '
  [.services[].ports[]? | .host_ip] as $ips |
  ($ips | length == 3 and all(. == "100.107.121.5")) and
  ((.services.comfyui.ports // []) | length == 0) and
  ((.services.transcriptionsuite.ports // []) | length == 0) and
  (.services.transcriptionsuite.environment.TLS_ENABLED == "true") and
  (.services.transcriptionsuite.environment.DOMINUS_TRANSCRIPTION_PRODUCTION_ENABLED == "false") and
  (.networks["ai-internal"].internal == true) and
  (.services["llama-router"].command | index("--offline") != null) and
  (.services["llama-router"].command | index("--models-dir") == null) and
  (.services["llama-router"].command as $c |
    $c[($c | index("--sleep-idle-seconds")) + 1] == "898" and
    $c[($c | index("--models-max")) + 1] == "1")
' "$compose_contract"
mv -T -- "$compose_contract" "$snapshot_dir/compose.no-interpolate.json"
trap - EXIT
```

The Python gate first rejects a symlink, wrong owner, or mode other than `0600`
for `stack.env`. It also rejects any durable bind that resolves outside the RAID
source/data trees, any bind with implicit host-path creation, a raw ComfyUI
port, a non-Tailscale published port, and any NVIDIA device assigned to
Speaches. The user unit runs the same live gate before every start/reload.

Pull immutable images and build local images before enabling systemd:

```bash
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile '*' pull stack-preflight llama-router speaches transcriptionsuite transcriptionsuite-bootstrap
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile '*' build lmstudio-compat comfyui companion-voice hf-cache-materialize
```

Capture `docker image inspect` output and export the four local application
images as one checked Docker archive. A local tag alone is not an immutable
rollback identity. Tie the captured image IDs/digests and archive hash to the
aggregate source tree, base `HEAD`, and exact model lock in one provenance
record:

```bash
set -Eeuo pipefail
umask 077
snapshot_dir=/mnt/nvmer0/services/ai-stack/backups/source-2026-08-01-v1
docker image inspect \
  dominus-lmstudio-compat:2026-08-01 \
  dominus-comfyui:0.29.2 \
  dominus-companion-voice:2026-08-01 \
  dominus-hf-cache-materializer:2026-08-01 \
  | jq -S '[.[] | {Id, RepoTags, RepoDigests}]' \
  >"$snapshot_dir/application-images.json"
chmod 0600 "$snapshot_dir/application-images.json" \
  "$snapshot_dir/compose.no-interpolate.json"
archive_partial="$snapshot_dir/application-images.docker.tar.zst.partial"
archive_final="$snapshot_dir/application-images.docker.tar.zst"
test ! -e "$archive_partial"
test ! -e "$archive_final"
docker image save \
  dominus-lmstudio-compat:2026-08-01 \
  dominus-comfyui:0.29.2 \
  dominus-companion-voice:2026-08-01 \
  dominus-hf-cache-materializer:2026-08-01 \
  | zstd --threads=4 -3 -o "$archive_partial"
zstd --test "$archive_partial"
archive_manifest=$(mktemp "$snapshot_dir/.application-images-manifest.XXXXXX.json")
trap 'rm -f -- "$archive_manifest"' EXIT
zstd --decompress --stdout "$archive_partial" \
  | tar --extract --to-stdout --file=- manifest.json >"$archive_manifest"
jq -e '
  length == 4 and
  all(.[];
    (.Config | type == "string" and length > 0) and
    (.Layers | type == "array" and length > 0) and
    (.RepoTags | type == "array" and length == 1)) and
  ([.[].RepoTags[]] | sort) == [
    "dominus-comfyui:0.29.2",
    "dominus-companion-voice:2026-08-01",
    "dominus-hf-cache-materializer:2026-08-01",
    "dominus-lmstudio-compat:2026-08-01"
  ]
' "$archive_manifest"
rm -f -- "$archive_manifest"
trap - EXIT
mv -T -- "$archive_partial" "$archive_final"
chmod 0600 "$archive_final"
(
  cd "$snapshot_dir"
  sha256sum application-images.json >application-images.sha256
  sha256sum application-images.docker.tar.zst >application-images.docker.tar.zst.sha256
  sha256sum compose.no-interpolate.json >compose.no-interpolate.sha256
)

jq -n -S \
  --arg base_head "$(cat "$snapshot_dir/base-head.txt")" \
  --arg source_tree_sha256 "$(awk '{print $1}' "$snapshot_dir/source-tree.sha256")" \
  --arg models_lock_sha256 "$(awk '{print $1}' "$snapshot_dir/models.lock.sha256")" \
  --arg application_images_sha256 "$(awk '{print $1}' "$snapshot_dir/application-images.sha256")" \
  --arg application_archive_sha256 "$(awk '{print $1}' "$snapshot_dir/application-images.docker.tar.zst.sha256")" \
  --arg compose_contract_sha256 "$(awk '{print $1}' "$snapshot_dir/compose.no-interpolate.sha256")" \
  '{schema_version:1, base_head:$base_head,
    source_tree_sha256:$source_tree_sha256,
    models_lock_sha256:$models_lock_sha256,
    application_images_sha256:$application_images_sha256,
    application_archive_sha256:$application_archive_sha256,
    compose_no_interpolate_sha256:$compose_contract_sha256}' \
  >"$snapshot_dir/provenance.json"
(
  cd "$snapshot_dir"
  sha256sum provenance.json >provenance.sha256
)
```

Verify all six digest files before promotion:

```bash
(
  cd "$snapshot_dir"
  sha256sum --check --strict \
    source-tree.sha256 models.lock.sha256 application-images.sha256 \
    application-images.docker.tar.zst.sha256 \
    compose.no-interpolate.sha256 provenance.sha256
)
```

Copy the complete snapshot directory—including the checked image archive—back
to Amarillo over the same proven Tailscale SSH path. Amarillo's `/home` Btrfs
filesystem is the independent non-RAID off-host copy; a further offline copy
is recommended but is not a cutover gate. Recheck every digest after transfer.
A fresh voice build is not an exact replacement for the captured voice image
ID because its transitive/apt graph is not fully hash-hermetic.

## 7. Install the user units and common game guard

Back up the existing Nobara GameMode configuration, proxy, and units to the
RAID first. The old minute watcher is not strict: its observed unload was 949
seconds. Replace it with the monotonic proxy/deadline watchdog while preserving
the locked vLLM environment, model, loopback ports 8000/8001, and serve script.

```bash
set -Eeuo pipefail
install -d -m 0700 \
  /mnt/nvmer0/services/ai-stack/backups/user-units-2026-08-01
cp -a /home/jalsarraf/.config/gamemode.ini \
  /home/jalsarraf/.config/systemd/user/vllm-proxy.service \
  /home/jalsarraf/.config/systemd/user/vllm-server.service \
  /home/jalsarraf/.config/systemd/user/vllm-idle-watcher.service \
  /home/jalsarraf/.config/systemd/user/vllm-idle-watcher.timer \
  /home/jalsarraf/.local/bin/vllm-proxy.py \
  /home/jalsarraf/.local/bin/vllm-idle-watcher.sh \
  /mnt/nvmer0/services/ai-stack/backups/user-units-2026-08-01/

systemctl --user stop vllm-server.service vllm-proxy.service \
  vllm-idle-watcher.service || true
systemctl --user disable --now vllm-idle-watcher.timer
stale_activity_backup=/mnt/nvmer0/services/ai-stack/backups/user-units-2026-08-01/stale-activity
install -d -m 0700 "$stale_activity_backup"
archive_stale_activity() {
  local source_path=$1
  local destination_path=$2
  if [[ -e "$source_path" || -L "$source_path" ]]; then
    if [[ -e "$destination_path" || -L "$destination_path" ]]; then
      printf 'refusing to overwrite stale-activity archive: %s\n' \
        "$destination_path" >&2
      return 1
    fi
    mv -T -- "$source_path" "$destination_path" || return 1
  fi
}
archive_stale_activity \
  /mnt/nvmer0/services/ai-stack/state/vllm/activity-state.json \
  "$stale_activity_backup/activity-state.json" || exit 1
archive_stale_activity \
  /home/jalsarraf/.cache/vllm-proxy/last-activity \
  "$stale_activity_backup/last-activity" || exit 1

install -d /home/jalsarraf/.local/bin \
  /home/jalsarraf/.config/systemd/user/vllm-server.service.d
install -m 0755 config/vllm/dominus-vllm-proxy.py \
  /home/jalsarraf/.local/bin/dominus-vllm-proxy.py
install -m 0755 config/vllm/dominus-vllm-idle-watchdog.py \
  /home/jalsarraf/.local/bin/dominus-vllm-idle-watchdog.py
install -m 0644 config/vllm/dominus_gpu_lease.py \
  /home/jalsarraf/.local/bin/dominus_gpu_lease.py
install -m 0755 config/vllm/dominus-vllm-vram-preflight \
  /home/jalsarraf/.local/bin/dominus-vllm-vram-preflight
install -m 0755 config/gpu-guard/verify-canonical-gpu-processes.py \
  /home/jalsarraf/.local/bin/dominus-verify-canonical-gpu-processes
install -m 0755 config/gpu-guard/game-start.sh \
  /home/jalsarraf/.local/bin/dominus-gpu-game-start
install -m 0755 config/gpu-guard/game-end.sh \
  /home/jalsarraf/.local/bin/dominus-gpu-game-end
install -m 0644 systemd/vllm-server.service.d/10-dominus-game-guard.conf \
  /home/jalsarraf/.config/systemd/user/vllm-server.service.d/10-dominus-game-guard.conf
install -m 0644 systemd/dominus-ai-stack.service \
  /home/jalsarraf/.config/systemd/user/dominus-ai-stack.service
install -m 0644 systemd/vllm-proxy.service \
  /home/jalsarraf/.config/systemd/user/vllm-proxy.service
install -m 0644 systemd/vllm-idle-watchdog.service \
  /home/jalsarraf/.config/systemd/user/vllm-idle-watchdog.service
```

Merge `config/gpu-guard/gamemode.ini.snippet` into
`/home/jalsarraf/.config/gamemode.ini`, replacing its existing `[custom]`
section instead of creating a duplicate. The start hook must return nonzero if
its marker cannot be created or any canonical GPU process remains active. The
start hook stops the complete canonical unit, including CPU-only Speaches, to
close start/reload races. The end hook restores empty/lazy voice, image,
speech, llama, and gateway shells through that unit, never raw Compose or
weights. The native proxy/watchdog remain independently guarded.

```bash
systemctl --user daemon-reload
systemd-analyze --user verify \
  /home/jalsarraf/.config/systemd/user/dominus-ai-stack.service \
  /home/jalsarraf/.config/systemd/user/vllm-proxy.service \
  /home/jalsarraf/.config/systemd/user/vllm-idle-watchdog.service \
  vllm-server.service
sudo loginctl enable-linger jalsarraf
systemctl --user is-enabled vllm-idle-watcher.timer && exit 1 || true
```

Stop here after installation and `daemon-reload`. Do not enable or start the
new units in the installation phase. Enabling and starting are deliberately
separate and require explicit owner GPU clearance in section 8.

The user unit has explicit `mountpoint`, model-release, Docker, and Tailscale
prechecks. It retries on failure. The native proxy, watchdog, state, and logs
are also mount-gated; a missing RAID stops or refuses the backend instead of
creating a fallback below `/mnt`. The archived records above are stale activity
timestamps only; the dated backup retains them and the previous Nobara
scripts/units.

## 8. Start the base lazy service plane

Only after explicit owner GPU clearance, confirm the game marker is absent.
Before enabling anything, prove that the non-root runtime UIDs can write every
mutable bind. Although these probes replace the normal entrypoint with a shell,
the ComfyUI and companion-voice Compose services still request an NVIDIA
device; that is why this block is inside the clearance boundary. `--pull never`
also prevents a mutable image replacement during acceptance:

```bash
test ! -e /run/user/1000/dominus-gpu/game-active
cd /mnt/nvmer0/services/ai-stack/source/klukai/ops/dominus-nobara
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile image run --rm --no-deps --pull never --user 10001:10001 --entrypoint sh comfyui \
  -ec 'for d in /cache /data/input /data/output /data/user; do p=$(mktemp "$d/.write-probe.XXXXXX"); rm -f "$p"; done'
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile voice run --rm --no-deps --pull never --user 10001:10001 --entrypoint sh companion-voice \
  -ec 'p=$(mktemp /app/models/.write-probe.XXXXXX); rm -f "$p"'
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile speech run --rm --no-deps --pull never --user 1000:1001 --entrypoint sh speaches \
  -ec 'p=$(mktemp /home/ubuntu/.cache/huggingface/.write-probe.XXXXXX); rm -f "$p"'
```

Do not bypass either disabled TranscriptionSuite entrypoint for a write probe.
Its host directories remain unused recovery state until all enablement gates
exist.

After the probes pass, enable the guarded units. The base start restores five
lightweight/lazy containers plus the native vLLM proxy/watchdog. Speaches is
CPU-only; the other service shells must not load weights during health probes.
Record a VRAM baseline immediately before and after:

```bash
test ! -e /run/user/1000/dominus-gpu/game-active
systemctl --user enable dominus-ai-stack.service \
  vllm-proxy.service vllm-idle-watchdog.service
systemctl --user start vllm-idle-watchdog.service vllm-proxy.service
systemctl --user start dominus-ai-stack.service
systemctl --user status dominus-ai-stack.service --no-pager
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env ps
curl --fail http://100.107.121.5:1234/health
curl --fail -H 'Authorization: Bearer <new-token>' \
  http://100.107.121.5:1234/api/v0/models
curl --fail http://100.107.121.5:8301/health
curl --fail http://100.107.121.5:1234/health | jq -e '.comfyui_status == "ok"'
curl --fail http://100.107.121.5:8390/health
systemctl --user is-active --quiet vllm-proxy.service
systemctl --user is-active --quiet vllm-idle-watchdog.service
systemctl --user is-active --quiet vllm-server.service && exit 1 || true
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  ps --status running --services | grep -Fx transcriptionsuite && exit 1 || true
unexpected_public_bind=$(ss -H -lnt | awk \
  '$4 ~ /:(1234|8301|8390)$/ && $4 !~ /^100\.107\.121\.5:/ {print $4}')
test -z "$unexpected_public_bind"
test -z "$(ss -H -lnt | awk '$4 ~ /:9786$/ {print $4}')"
raw_comfy_bind=$(ss -H -lnt | awk '$4 ~ /:(8188|8388)$/ {print $4}')
test -z "$raw_comfy_bind"
unexpected_native_bind=$(ss -H -lnt | awk \
  '$4 ~ /:(8000|8001)$/ && $4 !~ /^127\.0\.0\.1:/ {print $4}')
test -z "$unexpected_native_bind"
nvidia-smi
```

The catalog must contain exactly the enabled runtime models in
`models.lock.json` (currently 21), with no mmproj or image artifact exposed as
a fake model. Before the first request, every model state must be not-loaded,
voice health must report XTTS unloaded, and no ComfyUI checkpoint may appear in
VRAM. Speaches must have no Docker device request and may use host RAM only
after a real speech request. Any health-triggered weight load blocks cutover.

## 9. LLM parity and residency test matrix

Test each of the 20 chat/VLM presets sequentially and the Nomic embedding
preset through `/v1/embeddings`. Use the first `runtime.aliases` value from the
lock as the request model. For VLMs, add a known local test image and verify
multimodal input; a text-only success is not a VLM test. Check tool-call JSON
and `reasoning_content` for the fleet's Venice and Qwen distillation routes.

After each model:

1. Confirm a response and correct model ID.
2. Confirm only that one model is resident.
3. Call the compatibility unload endpoint and confirm VRAM is released.
4. Record latency, peak VRAM, context used, and pass/fail.

The final Unsloth Gemma preset uses the recovered loaded context `134144`; its
advertised maximum context remains `262144`. Do not silently lower the loaded
context without recording a compatibility exception.

Hard TTL acceptance is mandatory:

1. Submit an inference request containing a deliberately excessive client
   field such as `"ttl": 999999`.
2. Verify the upstream request logged by the gateway does not contain `ttl`.
3. Record a monotonic timestamp at response completion, then make no inference
   requests. Health, props, metrics, and catalog checks do not renew the timer.
4. Poll unloaded state from 898 seconds onward and prove that model VRAM is gone
   no later than 900 seconds after response completion.
5. Inspect the running command and verify `--sleep-idle-seconds 898`,
   `--models-max 1`, and `--offline` are literal and `--models-dir` is absent.
   Any residency after 900 seconds is a failed cutover gate.

Test native vLLM separately through its preserved loopback proxy on port 8000:

1. Confirm the old `vllm-idle-watcher.timer` is disabled and the new watchdog
   is active.
2. Make one request, then inspect the RAID JSON state. It must contain the
   current boot ID, `max_idle_ttl_seconds: 900`, and a hard deadline exactly
   895 seconds after the last real byte/connection activity.
3. Leave an HTTP keep-alive connection open without sending bytes. It must not
   renew the deadline.
4. Prove `vllm-server.service`, its complete cgroup, process RSS, and model VRAM
   are all gone by 900 seconds. vLLM sleep mode does not pass.
5. Run the watchdog unit tests for absent, malformed, wrong-boot, wrong-TTL,
   future-dated, expired, and valid records. For one live fail-closed test,
   activate the backend through a fresh request and remove its state file; the
   watchdog must stop it within its next 250 ms pass. Restore only through a
   fresh proxy request, never by copying stale activity.
6. When free VRAM is below the fixed 22650 MiB preflight floor, the proxy must
   return JSON HTTP 503 immediately and must not wait for the old 300-second
   cold-start timeout. A healthy cold start has its own 75-second maximum.

Ports remain loopback `127.0.0.1:8000`/`:8001`; do not expose native vLLM on a
LAN address or bypass its proxy.

## 10. Service profile acceptance

Start and test one profile at a time. Do not keep multiple large GPU models
resident during validation.

### ComfyUI

```bash
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile image up -d --no-build comfyui
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  exec -T comfyui python - <<'PY'
import json
import urllib.request

for endpoint in ('system_stats', 'object_info'):
    with urllib.request.urlopen(f'http://127.0.0.1:8188/{endpoint}', timeout=10) as response:
        document = json.load(response)
        print(endpoint, type(document).__name__)
PY
```

The object inventory must include these exact restored names:

- `noobai_xl_v1.safetensors`
- `sd_xl_base_1.0.safetensors`
- `sdxl_turbo.safetensors`
- `animagine_xl_31.safetensors`
- `sdxl_lightning_4step.safetensors`
- `Klukai_GFL2_IL-03.safetensors`

Run a NoobAI + `Klukai_GFL2_IL-03.safetensors` workflow and an SDXL Turbo
workflow. Confirm inputs, outputs, and user state land under the explicit
`/data/input`, `/data/output`, and `/data/user` bind mounts. FLUX must remain
absent; its recovered files were zero bytes and had been intentionally
removed. Submit real jobs through Klukai core so it acquires the bounded lease;
never publish or call raw ports 8188/8388 from another host.

### Companion voice

```bash
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile voice up -d --no-build companion-voice
curl --fail http://100.107.121.5:8301/health
```

Health must report lazy loading and must not consume XTTS VRAM. Test one TTS
request using the new bearer token, verify the reference WAV from the locked
release, call `/unload`, and confirm VRAM is released. Test `base.en` STT from
the local locked directory.

### Speaches (CPU-isolated cutover mode)

```bash
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  --profile speech up -d --no-build speaches
curl --fail -H 'Authorization: Bearer <new-token>' \
  http://100.107.121.5:8390/v1/models
```

The local registry must show `Systran/faster-whisper-large-v3` and
`speaches-ai/Kokoro-82M-v1.0-ONNX` while `HF_HUB_OFFLINE=1`. Run one STT and
one TTS request. Any attempted network download or missing model-card error is
a failed cache-materialization gate. Verify `WHISPER__INFERENCE_DEVICE=cpu`,
`WHISPER__COMPUTE_TYPE=int8`, no Docker device request, and no Speaches PID in
the NVIDIA compute-process list. After 600 idle seconds, `/api/ps` must contain
no Whisper model and process RSS must return near the recorded empty-shell
baseline. Confirm VAD timestamps, realtime, voice-chat, and diarization routes
return 404, and an STT request cannot enable `vad_filter`. Any configured STT
or TTS TTL above the 895-second Speaches timer cutoff, or any mismatch between
them, must fail startup. The public fleet-wide policy remains 900 seconds; the
five-second margin accounts for upstream timer scheduling jitter. GPU Speaches
is not an accepted cutover configuration.

### TranscriptionSuite

The pinned v1.3.7 production and bootstrap definitions are recovery evidence,
not available services. Both wrappers exit with status 78, both literal enable
flags are `false`, neither receives an NVIDIA device, and port 9786 is internal
and unpublished. Do not run either profile or bypass its entrypoint.

Future enablement is a separate reviewed change and requires all three gates
to land together:

1. accepted terms, exact hashes, transferred bytes, and a rebuilt cache view
   for `pyannote/speaker-diarization-community-1`;
2. an exclusive launcher/interlock covering bootstrap, startup, every request,
   cleanup, GameMode, llama.cpp, native vLLM, ComfyUI, and companion voice;
3. tested inbound authentication with literal TLS enabled and no unauthenticated
   host route.

The upstream first boot emits a one-time admin credential. A future auth design
must capture it through a private, non-journaled channel, immediately rotate it,
store only the rotated value in a regular owner-only secret file, prove the
one-time value is rejected, and remove the capture without leaving token data
in Docker logs, journald, shell history, or provenance. Until that workflow is
implemented and tested, there is no safe bootstrap or `up` command.

The intended v1.3.7 model identities remain:

- main: `nvidia/parakeet-tdt-0.6b-v3`
- live: `Systran/faster-whisper-large-v3`
- diarization: `pyannote/speaker-diarization-community-1`
- `INSTALL_NEMO=true`, `INSTALL_WHISPER=true`

## 11. Game interlock acceptance

Run this test while a llama model is resident, but without an important job in
flight:

```bash
/home/jalsarraf/.local/bin/dominus-gpu-game-start
test -e /run/user/1000/dominus-gpu/game-active
systemctl --user is-active dominus-ai-stack.service && exit 1 || true
systemctl --user is-active vllm-server.service && exit 1 || true
systemctl --user is-active --quiet vllm-proxy.service
systemctl --user is-active --quiet vllm-idle-watchdog.service
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env ps
nvidia-smi
/home/jalsarraf/.local/bin/dominus-verify-canonical-gpu-processes
```

The complete canonical Compose unit, CPU gateway, Speaches, all three enabled
GPU-capable containers (`llama-router`, `comfyui`, and `companion-voice`), and
the native vLLM backend must
be fully stopped before the hook returns. Port 1234 is therefore closed during
the game; only the independent native loopback proxy remains to return prompt
JSON HTTP 503. The hook independently rejects any canonical PID left in
NVIDIA's compute inventory. The old minute timer must remain disabled and the
250 ms watchdog active.

In a separate maintenance-window repetition, restart Docker while the marker
exists. Because the hook manually stopped every GPU container and their
policies are `unless-stopped` (TranscriptionSuite is `no`), Docker recovery
must not resurrect them. The canonical unit remains stopped and port 1234 must
remain closed:

```bash
sudo systemctl restart docker.service
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env \
  ps --status running --services \
  lmstudio-compat llama-router comfyui speaches transcriptionsuite \
  transcriptionsuite-bootstrap companion-voice \
  | grep . && exit 1 || true
curl --silent --show-error --max-time 2 \
  http://100.107.121.5:1234/health && exit 1 || true
```

Then simulate game exit:

```bash
/home/jalsarraf/.local/bin/dominus-gpu-game-end
test ! -e /run/user/1000/dominus-gpu/game-active
systemctl --user is-active vllm-proxy.service
docker compose --env-file /mnt/nvmer0/services/ai-stack/config/stack.env ps
nvidia-smi
```

The CPU gateway, empty llama router, native vLLM proxy/watchdog, and lazy
voice/ComfyUI/Speaches shells should return through `dominus-ai-stack.service`.
TranscriptionSuite stays stopped. No LLM, voice, image, or transcription weight
may load until a real request. Finally launch a GameMode test application, confirm the start hook
finishes within its 45-second timeout only after verified quiescence, and
confirm the real end hook produces the same empty state.

## 12. Client cutover

Change Amarillo/Klukai and the other fleet clients only after the preceding
gates pass:

- LLM base URL: `http://100.107.121.5:1234`
- voice URL: `http://100.107.121.5:8301`
- Klukai-internal ComfyUI facade: `http://100.107.121.5:1234/api/v1/comfy`
- Speaches URL: `http://100.107.121.5:8390`
- TranscriptionSuite: no client endpoint; reserved internal port 9786 is
  production-disabled

Distribute only the newly rotated tokens. Run Klukai casual chat, agent/tool
chat, memory extraction, scheduled Heresy route, image generation, voice TTS,
and STT end to end. Keep the old client environment file as a private rollback
copy until acceptance only for still-live Nobara endpoints. The dead Windows/
WSL2 address is not a rollback target.

On Amarillo, use the transactional cutover tool for OpenCode and Klukai's root
`.env`; do not hand-edit secrets or enable shell tracing. Provision the new LM
and voice tokens as exactly one line in the default mode-`0600` files first:

```bash
test "$(stat -c '%a' /home/jalsarraf/.config/agents/lmstudio-dominus-inference.token)" = 600
test "$(stat -c '%a' /home/jalsarraf/.config/agents/voice-dominus-inference.token)" = 600

cd /home/jalsarraf/git/klukai
ops/dominus-nobara/scripts/cutover-amarillo-consumers.sh --dry-run
ops/dominus-nobara/scripts/cutover-amarillo-consumers.sh --apply
docker compose config --quiet
```

The apply step makes and checksums an exact private backup, then atomically
updates the three OpenCode files and `/home/jalsarraf/git/klukai/.env`.
Klukai receives the literal Tailscale LM/voice URLs, the authenticated ComfyUI
facade URL, and both rotated tokens; unrelated `.env` entries are preserved and
the installed file is mode `0600`. The tool prints the exact rollback command
and never prints either token. It inventories `aichat` blockers but does not
write or restart `aichat`.

Only after `docker compose config --quiet` and the preceding target acceptance
gates pass, recreate the Amarillo core so it consumes the new environment:

```bash
docker compose up --detach --no-build companion-core
docker compose ps companion-core
curl --fail http://127.0.0.1:8300/api/health/ready
```

If application acceptance fails, run the rollback command printed by the
cutover tool and recreate `companion-core` again. The backup restores the
prior `.env` bytes and permissions exactly. That prior file is useful only for
a still-live Nobara release; a dead Windows/WSL2 endpoint is never considered
a viable rollback destination.

## 13. Backup and rollback

Before cutover, copy the following to Amarillo and an independent non-RAID
device:

- `models.lock.json`, `.manifest`, and model/config checksums;
- `preserved-target-models.lock.json` and
  `preserved-target-models.sha256` after a clean live target verification;
- the complete allowlisted per-file source manifest, aggregate tree digest,
  base `HEAD`/status, exact `models.lock` digest, no-interpolate Compose
  contract digest, and final provenance record (the base commit alone is
  insufficient);
- exported local gateway, ComfyUI, companion voice, and materializer images;
- ComfyUI user/input/output data;
- TranscriptionSuite `/data`, `/runtime`, and user config;
- native vLLM RAID logs/state and the preserved model/runtime checksum ledger;
- `config/vllm/requirements.accepted.txt` and the verified surviving
  `/mnt/nvmer0/ai/vllm` environment;
- the Docker/containerd migration ledger and both pre-migration config files;
- unique LoRAs and the Klukai reference WAV;
- the private service configuration through an encrypted secret backup.

For an application-release rollback (only to an earlier verified Nobara set):

1. Stop only `dominus-ai-stack` services; never remove volumes.
2. Repoint `models/current` and `cache/speech/current` to the recorded prior
   release/cache pair.
3. Restore the prior allowlisted source snapshot by its aggregate tree digest,
   then verify its recorded base `HEAD`, model-lock digest, no-interpolate
   Compose contract digest, and captured local image IDs.
4. Recreate the affected containers so Docker resolves the changed symlinks.
5. Run health and a one-model smoke test before reverting clients.

The model release, speech cache release, disabled TranscriptionSuite recovery
state, allowlisted source-tree digest, base `HEAD`, model-lock/Compose contract
digests, and captured image ledger form one rollback unit; never mix versions
casually.

To roll back the Docker/containerd storage migration while the retained root
stores still exist, stop every container cleanly and use the dated files—not
the lost Windows/WSL2 host:

```bash
sudo systemctl stop docker.service docker.socket containerd.service
sudo install -m 0644 \
  /mnt/nvmer0/services/ai-stack/backups/container-storage-v1/daemon.json.before \
  /etc/docker/daemon.json
sudo install -m 0644 \
  /mnt/nvmer0/services/ai-stack/backups/container-storage-v1/containerd-config.toml.before \
  /etc/containerd/config.toml
sudo rm -f -- \
  /etc/systemd/system/docker.service.d/10-dominus-nvme-mount.conf \
  /etc/systemd/system/containerd.service.d/10-dominus-nvme-mount.conf
sudo restorecon -v /etc/docker/daemon.json /etc/containerd/config.toml
sudo systemctl daemon-reload
sudo systemctl start containerd.service docker.socket docker.service
docker info --format '{{.DockerRootDir}}' | grep -Fx /var/lib/docker
test "$(sudo containerd config dump \
  | awk -F"'" '$1 == "root = " {print $2; exit}')" = /var/lib/containerd
```

Do not delete `/mnt/nvmer0/docker-data`, `/mnt/nvmer0/containerd`,
`/var/lib/docker`, or `/var/lib/containerd` as part of rollback. Resolve and
back up any post-migration image changes before choosing a later cleanup.

## 14. Acceptance and Amarillo staging deletion

Acceptance requires all of the following:

- remote size and SHA-256 verification is clean;
- all 31 preserved native-vLLM target files pass their separate checksum ledger;
- the native vLLM 195-line accepted package fingerprint matches exactly;
- all 21 catalog entries map to the exact llama preset and no fake artifacts;
- every LLM/VLM/embedding smoke test passes sequentially;
- one-model limit and the hard 900-second maximum idle TTL pass;
- native vLLM hard-stops by 900 seconds and rejects games/low-VRAM starts with
  prompt 503 responses;
- leased ComfyUI, leased voice, and CPU-only Speaches pass; both
  TranscriptionSuite definitions remain hard-disabled with no GPU or host port;
- GameMode blocks loads and restores only empty/lazy service shells;
- DockerRootDir and containerd root are both on the mounted NVMe RAID, their
  root-unit mount gates pass, and no bind write falls back to `/`;
- every published/client/transfer path is proven Tailscale-only, raw ComfyUI
  ports are absent, and all three
  public service secrets are non-empty;
- Klukai and fleet end-to-end calls pass;
- rollback was rehearsed or its artifacts were independently verified;
- the owner explicitly accepts the cutover.

Only then inspect the dedicated Amarillo staging directory, verify its marker
contains lock ID `dominus-wsl2-rebuild-2026-08-01-v1`, and remove exactly that
directory. Do not remove `~/.cache/huggingface`, `~/.lmstudio`, surviving GGUF
files, or any target release/cache. Record what was removed and that the
dedicated staging copy is no longer recoverable.

## Known parity limits

- The lost local ComfyUI image cannot be pulled by digest. This rebuild pins
  official ComfyUI 0.29.2 and the recovered model set; any unrecorded custom
  node remains an explicit compatibility test rather than a guessed install.
- Several model aliases/quantizations are exact but lost bytes were not
  provable. `models.lock.json` labels byte-exact and reconstructed artifacts
  separately instead of claiming false parity.
- PyAnnote Community-1, an exclusive transcription GPU interlock, and tested
  inbound authentication are all absent; TranscriptionSuite and its bootstrap
  therefore stay hard-disabled.
- The old WSL vision/Triton/CompreFace chain had been crash-looping for months
  and had unversioned source. It is not a cutover-critical service and should
  not be resurrected without a separate specification.
- RAID 0 provides speed and capacity only. Availability and recovery depend on
  the off-host backups above.
