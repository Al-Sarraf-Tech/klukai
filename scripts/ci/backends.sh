#!/usr/bin/env bash
# Host owns disposable backends; only the pinned worker runs their test drivers.
set -euo pipefail
umask 077
: "${KLUKAI_CI_OUTPUT:?Supply a new absolute private evidence directory}"
: "${KLUKAI_CORE_CI_IMAGE:?Supply an immutable cached Python3.14 CI image identity}"
: "${KLUKAI_CORE_POSTGRES_IMAGE:?Supply an immutable cached PostgreSQL17 image identity}"
: "${KLUKAI_CORE_REDIS_IMAGE:?Supply an immutable cached Valkey image identity}"
: "${KLUKAI_CORE_QDRANT_IMAGE:?Supply an immutable cached Qdrant image identity}"
: "${KLUKAI_CI_REVISION:?Supply the full expected source revision}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
exec python3 -m tools.ci.backend_runner --root "$REPO_ROOT" --output "$KLUKAI_CI_OUTPUT" \
  --revision "$KLUKAI_CI_REVISION" --image "$KLUKAI_CORE_CI_IMAGE" \
  --postgres-image "$KLUKAI_CORE_POSTGRES_IMAGE" \
  --redis-image "$KLUKAI_CORE_REDIS_IMAGE" --qdrant-image "$KLUKAI_CORE_QDRANT_IMAGE" "$@"
