#!/bin/sh
# Bring up the Klukai CI network with its guard first. Run by klukai-ci-network.service.
set -eu
here=$(dirname "$(readlink -f "$0")")
nft -f "$here/klukai-ci-guard.nft"
if ! virsh -q -c qemu:///system net-info klukai-ci >/dev/null 2>&1; then
  virsh -q -c qemu:///system net-define "$here/klukai-ci-net.xml"
fi
virsh -q -c qemu:///system net-autostart --disable klukai-ci >/dev/null
if [ "$(virsh -q -c qemu:///system net-info klukai-ci | awk '/^Active:/{print $2}')" != yes ]; then
  virsh -q -c qemu:///system net-start klukai-ci
fi
# Guests write CI evidence here through virtiofs: never executable on the host.
artifacts=/srv/klukai-ci/artifacts
install -d -o jalsarraf -g jalsarraf -m 0750 "$artifacts"
if ! mountpoint -q "$artifacts"; then
  mount --bind "$artifacts" "$artifacts"
fi
mount -o remount,bind,nosuid,nodev,noexec "$artifacts"

nft list table inet klukai_ci_guard >/dev/null
