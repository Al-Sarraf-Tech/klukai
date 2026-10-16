#!/usr/bin/env bash
set -euo pipefail

# The desktop keyring can block cookie initialization in headless Chrome.
# Only Flutter's disposable test profiles may use the basic password store;
# never apply this wrapper to an existing browser/account profile.
profile=''
args=()
for arg in "$@"; do
  case "$arg" in
    --user-data-dir=*) profile="${arg#--user-data-dir=}" ;;
    # Flutter supplies this by default. The private CI guest enables a scoped
    # namespace sandbox for the pinned browser instead of running unsandboxed.
    --no-sandbox|--no-sandbox=*) continue ;;
  esac
  args+=("$arg")
done
case "$profile" in
  "${TMPDIR:-/tmp}"/flutter_tools.*/flutter_tools_chrome_device.*) ;;
  *) echo 'Chrome test wrapper requires a disposable Flutter profile.' >&2; exit 64 ;;
esac

exec "${KLUKAI_CHROME_BINARY:-/usr/bin/google-chrome}" \
  --password-store=basic --disable-setuid-sandbox "${args[@]}"
