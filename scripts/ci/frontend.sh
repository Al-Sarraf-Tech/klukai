#!/usr/bin/env bash
# Mandatory frontend gate. Output is an explicitly selected private run directory,
# outside the checkout; successful evidence is manifest.json plus raw/ and pwa/.
# Existing SDK: FLUTTER=/absolute/flutter/bin/flutter (or flutter on PATH).
# Missing SDK: KLUKAI_FLUTTER_ARCHIVE_PATH (absolute owner snapshot) OR
# KLUKAI_FLUTTER_ARCHIVE_URL (immutable HTTPS), plus required
# KLUKAI_FLUTTER_ARCHIVE_SHA256. Archives must contain flutter/ including its Git
# identity/cache. A snapshot is never presented as an official release archive.
set -euo pipefail
umask 077

: "${KLUKAI_CI_OUTPUT:?Set an absolute, new private frontend evidence directory}"
for tool in python3 git curl tar sha256sum; do command -v "$tool" >/dev/null; done
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EVIDENCE="$REPO_ROOT/tools/ci/frontend_evidence.py"
REVISION="${KLUKAI_CI_REVISION:-${GITHUB_SHA:-$(git -C "$REPO_ROOT" rev-parse HEAD)}}"
OUTPUT="$KLUKAI_CI_OUTPUT"
python3 "$EVIDENCE" init --root "$REPO_ROOT" --output "$OUTPUT" --revision "$REVISION"
export CI=true FLUTTER_SUPPRESS_ANALYTICS=true DART_SUPPRESS_ANALYTICS=true

