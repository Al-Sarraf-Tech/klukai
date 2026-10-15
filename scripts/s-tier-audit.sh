#!/usr/bin/env bash
# Evidence-backed S/S+ audit. Missing or unexamined evidence never earns a tier.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd -P)
exec "${PYTHON:-python3}" "${REPO_ROOT}/tools/quality/audit.py" --root "${REPO_ROOT}" "$@"
