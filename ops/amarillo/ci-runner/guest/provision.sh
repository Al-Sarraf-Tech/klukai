#!/bin/bash
# Provision the Klukai CI base image inside a builder VM (run once by build-base.py).
# Inputs are on the read-only virtiofs share /mnt/build-inputs; the package and image
# manifest goes to /mnt/build-out. Versions follow the Dominus heavy guest inventory
# (2026-10-19): Ubuntu 24.04 + Docker (containerd store) + buildx + compose v2,
# Python 3.12 venv, Flutter 3.44.2 / Chrome 155.0.8059.79 toolchain, runner 2.338.0.
set -euo pipefail
IN=/mnt/build-inputs
OUT=/mnt/build-out
TOOLCHAIN=/opt/klukai-ci/toolchains/b838d34bba223857b3cab6b924de31a9ddee0c1f7c81df7611f3a0cb883a17c7
exec > >(tee "$OUT/provision.log") 2>&1
export DEBIAN_FRONTEND=noninteractive

# A fixed image: no background upgrades, no snaps.
systemctl disable --now unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer || true
apt-get -y purge unattended-upgrades snapd || true

apt-get update
apt-get -y install --no-install-recommends \
  docker.io docker-buildx docker-compose-v2 containerd runc \
  python3 python3-venv python3.12-venv build-essential \
  git jq curl xz-utils tar gzip rsync file ca-certificates tzdata \
  shellcheck bubblewrap apparmor apparmor-utils sudo qemu-guest-agent \
  libicu74 libssl3t64 zlib1g libkrb5-3 liblttng-ust1t64 \
  at-spi2-common fontconfig libasound2-data libasound2t64 libatk-bridge2.0-0t64 \
  libatk1.0-0t64 libatspi2.0-0t64 libavahi-client3 libavahi-common-data libavahi-common3 \
  libcups2t64 libdatrie1 libdrm-intel1 libgbm1 libgraphite2-3 libharfbuzz0b libllvm20 \
  libpango-1.0-0 libpciaccess0 libthai-data libthai0 libx11-xcb1 libxcb-dri3-0 \
  libxcb-present0 libxcb-randr0 libxcb-sync1 libxcb-xfixes0 libxcomposite1 libxdamage1 \
  libxfixes3 libxi6 libxrandr2 libxshmfence1 mesa-libgallium libnss3 fonts-dejavu-core \
  libcairo2 libcairo-gobject2 libcairo-script-interpreter2 libpixman-1-0 libxcb-render0 \
  libxcb-shm0 libxrender1 libsm6 libice6 x11-common libpcre2-16-0 libpcre2-32-0 \
  libgirepository-2.0-0 libcares2 nodejs pkgconf pkg-config \
  libcairo2-dev libffi-dev zlib1g-dev libbz2-dev libexpat1-dev libpng-dev libfreetype-dev \
  libfontconfig-dev libglib2.0-dev libx11-dev libxext-dev libxrender-dev libsm-dev uuid-dev

# The runner account: uid 1000 so evidence written through virtiofs maps to the
# relay's user on amarillo. Its only root command is the heavy-userns AppArmor load.
useradd -m -u 1000 -U -s /bin/bash runner
usermod -aG docker runner
printf 'runner ALL=(root) NOPASSWD: /usr/sbin/apparmor_parser --replace\n' > /etc/sudoers.d/klukai-ci-runner
chmod 0440 /etc/sudoers.d/klukai-ci-runner
visudo -cf /etc/sudoers.d/klukai-ci-runner

# GitHub Actions runner 2.338.0 (sha256 checked by build-base.py before boot).
install -d -o runner -g runner /opt/actions-runner
tar -xzf "$IN/actions-runner-linux-x64-2.338.0.tar.gz" -C /opt/actions-runner
printf 'ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/klukai-ci/job-started.sh\n' >> /opt/actions-runner/.env
chown -R runner:runner /opt/actions-runner

# Flutter + Chrome at the exact path and identity ci.yml pins (snapshots copied
# from the Dominus heavy guest's toolchain provenance; digests checked on the host).
install -d -m 0755 /opt/klukai-ci /opt/klukai-ci/toolchains "$TOOLCHAIN"
python3 - "$IN" "$TOOLCHAIN" <<'PY'
import sys, tarfile
src, dest = sys.argv[1], sys.argv[2]
for name in ("flutter-owner-snapshot.tar.gz", "chrome-owner-snapshot.tar.gz"):
    with tarfile.open(f"{src}/{name}") as tar:
        tar.extractall(dest, filter="data")