FLUTTER="${FLUTTER:-$(command -v flutter || true)}"
archive_kind=installed
archive_sha=''
if [[ -z "$FLUTTER" || ! -x "$FLUTTER" ]]; then
  : "${KLUKAI_FLUTTER_ARCHIVE_SHA256:?Missing SDK requires an explicitly pinned archive SHA256}"
  archive_sha="$KLUKAI_FLUTTER_ARCHIVE_SHA256"
  [[ "$archive_sha" =~ ^[a-f0-9]{64}$ ]] || { echo 'Invalid SDK SHA256' >&2; exit 1; }
  if [[ -n "${KLUKAI_FLUTTER_ARCHIVE_PATH:-}" ]]; then
    [[ -z "${KLUKAI_FLUTTER_ARCHIVE_URL:-}" ]] || { echo 'Select one SDK archive source' >&2; exit 1; }
    archive="$KLUKAI_FLUTTER_ARCHIVE_PATH"
    [[ "$archive" = /* && -f "$archive" && ! -L "$archive" ]] || { echo 'SDK archive must be an absolute local regular file' >&2; exit 1; }
    archive_kind=owner-snapshot
  else
    : "${KLUKAI_FLUTTER_ARCHIVE_URL:?Missing SDK requires an immutable HTTPS archive URL or absolute local snapshot}"
    [[ "$KLUKAI_FLUTTER_ARCHIVE_URL" == https://* ]] || { echo 'SDK URL must use HTTPS' >&2; exit 1; }
    archive="$OUTPUT/flutter-sdk.tar.xz"
    curl --fail --location --silent --show-error --retry 2 --proto '=https' --proto-redir '=https' \
      "$KLUKAI_FLUTTER_ARCHIVE_URL" --output "$archive"
    archive_kind=hash-pinned-https-archive
  fi
  actual_sha="$(sha256sum "$archive")"
  [[ "${actual_sha%% *}" == "$archive_sha" ]] || { echo 'SDK archive SHA256 mismatch' >&2; exit 1; }
  mkdir -m 700 "$OUTPUT/sdk"
  # Python's data filter refuses escaping names/links and device files. A single
  # flutter/ root is required so an archive cannot overwrite raw evidence.
  python3 - "$archive" "$OUTPUT/sdk" <<'PY'
import pathlib, sys, tarfile
archive, destination = sys.argv[1:]
with tarfile.open(archive, 'r:*') as package:
    for member in package.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or path.parts[:1] != ('flutter',):
            raise SystemExit('SDK archive has an unsafe or unexpected path')
    package.extractall(destination, filter='data')
PY
  [[ "$archive_kind" != hash-pinned-https-archive ]] || rm -- "$archive"
  FLUTTER="$OUTPUT/sdk/flutter/bin/flutter"
fi
[[ "$FLUTTER" = /* && -x "$FLUTTER" ]] || { echo 'Flutter must be an absolute executable' >&2; exit 1; }
SDK_ROOT="$(cd "$(dirname "$FLUTTER")/.." && pwd)"
DART="$SDK_ROOT/bin/cache/dart-sdk/bin/dart"
[[ -x "$DART" ]] || { echo 'Pinned SDK has no cached Dart executable' >&2; exit 1; }
# The full framework revision is checked independently of a human version label.
[[ "$(git -C "$SDK_ROOT" rev-parse HEAD)" == c9a6c484230f8b5e408ec57be1ef71dee1e77020 ]] \
  || { echo 'Wrong Flutter framework commit' >&2; exit 1; }
[[ -z "$(git -C "$SDK_ROOT" status --porcelain --untracked-files=no)" ]] \
  || { echo 'Flutter framework has tracked modifications' >&2; exit 1; }
CHROME="${KLUKAI_CHROME_BINARY:-$(command -v google-chrome || command -v chromium || true)}"
[[ "$CHROME" = /* && -x "$CHROME" ]] || { echo 'Chrome is required' >&2; exit 1; }
# Record/run the native executable, rather than hashing only a distribution
# launcher. Google's Linux wrapper keeps the native `chrome` beside it. Other
# distributions can set KLUKAI_CHROME_BINARY to their actual native executable.
CHROME="$(python3 - "$CHROME" <<'PY'
from pathlib import Path
import os, sys
launcher = Path(sys.argv[1]).resolve()
for path in (launcher, launcher.parent / 'chrome'):
    if path.is_file() and os.access(path, os.X_OK):
        with path.open('rb') as binary:
            if binary.read(4) == b'\x7fELF':
                print(path)
                break
else:
    raise SystemExit('Set KLUKAI_CHROME_BINARY to the native Chrome ELF executable')
PY
)"
export FLUTTER KLUKAI_CHROME_BINARY="$CHROME"
export CHROME_EXECUTABLE="$REPO_ROOT/flutter_app/tool/chrome_test.sh"
[[ -x "$CHROME_EXECUTABLE" ]] || { echo 'Disposable-profile Chrome wrapper is missing' >&2; exit 1; }
python3 - "$OUTPUT/sdk-provenance.json" "$archive_kind" "$archive_sha" <<'PY'
import json, pathlib, sys
path, kind, sha = sys.argv[1:]
pathlib.Path(path).write_text(json.dumps({'kind': kind, 'archive_sha256': sha or None}) + '\n')
PY

run() {
  python3 "$EVIDENCE" run --output "$OUTPUT" --name "$1" --cwd "$REPO_ROOT/flutter_app" -- "${@:2}"
}
run flutter-version "$FLUTTER" --version --machine
run dart-version "$DART" --version
run chrome-version "$CHROME" --version
python3 "$EVIDENCE" tools --output "$OUTPUT" --flutter "$FLUTTER" --dart "$DART" --chrome "$CHROME"
run pub "$FLUTTER" pub get --enforce-lockfile
run format "$DART" format --output=none --set-exit-if-changed lib test
run analyze "$FLUTTER" analyze --no-pub --fatal-infos

for platform in vm chrome; do
  python3 "$EVIDENCE" select-tests --root "$REPO_ROOT" --platform "$platform" > "$OUTPUT/$platform-tests.nul"
  mapfile -d '' -t tests < "$OUTPUT/$platform-tests.nul"
  # Flutter's default tester emits platform=vm machine events, but its CLI
  # does not accept --platform vm. Chrome requires explicit selection.
  platform_args=()
  if [[ "$platform" == chrome ]]; then platform_args=(--platform chrome); fi
  # One test process per vCPU of the frontend VM.
  run "$platform" "$FLUTTER" test --no-pub --machine --concurrency=4 "${platform_args[@]}" "${tests[@]}"
  python3 "$EVIDENCE" machine "$OUTPUT/raw/$platform.stdout" --platform "$platform" --exit-code 0
done

# Clean only the ignored build output. Build-only exits before production rsync,
# cache-busting rewrites and endpoint requests. Font/CDN exceptions are disabled.
rm -rf -- "$REPO_ROOT/flutter_app/build/web"
export KLUKAI_ALLOW_FONT_CDN=0
export FONT_CACHE="${FONT_CACHE:-${KLUKAI_CI_ARTIFACT_ROOT:-$(dirname "$OUTPUT")}/cache/fonts}"
run build "$REPO_ROOT/scripts/deploy-web.sh" --build-only
python3 "$EVIDENCE" create --root "$REPO_ROOT" --output "$OUTPUT" --revision "$REVISION" \
  --flutter "$FLUTTER" --dart "$DART" --chrome "$CHROME"
verified="$(python3 "$EVIDENCE" verify --output "$OUTPUT" --revision "$REVISION")"
printf '%s\n' "$verified"
# The deployer reads this line from the run's own job log and refuses a PWA
# tree on the shared artifact disk whose digest differs.
python3 -c 'import json, sys; print("KLUKAI_PWA_SHA256=" + json.loads(sys.argv[1])["pwa_sha256"])' "$verified"
