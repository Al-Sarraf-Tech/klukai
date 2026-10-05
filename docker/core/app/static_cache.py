"""Cache policy for the PWA static files mounted at /app.

StaticFiles sends ETag/Last-Modified but no Cache-Control, so browsers (iOS
Safari especially) guess. Now that CanvasKit and the fallback fonts are
served locally (no gstatic CDN — Brave/privacy), say exactly how long each
file may be kept:

- content-hashed bundles (main.dart.<hash>.js, flutter_bootstrap.<hash>.js):
  a year, immutable — a new build gets a new name;
- CanvasKit, fonts, icons, images: a day — they only change with a Flutter
  upgrade or a redeploy, and a day bounds any mismatch;
- everything else (HTML entry points, manifest, unhashed JS like the
  companion stage): revalidate every time (cheap 304s via ETag).
"""

from __future__ import annotations

import re

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

_HASHED = re.compile(r"(^|/)(main\.dart|flutter_bootstrap)\.[0-9a-f]{8}\.js$")
_DAY_PREFIXES = ("canvaskit/", "fonts/", "assets/fonts/", "assets/packages/", "icons/")
_DAY_SUFFIXES = (".wasm", ".ttf", ".otf", ".woff2", ".png", ".webp", ".jpg", ".ico")

IMMUTABLE = "public, max-age=31536000, immutable"
ONE_DAY = "public, max-age=86400"
REVALIDATE = "no-cache"


def cache_control_for(path: str) -> str:
    """Cache-Control for a path relative to the static root."""
    p = path.lstrip("/")
    if _HASHED.search(p):
        return IMMUTABLE
    if p.startswith(_DAY_PREFIXES) or p.endswith(_DAY_SUFFIXES):
        return ONE_DAY
    return REVALIDATE


class CachedStaticFiles(StaticFiles):
    """StaticFiles that stamps Cache-Control per cache_control_for()."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        # Errors (404/405) raise inside super(), so anything returned here is
        # a file or a 304 for one.
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = cache_control_for(path)
        return response