PY
test -x "$TOOLCHAIN/flutter/bin/flutter" && test -x "$TOOLCHAIN/chrome/chrome"
chown -R runner:runner "$TOOLCHAIN/flutter"
chmod 0700 "$TOOLCHAIN/flutter"
chown -R root:root "$TOOLCHAIN/chrome"
chmod -R u+rwX,go+rX,go-w "$TOOLCHAIN/chrome"
chmod 0755 "$TOOLCHAIN/chrome/chrome" "$TOOLCHAIN/chrome/chrome-sandbox"
echo "8f97fcd3099892c5cbd4ca62ea32ae21f0af25cc2a378f969a84f41b314bf11a  $TOOLCHAIN/chrome/chrome" | sha256sum -c -
# Every shared library Chrome links must resolve (libcairo was missing once).
if ldd "$TOOLCHAIN/chrome/chrome" | grep 'not found'; then
  echo "Chrome is missing shared libraries" >&2
  exit 1
fi
"$TOOLCHAIN/chrome/chrome" --version
install -m 0644 "$IN/klukai-ci-chrome-b838d34bba223857" /etc/apparmor.d/
apparmor_parser --replace /etc/apparmor.d/klukai-ci-chrome-b838d34bba223857

# Images the jobs use without pulling (postgres/valkey/qdrant) plus build bases,
# so fresh VMs make no anonymous Docker Hub pulls for them.
systemctl start docker
for image in \
  postgres@sha256:6f30057d31f5861b66f3545d4821f987aacf1dd920765f0acadea0c58ff975b1 \
  valkey/valkey@sha256:cb3339abbd6e18942cd8ac3349f0c371a82ba5b6204b242563ba299e7956cbd0 \
  qdrant/qdrant@sha256:f1c7272cdac52b38c1a0e89313922d940ba50afd90d593a1605dbbc214e66ffb \
  python@sha256:dc1546eefcbe8caaa1f004f16ab76b204b5e1dbd58ff81b899f21cd40541232f \
  python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 \
  gcr.io/distroless/base-debian13:nonroot@sha256:a0d70d6a97cd697d9362bc2aae4a6560dd65817e365d0043b07325a97975dc91 \
  docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
do
  docker pull -q "$image"
done

# One job per boot: the seed's JIT registration, the shared evidence tree, poweroff.
install -d -m 0755 /opt/klukai-ci/artifacts
cat "$IN/fstab.append" >> /etc/fstab
install -m 0755 "$IN/klukai-ci-job" /usr/local/sbin/klukai-ci-job
install -m 0644 "$IN/klukai-ci-job.service" /etc/systemd/system/klukai-ci-job.service
install -m 0755 "$IN/job-started.sh" /opt/klukai-ci/job-started.sh
systemctl daemon-reload

# Guests boot from the seed only, never create a login user, and need no SSH.
cat > /etc/cloud/cloud.cfg.d/99-klukai-ci.cfg <<'EOF'
datasource_list: [NoCloud]
users: []
disable_root: true
ssh_pwauth: false
EOF
systemctl disable ssh.service ssh.socket || true

# Swap file: the frontend job's Flutter tests can briefly exceed 8 GiB. (The
# cloud kernel ships no zram module.) fallocate leaves the qcow2 base sparse.
fallocate -l 4G /swapfile
chmod 0600 /swapfile
mkswap /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
# Same guest-only mitigation as the Dominus heavy guest (QEMU 10.2.2/libvirt 12).
printf 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT nousershstk"\n' \
  > /etc/default/grub.d/90-klukai-ci.cfg
update-grub

# Record what was built.
dpkg-query -W -f='${Package}\t${Version}\n' > "$OUT/packages.tsv"
docker images --digests --format '{{.Repository}}@{{.Digest}}' > "$OUT/images.txt"
{
  echo "docker=$(docker version --format '{{.Server.Version}}')"
  echo "buildx=$(docker buildx version | head -1)"
  echo "compose=$(docker compose version --short)"
  echo "python=$(python3 --version)"
  # Never run flutter here: frontend.sh requires a pristine tree at the pinned commit.
  echo "flutter=$(git -c safe.directory='*' -C "$TOOLCHAIN/flutter" rev-parse HEAD)"
  echo "chrome=$("$TOOLCHAIN/chrome/chrome" --version)"
  echo "runner=2.338.0"
} > "$OUT/versions.txt"

# Seal: no logs, identity or caches carried into job VMs, and no builder mounts.
sed -i '/build-inputs\|build-out/d' /etc/fstab
systemctl stop docker containerd
apt-get clean
rm -rf /var/lib/apt/lists/* /var/log/journal/* /tmp/* /var/tmp/*
rm -f /etc/ssh/ssh_host_*
cloud-init clean --logs --machine-id
fstrim -av || true
echo "<2>KLUKAI-BASE-OK" > /dev/kmsg
