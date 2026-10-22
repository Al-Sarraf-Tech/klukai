#!/usr/bin/env bash
set -euo pipefail

# Build the Flutter PWA and deploy it to web-build/ (bind-mounted into
# companion-core as /app/static) with content-hash cache busting.
#
#   scripts/deploy-web.sh               build + deploy
#   scripts/deploy-web.sh --build-only  build into flutter_app/build/web and
#                                       stop (no web-build/ changes, no curl)
#
# Brave / privacy: the app makes NO third-party requests.
#   - CanvasKit is served from /app/canvaskit (--no-web-resources-cdn).
#   - Fallback fonts are mirrored from fonts.gstatic.com AT BUILD TIME into
#     build/web/fonts/fallback/ (cached in $FONT_CACHE), and the bootstrap is
#     pointed at them. If the mirror cannot complete the deploy fails, unless
#     KLUKAI_ALLOW_FONT_CDN=1 (then clients fetch fonts from Google).
#   - A final audit fails the build if any shipped file references a
#     third-party origin other than the engine's (overridden) default
#     font URL.

BUILD_ONLY=0
[[ "${1:-}" == "--build-only" ]] && BUILD_ONLY=1

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FLUTTER="${FLUTTER:-/home/jalsarraf/.local/share/flutter/bin/flutter}"
WEB_BUILD="$REPO_ROOT/web-build"
FLUTTER_APP="$REPO_ROOT/flutter_app"
OUT="$FLUTTER_APP/build/web"
FONT_CACHE="${FONT_CACHE:-$HOME/.cache/klukai/fallback-fonts}"
FONT_CDN="https://fonts.gstatic.com/s/"

echo "=== Step 1: Build Flutter web (CanvasKit served locally) ==="
cd "$FLUTTER_APP"
"$FLUTTER" build web --release --base-href=/app/ --no-web-resources-cdn

echo ""
echo "=== Step 2: Mirror fallback fonts (no requests to fonts.gstatic.com) ==="
MAIN_JS="$OUT/main.dart.js"
if python3 "$REPO_ROOT/tools/ci/font_cache.py" \
    --source "$MAIN_JS" --cache "$FONT_CACHE" --output "$OUT/fonts/fallback" \
    --manifest "$REPO_ROOT/tools/ci/fallback-fonts.lock.json"; then
  sed -i 's|const klukaiFontFallbackBase = null;|const klukaiFontFallbackBase = "fonts/fallback/";|' "$OUT/flutter_bootstrap.js"
  grep -q 'klukaiFontFallbackBase = "fonts/fallback/"' "$OUT/flutter_bootstrap.js" \
    || { echo "  ERROR: bootstrap template not found (web/flutter_bootstrap.js)"; exit 1; }
  echo "  bootstrap points at verified fonts/fallback/"
elif [[ "${KLUKAI_ALLOW_FONT_CDN:-0}" == "1" ]]; then
  echo "  WARNING: verified font mirror unavailable; clients will use $FONT_CDN"
else
  echo "  ERROR: complete verified fallback fonts could not be mirrored."
  echo "  Re-run when online, or set KLUKAI_ALLOW_FONT_CDN=1 to accept Google-hosted fonts."
  exit 1
fi

echo ""
echo "=== Step 3: Third-party origin audit ==="
# The engine and loader still carry their CDN defaults as strings: the font
# base (overridden by the bootstrap above) and the CanvasKit base (dead code
# once the build config says useLocalCanvasKit). Anything else is a leak.
grep -q '"useLocalCanvasKit":true' "$OUT/flutter_bootstrap.js" \
  || { echo "  ERROR: build config does not use local CanvasKit"; exit 1; }
leaks=$(grep -rhoE 'https?://[a-zA-Z0-9.-]+\.(com|net|org|io|dev|app|cc)[^"'"'"' )]*' \
          "$OUT" --include='*.js' --include='*.html' --include='*.json' --include='*.css' 2>/dev/null \
        | grep -vE '^https://fonts\.gstatic\.com/s/$|^https://www\.gstatic\.com/flutter-canvaskit$' \
        | grep -E 'gstatic|googleapis|google-analytics|googletagmanager|cdn\.|unpkg|jsdelivr' \
        | sort -u || true)
if [[ -n "$leaks" ]]; then
  echo "  ERROR: third-party references in the build:"; echo "$leaks" | sed 's/^/    /'; exit 1
fi
echo "  none"

echo ""
echo "=== First-load weight (uncompressed / gzip -9) ==="
for f in index.html flutter_bootstrap.js main.dart.js canvaskit/canvaskit.js canvaskit/canvaskit.wasm \
         assets/fonts/MaterialIcons-Regular.otf; do
  [[ -f "$OUT/$f" ]] && printf '  %-42s %8s KB  %8s KB\n' "$f" \
    "$(( $(stat -c %s "$OUT/$f") / 1024 ))" "$(( $(gzip -9c "$OUT/$f" | wc -c) / 1024 ))"
done

echo ""
echo "=== Login from flutter_app/web/login.html ==="
cp "$FLUTTER_APP/web/login.html" "$OUT/login.html"
test -s "$OUT/login.html"
cmp -s "$FLUTTER_APP/web/login.html" "$OUT/login.html"

if [[ $BUILD_ONLY -eq 1 ]]; then
  echo ""; echo "=== Build only: $OUT (web-build/ untouched) ==="; exit 0
fi

# web-build/ is bind-mounted and served live, so every rewrite happens in the
# staging dir $OUT first; the only mutation of web-build/ is one rsync at the end.
echo ""
echo "=== Step 4: Content-hash JS files (in $OUT) ==="
cd "$OUT"

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
grep -q "$MAIN_HASHED" flutter_bootstrap.js || { echo "  ERROR: bootstrap no longer references $MAIN_HASHED"; exit 1; }

# Hash flutter_bootstrap.js (after updating its content)
BOOT_HASH=$(md5sum flutter_bootstrap.js | cut -c1-8)
BOOT_HASHED="flutter_bootstrap.${BOOT_HASH}.js"
mv flutter_bootstrap.js "$BOOT_HASHED"
echo "  flutter_bootstrap.js → $BOOT_HASHED"

# Update reference in index.html
sed -i "s|flutter_bootstrap.js|$BOOT_HASHED|g" index.html
grep -q "$BOOT_HASHED" index.html || { echo "  ERROR: index.html does not reference $BOOT_HASHED"; exit 1; }

echo ""
echo "=== Step 5: Rsync staged build to web-build (the only live change) ==="
rsync -a --delete "$OUT/" "$WEB_BUILD/"

echo ""
echo "=== Step 6: Verify local deploy ==="
# amarillo's companion-core bind-mounts ./web-build as /app/static (docker-compose.yml),
# so the rsync in Step 5 IS the deploy. No remote rsync needed.
echo "  Login page:"
curl -s http://localhost:8300/ | head -1
echo "  Flutter app:"
curl -s http://localhost:8300/app/ | grep "base href"
echo "  Hashed JS exists:"
ls "$WEB_BUILD/$MAIN_HASHED" "$WEB_BUILD/$BOOT_HASHED"

echo ""
echo "=== Deploy complete ==="
echo "  Main JS: $MAIN_HASHED"
echo "  Bootstrap: $BOOT_HASHED"
echo "  Cloudflare will serve fresh files (new filenames = cache MISS)"
