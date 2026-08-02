#!/usr/bin/env bash
# Production wrapper: refuse an unsealed/drifted runtime, force package and
# model libraries offline, then hand control to the upstream v1.3.7 entrypoint.
set -Eeuo pipefail

if [[ "${DOMINUS_TRANSCRIPTION_PRODUCTION_ENABLED:-false}" != "true" ]]; then
  echo "TranscriptionSuite production API is disabled pending all admission gates" >&2
  exit 78
fi

gosu appuser /usr/bin/python3.13 /bootstrap/runtime_contract.py verify
export UV_OFFLINE=1
export HF_HUB_OFFLINE=1
exec /app/docker/docker-entrypoint.sh "$@"
