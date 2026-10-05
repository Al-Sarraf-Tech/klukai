{{flutter_js}}
{{flutter_build_config}}

// No service worker. Flutter's is deprecated, and the one it still emits only
// unregisters itself, so registering it on every load was pure churn. Old
// installs that registered it still find that cleanup worker through their
// normal update check (the file stays in the build). Freshness comes from
// content-hashed filenames (scripts/deploy-web.sh) and HTTP caching.
//
// Fallback fonts (Roboto, Noto Sans JP for her Japanese, emoji): when
// scripts/deploy-web.sh has mirrored the exact files this engine asks for into
// fonts/fallback/, it swaps the null below for that path, so the app makes no
// request to fonts.gstatic.com (Brave Shields, privacy). A plain
// `flutter build` / `flutter run` keeps Flutter's default (the CDN), so dev
// builds never render without text.
const klukaiFontFallbackBase = null;

_flutter.loader.load({
  config: klukaiFontFallbackBase
    ? { fontFallbackBaseUrl: new URL(klukaiFontFallbackBase, document.baseURI).href }
    : {},
});
