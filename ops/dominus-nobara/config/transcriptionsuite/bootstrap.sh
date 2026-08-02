#!/usr/bin/env bash
# Controlled-egress, one-shot dependency bootstrap for the pinned v1.3.7 image.
set -Eeuo pipefail

if [[ "${DOMINUS_TRANSCRIPTION_BOOTSTRAP_ENABLED:-false}" != "true" ]]; then
  echo "TranscriptionSuite bootstrap is disabled pending all admission gates" >&2
  exit 78
fi

test "$(sha256sum /app/server/uv.lock | awk '{print $1}')" = \
  9dc0f358e5a26bd052d7e2fee09e42224a1076789d9ce89ae9a1e31e0281f811
test "$(sha256sum /app/server/pyproject.toml | awk '{print $1}')" = \
  522381de1e9cd91952d5513aaa917bfc9ae8604e693c3250264fd2ef1a2c04ff
test "$(sha256sum /app/docker/bootstrap_runtime.py | awk '{print $1}')" = \
  b73a0042414a19c3a9c8990104c3f5475003b410d6fe9826282f55f62329a93b

mkdir -p /runtime/cache /models /startup-events
chown -R appuser:appuser /runtime /models /startup-events

cd /app
gosu appuser /usr/bin/python3.13 docker/bootstrap_runtime.py
gosu appuser /usr/bin/python3.13 /bootstrap/runtime_contract.py seal
