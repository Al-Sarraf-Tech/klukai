#!/usr/bin/env bash
set -euo pipefail

# Deploy Flutter web build to dominus with content-hash cache busting.
# Renames main.dart.js and flutter_bootstrap.js with content hashes
# so Cloudflare serves fresh files on every deploy.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLUTTER="${FLUTTER:-/home/jalsarraf/flutter/bin/flutter}"
WEB_BUILD="$REPO_ROOT/web-build"
FLUTTER_APP="$REPO_ROOT/flutter_app"

echo "=== Step 1: Build Flutter web ==="
cd "$FLUTTER_APP"
"$FLUTTER" build web --release --base-href=/app/

echo ""
echo "=== Step 2: Save login.html ==="
cp "$WEB_BUILD/login.html" /tmp/login_backup.html

echo ""
echo "=== Step 3: Rsync Flutter build to web-build ==="
rsync -a --delete "$FLUTTER_APP/build/web/" "$WEB_BUILD/"

echo ""
echo "=== Step 4: Restore login.html ==="
cp /tmp/login_backup.html "$WEB_BUILD/login.html"

echo ""
echo "=== Step 5: Content-hash JS files ==="
cd "$WEB_BUILD"

# Remove any previously hashed files
rm -f main.dart.*.js flutter_bootstrap.*.js

# Hash main.dart.js
MAIN_HASH=$(md5sum main.dart.js | cut -c1-8)
MAIN_HASHED="main.dart.${MAIN_HASH}.js"
mv main.dart.js "$MAIN_HASHED"
echo "  main.dart.js → $MAIN_HASHED"

# Update reference in flutter_bootstrap.js (build config references main.dart.js)
sed -i "s|\"mainJsPath\":\"main.dart.js\"|\"mainJsPath\":\"$MAIN_HASHED\"|g" flutter_bootstrap.js
# Also update the fallback entrypoint URL
sed -i "s|c(\"main.dart.js\")|c(\"$MAIN_HASHED\")|g" flutter_bootstrap.js

# Hash flutter_bootstrap.js (after updating its content)
BOOT_HASH=$(md5sum flutter_bootstrap.js | cut -c1-8)
BOOT_HASHED="flutter_bootstrap.${BOOT_HASH}.js"
mv flutter_bootstrap.js "$BOOT_HASHED"
echo "  flutter_bootstrap.js → $BOOT_HASHED"

# Update reference in index.html
sed -i "s|flutter_bootstrap.js|$BOOT_HASHED|g" index.html

echo ""
echo "=== Step 6: Deploy to dominus ==="
rsync -avz --delete -e 'ssh -p 2222' "$WEB_BUILD/" wsl2:~/companion/web-build/

echo ""
echo "=== Step 7: Verify ==="
echo "  Login page:"
ssh -p 2222 wsl2 'curl -s http://localhost:8300/ | head -1'
echo "  Flutter app:"
ssh -p 2222 wsl2 'curl -s http://localhost:8300/app/ | grep "base href"'
echo "  Hashed JS exists:"
ssh -p 2222 wsl2 "ls ~/companion/web-build/$MAIN_HASHED ~/companion/web-build/$BOOT_HASHED"

echo ""
echo "=== Deploy complete ==="
echo "  Main JS: $MAIN_HASHED"
echo "  Bootstrap: $BOOT_HASHED"
echo "  Cloudflare will serve fresh files (new filenames = cache MISS)"
